import os
import re
import sys
import time
import json
import logging
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("twitter-groupme")

# Base URL of a Nitter instance. Swap this if the current one stops
# working — the scraping logic below targets Nitter's standard HTML
# template, which should be the same across any real Nitter instance.
NITTER_BASE_URL = os.environ.get("NITTER_BASE_URL", "https://nitter.cf")

DEFAULT_ACCOUNTS = (
    "Israelcohen911,Ishaycoen,Annabarskiy,Shiritc,Avigrin10,Moshe_nayes,"
    "Shilofreid,Eliorlevy,Arivlin1,Liran__tamari,YBhkmh75155,y_bregman1,"
    "HezkeiB,danabetzalel,ok125125,yossishtark,nbjovsr88,thebelaaz,hasidic_1"
)
ACCOUNTS = [a.strip() for a in os.environ.get("TWITTER_ACCOUNTS", DEFAULT_ACCOUNTS).split(",") if a.strip()]

GROUPME_BOT_ID = os.environ["GROUPME_BOT_ID"]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/seen.json")
MAX_BACKFILL = int(os.environ.get("MAX_BACKFILL", "5"))

# Delay (seconds) between fetching each account, to avoid tripping the
# Nitter instance's rate limiter when polling many accounts back-to-back.
FETCH_DELAY_SECONDS = float(os.environ.get("FETCH_DELAY_SECONDS", "1.5"))
# Extra backoff (seconds) after hitting a 429 specifically, on top of
# the normal per-account delay above.
RATE_LIMIT_BACKOFF_SECONDS = float(os.environ.get("RATE_LIMIT_BACKOFF_SECONDS", "5"))

GROUPME_ACCESS_TOKEN = os.environ["GROUPME_ACCESS_TOKEN"]  # from dev.groupme.com, needed to upload images
GROUPME_POST_URL = "https://api.groupme.com/v3/bots/post"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def load_seen():
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    trimmed = list(seen)[-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def split_message(text, limit=1000, reserve=10):
    max_len = limit - reserve
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def post_to_groupme(body, signature, picture_url=None):
    limit = 1000
    reserve = 10
    max_len = limit - reserve

    chunks = split_message(body, limit=limit, reserve=reserve)
    if not chunks:
        chunks = [""]

    last_with_sig = f"{chunks[-1]}\n\n{signature}".strip()
    if len(last_with_sig) <= max_len:
        chunks[-1] = last_with_sig
    else:
        chunks.append(signature)

    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"({i}/{total}) " if total > 1 else ""
        payload = {"bot_id": GROUPME_BOT_ID, "text": prefix + chunk}
        if picture_url and i == 1:
            payload["picture_url"] = picture_url

        resp = requests.post(GROUPME_POST_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            log.error("GroupMe post failed (%s): %s", resp.status_code, resp.text)
        else:
            log.info("Posted: %s", (prefix + chunk)[:80])

        if total > 1 and i < total:
            time.sleep(1)


def upload_image_to_groupme(image_url):
    try:
        img_resp = requests.get(image_url, headers=HEADERS, timeout=15)
        img_resp.raise_for_status()

        upload_resp = requests.post(
            "https://image.groupme.com/pictures",
            headers={
                "X-Access-Token": GROUPME_ACCESS_TOKEN,
                "Content-Type": img_resp.headers.get("Content-Type", "image/jpeg"),
            },
            data=img_resp.content,
            timeout=20,
        )
        upload_resp.raise_for_status()
        return upload_resp.json()["payload"]["url"]
    except Exception:
        log.exception("Failed to upload image to GroupMe")
        return None


def fetch_tweets(handle):
    """
    Scrape a Nitter instance's profile page — standard Nitter HTML
    template. Each tweet lives in a div with class 'timeline-item' and
    has a permalink under a.tweet-link containing the status ID.

    NOTE: this targets the standard Nitter template. If the configured
    instance (NITTER_BASE_URL) isn't actually running real Nitter
    software, or uses a customized template, this will return nothing —
    view-source the profile page directly and compare against what's
    expected here.
    """
    url = f"{NITTER_BASE_URL}/{handle}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    tweets = []
    for item in soup.select("div.timeline-item"):
        link_tag = item.select_one("a.tweet-link")
        if not link_tag or not link_tag.get("href"):
            continue
        href = link_tag["href"]  # e.g. "/nasa/status/1234567890#m"
        match = re.search(r"/status/(\d+)", href)
        if not match:
            continue
        tweet_id = match.group(1)

        # A retweet shows a "<name> Retweeted" header above the tweet
        # content, but the tweet-content/fullname fields below it still
        # belong to the ORIGINAL author, not the account being tracked —
        # same pattern as a Telegram forward. Capture both instead of
        # skipping retweets entirely.
        retweet_header = item.select_one(".retweet-header")
        is_retweet = retweet_header is not None

        content_el = item.select_one("div.tweet-content")
        text = content_el.get_text("\n", strip=True) if content_el else ""

        # A quote tweet embeds the quoted post in a separate div.quote
        # block alongside the account's own commentary — without this,
        # the quoted content would be silently missing entirely.
        quote_el = item.select_one("div.quote")
        quoted_text = None
        quoted_author = None
        if quote_el:
            quote_text_el = quote_el.select_one(".quote-text")
            quoted_text = quote_text_el.get_text("\n", strip=True) if quote_text_el else None
            quote_author_el = quote_el.select_one(".username")
            quoted_author = quote_author_el.get_text(strip=True).lstrip("@") if quote_author_el else None

        author_el = item.select_one("a.fullname")
        original_author = author_el.get_text(strip=True) if author_el else handle

        username_el = item.select_one("a.username")
        original_handle = username_el.get_text(strip=True).lstrip("@") if username_el else None

        image_urls = []
        for img in item.select("div.attachments img"):
            src = img.get("src", "")
            if src:
                # Nitter serves media through its own proxy path; convert
                # to an absolute URL against the configured instance.
                image_urls.append(src if src.startswith("http") else f"{NITTER_BASE_URL}{src}")

        link = f"https://x.com{href.split('#')[0]}"

        if text or image_urls:
            tweets.append({
                "id": tweet_id,
                "text": text,
                "link": link,
                "image_urls": image_urls,
                "handle": handle,
                "is_retweet": is_retweet,
                "original_author": original_author,
                "original_handle": original_handle,
                "quoted_text": quoted_text,
                "quoted_author": quoted_author,
            })

    return tweets


def format_tweet(tweet):
    body = tweet["text"]

    if tweet.get("quoted_text"):
        quoted_by = f"@{tweet['quoted_author']}" if tweet.get("quoted_author") else "unknown"
        body = f"{body}\n\nQuoting {quoted_by}:\n{tweet['quoted_text']}"

    if tweet.get("is_retweet"):
        original = f"@{tweet['original_handle']}" if tweet.get("original_handle") else tweet.get("original_author", "")
        signature = f"@{tweet['handle']} — RT from {original}"
    else:
        signature = f"@{tweet['handle']}"

    return body, f"— {signature}"


def poll_once(seen, first_run):
    all_tweets = []
    for account in ACCOUNTS:
        try:
            all_tweets.extend(fetch_tweets(account))
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429:
                log.warning("Rate limited fetching %s (429), backing off %ss", account, RATE_LIMIT_BACKOFF_SECONDS)
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
            else:
                log.exception("Failed to fetch tweets for %s", account)
        except Exception:
            log.exception("Failed to fetch tweets for %s", account)

        # Small gap between every account fetch (not just after a 429) so
        # we don't hammer the instance and trip the limiter in the first
        # place.
        time.sleep(FETCH_DELAY_SECONDS)

    if first_run:
        for tweet in all_tweets:
            seen.add(tweet["id"])
        log.info("Startup: marked %d existing tweets as seen, posting nothing", len(all_tweets))
        save_seen(seen)
        return seen

    new_tweets = [t for t in all_tweets if t["id"] not in seen]
    if len(new_tweets) > MAX_BACKFILL:
        new_tweets = new_tweets[-MAX_BACKFILL:]

    for tweet in new_tweets:
        image_urls = tweet.get("image_urls") or []
        first_image = upload_image_to_groupme(image_urls[0]) if image_urls else None

        body, signature = format_tweet(tweet)
        post_to_groupme(body, signature, picture_url=first_image)
        seen.add(tweet["id"])
        time.sleep(1)

        for extra_url in image_urls[1:]:
            extra_image = upload_image_to_groupme(extra_url)
            if extra_image:
                post_to_groupme("", "", picture_url=extra_image)
                time.sleep(1)

    if new_tweets:
        save_seen(seen)

    return seen


def main():
    log.info(
        "Starting Twitter -> GroupMe bot. Instance: %s Accounts: %s Poll interval: %ss",
        NITTER_BASE_URL, ", ".join(ACCOUNTS), POLL_SECONDS
    )
    seen = load_seen()
    first_run = True
    while True:
        try:
            seen = poll_once(seen, first_run)
            first_run = False
        except Exception:
            log.exception("Error during poll cycle")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

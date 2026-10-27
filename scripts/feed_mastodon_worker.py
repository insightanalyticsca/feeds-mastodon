#!/usr/bin/env python3
"""
feeds-mastodon worker — fetch RSS feeds, summarize with Groq, post to Mastodon.

Designed for GitHub Actions. Runs on a schedule (every 2 hours by default).

Pipeline:
  1. Read sources.json for the list of RSS feeds
  2. Fetch all feeds in parallel, collect the most recent N articles
  3. Send the batch to Groq for a concise digest summary
  4. Post the digest to Mastodon (splits into a thread if > 500 chars)

Requires these GitHub repository secrets:
  GROQ_API_KEY          — your Groq API key (https://console.groq.com/keys)
  MASTODON_INSTANCE     — e.g. https://mastodon.social
  MASTODON_ACCESS_TOKEN  — the access token from Mastodon app settings

Optional:
  GROQ_PROXY_URL        — if set, routes Groq requests through this proxy
                          (e.g. https://startling-belekoy-b0ec70.netlify.app/groq-proxy)
                          instead of calling api.groq.com directly. Use this if
                          Groq blocks your IP.
  GROQ_MODEL            — override the default model (default: openai/gpt-oss-120b)

Usage:
  python feed_mastodon_worker.py [--dry-run] [--max-articles 15] [--max-posts 3]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "scripts" / "sources.json"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_PROXY_URL = os.getenv("GROQ_PROXY_URL", "")  # optional — route through Netlify edge function
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")  # llama-3.3-70b is deprecated

MASTODON_INSTANCE = os.getenv("MASTODON_INSTANCE", "").rstrip("/")
MASTODON_TOKEN = os.getenv("MASTODON_ACCESS_TOKEN", "")
MASTODON_API = f"{MASTODON_INSTANCE}/api/v1/statuses" if MASTODON_INSTANCE else ""

MAX_POST_CHARS = 500  # Mastodon limit
# Use a browser-like UA for RSS feeds (RSSHub blocks bot-like UAs with 403)
RSS_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


# ─── RSS fetching ──────────────────────────────────────────────────────────
def fetch_feed(url, timeout=15):
    """Fetch and parse an RSS/Atom feed. Returns list of article dicts."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": RSS_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        root = ET.fromstring(data)

        articles = []
        # RSS 2.0: <rss><channel><item>
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            articles.append({"title": title, "link": link, "desc": desc, "pub": pub, "source": url})
        # Atom: <feed><entry>
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns)
        if not entries:
            entries = root.findall(".//entry")
        for entry in entries:
            title = (entry.findtext("atom:title", "", ns) or entry.findtext("title", "") or "").strip()
            link = ""
            link_el = entry.find("atom:link", ns)
            if link_el is None:
                link_el = entry.find("link")
            if link_el is not None:
                link = link_el.get("href", "")
            desc = (entry.findtext("atom:summary", "", ns) or entry.findtext("atom:content", "", ns) or "").strip()
            pub = (entry.findtext("atom:updated", "", ns) or entry.findtext("atom:published", "", ns) or "").strip()
            articles.append({"title": title, "link": link, "desc": desc, "pub": pub, "source": url})

        return articles
    except Exception as e:
        print(f"  ⚠ fetch failed: {url[:60]}... — {e}", file=sys.stderr)
        return []


def clean_html(text):
    """Strip HTML tags from RSS descriptions."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    return text.strip()


def parse_date(pub_str):
    """Parse a pubDate string into a datetime. Returns None if unparseable."""
    if not pub_str:
        return None
    try:
        return parsedate_to_datetime(pub_str)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def fetch_all_feeds(max_articles=15):
    """Fetch all feeds from sources.json, return top N most recent articles."""
    with open(SOURCES_PATH, "r") as f:
        config = json.load(f)
    feeds = config.get("feeds", [])
    print(f"📡 Fetching {len(feeds)} RSS feeds...")

    all_articles = []
    ok_count = 0
    fail_count = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_feed, feed["url"]): feed["name"] for feed in feeds}
        for future in as_completed(futures):
            name = futures[future]
            try:
                articles = future.result()
                for a in articles:
                    a["source_name"] = name
                all_articles.extend(articles)
                if articles:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

    print(f"  ✓ {ok_count} feeds OK, {fail_count} feeds failed/empty")

    # Parse dates + sort by most recent
    for a in all_articles:
        a["_dt"] = parse_date(a.get("pub", ""))
    all_articles.sort(key=lambda x: x["_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Deduplicate by title
    seen = set()
    deduped = []
    for a in all_articles:
        key = a["title"].lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(a)

    top = deduped[:max_articles]
    print(f"\n📰 Top {len(top)} most recent articles:")
    for a in top:
        print(f"  • [{a['source_name']}] {a['title'][:70]}")
    return top


# ─── Groq summarization ────────────────────────────────────────────────────
def summarize_with_groq(articles):
    """Send articles to Groq for a concise digest. Returns summary text."""
    # Build the article list for the prompt
    article_lines = []
    for i, a in enumerate(articles, 1):
        title = a["title"][:120]
        desc = clean_html(a["desc"])[:300]
        article_lines.append(f"{i}. [{a['source_name']}] {title}\n   {desc}")
    articles_text = "\n\n".join(article_lines)

    prompt = f"""You are a news digest editor. Summarize the following {len(articles)} news articles into a concise, scannable digest for Mastodon.

Rules:
- Pick the 5 most important/noteworthy stories
- For each, write ONE sentence (max 25 words) capturing the key fact
- Format as a numbered list: "1. [source] — summary"
- Write in English (translate if the source is Russian)
- Total MUST be under 450 characters (Mastodon limit is 500)
- No markdown, no bold, no hashtags in the body
- Be factual, no editorializing

Articles:
{articles_text}

Write the digest now (just the numbered list, nothing else):"""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "You are a concise news digest editor. Output only the digest, no preamble."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 600,
        "stream": False,
    }

    # If GROQ_PROXY_URL is set, route through it (the proxy holds the key)
    if GROQ_PROXY_URL:
        url = GROQ_PROXY_URL
        headers = {"Content-Type": "application/json"}
        # The proxy doesn't need the key in the Authorization header — it has its own
        print(f"🧠 Sending to Groq via proxy: {GROQ_PROXY_URL}")
    else:
        if not GROQ_API_KEY:
            print("⚠ GROQ_API_KEY not set and GROQ_PROXY_URL not set — cannot summarize")
            return None
        url = GROQ_URL
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        print(f"🧠 Sending to Groq directly (model: {GROQ_MODEL})")

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())

        text = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            print("⚠ Groq returned empty summary")
            return None
        print(f"✓ Groq summary ({len(text)} chars):\n{text}")
        return text
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"✗ Groq HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"✗ Groq error: {e}", file=sys.stderr)
        return None


# ─── Mastodon posting ─────────────────────────────────────────────────────
def split_for_mastodon(text, max_chars=MAX_POST_CHARS):
    """Split text into chunks that fit within Mastodon's character limit."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < 0:
            cut = remaining.rfind(" ", 0, max_chars)
        if cut < 0:
            cut = max_chars
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return chunks


def post_to_mastodon(text, max_posts=3, dry_run=False):
    """Post text to Mastodon. Splits into a thread if > 500 chars."""
    if not MASTODON_INSTANCE or not MASTODON_TOKEN:
        print("⚠ MASTODON_INSTANCE or MASTODON_ACCESS_TOKEN not set — skipping post")
        return False

    chunks = split_for_mastodon(text)
    if len(chunks) > max_posts:
        chunks = chunks[:max_posts]
        if len(chunks[-1]) >= MAX_POST_CHARS - 3:
            chunks[-1] = chunks[-1][:MAX_POST_CHARS - 4] + "…"

    print(f"\n📤 Posting {len(chunks)} status(es) to {MASTODON_INSTANCE}...")

    in_reply_to_id = None
    for i, chunk in enumerate(chunks):
        if dry_run:
            print(f"  [DRY RUN] Post {i+1}/{len(chunks)} ({len(chunk)} chars):")
            print(f"    {chunk[:200]}{'...' if len(chunk) > 200 else ''}")
            continue

        try:
            payload = json.dumps({
                "status": chunk,
                "visibility": "public",
                "in_reply_to_id": in_reply_to_id,
            }).encode()
            req = urllib.request.Request(
                MASTODON_API,
                data=payload,
                headers={
                    "Authorization": f"Bearer {MASTODON_TOKEN}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                post_id = result.get("id")
                url = result.get("url") or result.get("uri")
                in_reply_to_id = post_id
                print(f"  ✓ Posted {i+1}/{len(chunks)} — {url}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            print(f"  ✗ Mastodon HTTP {e.code}: {body}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"  ✗ Mastodon error: {e}", file=sys.stderr)
            return False

        if i + 1 < len(chunks) and not dry_run:
            time.sleep(0.3)

    return True


# ─── Main ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Fetch RSS → summarize with Groq → post to Mastodon")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + summarize but don't post to Mastodon")
    parser.add_argument("--max-articles", type=int, default=15, help="Max articles to send to Groq (default: 15)")
    parser.add_argument("--max-posts", type=int, default=3, help="Max Mastodon posts in a thread (default: 3)")
    parser.add_argument("--no-post", action="store_true", help="Don't post, just print the summary")
    args = parser.parse_args()

    print(f"🚀 feeds-mastodon worker — {datetime.now(timezone.utc).isoformat()}")
    print(f"   Groq model: {GROQ_MODEL}")
    print(f"   Groq proxy: {GROQ_PROXY_URL or '(direct)'}")
    print(f"   Mastodon: {MASTODON_INSTANCE or '(not configured)'}")
    print()

    articles = fetch_all_feeds(max_articles=args.max_articles)
    if not articles:
        print("❌ No articles fetched — nothing to post")
        return

    summary = summarize_with_groq(articles)
    if not summary:
        print("❌ No summary from Groq — nothing to post")
        return

    if args.no_post or args.dry_run:
        print(f"\n{'[DRY RUN]' if args.dry_run else '[NO POST]'} Summary ready ({len(summary)} chars)")
        return

    success = post_to_mastodon(summary, max_posts=args.max_posts, dry_run=args.dry_run)
    if success:
        print("\n✅ Done — digest posted to Mastodon")
    else:
        print("\n❌ Failed to post to Mastodon")
        sys.exit(1)


if __name__ == "__main__":
    main()

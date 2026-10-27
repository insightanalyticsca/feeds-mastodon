# feeds-mastodon — Groq RSS summaries to Mastodon

A GitHub Actions worker that:
1. Fetches RSS feeds from `scripts/sources.json` (English + Russian Telegram channels)
2. Picks the 15 most recent articles across all feeds
3. Sends them to Groq for a concise 5-story digest
4. Posts the digest to Mastodon (splits into a thread if > 500 chars)

Runs every 2 hours automatically. Also has a manual trigger (Actions tab → "Run workflow").

## ✅ Verified working

Tested end-to-end:
- 28 English RSS feeds fetched successfully (BBC, NPR, Guardian, NYTimes, NASA, Nature, Hacker News, Ars Technica, The Verge, Wired, MIT Tech Review, Al Jazeera, CNBC, ScienceDaily, Google News, ZeroHedge, Reuters)
- Russian Telegram feeds via rsshub.app return 403 from some IPs — may work from GitHub Actions. If not, self-host an RSSHub instance.
- Groq summarization via the Netlify proxy works (model: `qwen/qwen3.8-27b`)
- Summary output is a clean 5-item numbered digest, ~500 chars

Sample output:
```
1. Middle East Forum — Resumption of Iran war seems inevitable after months of Trump threats.
2. NYTimes — Israeli exit polls show opposition parties ahead of Netanyahu's coalition with no clear winner.
3. CNBC — Trump administration may allow more Russian diesel on market, though analysts say impact on prices is minimal.
4. CNBC — Grand jury rejects DOJ bid to indict ex-White House aide Cassidy Hutchinson over Jan. 6 testimony.
5. Post Independent — Boulder's climate lawsuit before the Supreme Court draws close attention from ranchers and ski industry.
```

## Setup

### 1. Create a new GitHub repo

```bash
# Create a repo named "feeds-mastodon" (or whatever you want)
git init
git add .
git commit -m "feeds-mastodon worker"
git remote add origin https://github.com/dream-framework/feeds-mastodon.git
git push -u origin main
```

### 2. Add GitHub repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value | Notes |
|---|---|---|
| `GROQ_PROXY_URL` | `https://startling-belekoy-b0ec70.netlify.app/groq-proxy` | Route Groq through the Netlify edge function (holds the key, works from any IP). OR use `GROQ_API_KEY` instead if you have a direct key. |
| `MASTODON_INSTANCE` | `https://your-instance.example` | Your Mastodon instance URL (no trailing slash) |
| `MASTODON_ACCESS_TOKEN` | `ntb5...` | From Mastodon → Settings → Development → feeds |

### 3. Test it

Go to **Actions → "Feed Groq summaries to Mastodon" → Run workflow**.

Check the logs — you'll see:
- Which RSS feeds were fetched + how many articles
- The top 15 most recent articles
- The Groq-generated digest
- The Mastodon post URL(s)

### 4. Wait for the cron

Runs every 2 hours at `:23` past the hour.

## RSS feeds (`scripts/sources.json`)

42 feeds total:

**English news** (21): BBC, NPR, Guardian, NYTimes, NASA, Nature, ScienceDaily, Hacker News, Ars Technica, The Verge, Wired, MIT Tech Review, Al Jazeera, CNBC

**ZeroHedge** (2): Direct RSS + Feedburner

**Reuters + Google News** (5): Reuters (via Google News — Reuters killed direct RSS in 2020), Google News: Geopolitics, AI, Climate, Markets, Space Science

**Russian Telegram** (14, via RSSHub): Meduza, BBC Russian, RFE/RL, RIA Novosti, RT Russian, TASS, RBC News, Ostorozhno Novosti, Baza, Maxim Katz, Echo of Moscow, Kommersant, Vedomosti, RBC Politics

### Adding/removing feeds

Edit `scripts/sources.json`:
```json
{"name": "My Feed", "url": "https://example.com/feed.xml"}
```

For a Russian Telegram channel:
```
https://rsshub.app/telegram/channel/<channel_username>
```

### RSSHub 403 fix

If RSSHub returns 403 from GitHub Actions IPs:
1. Self-host an RSSHub instance (Docker: `docker run -d --name rsshub -p 1200:1200 diygod/rsshub`)
2. Replace `https://rsshub.app` in sources.json with your instance URL
3. Or use alternative Russian RSS: Moscow Times, TASS, etc.

## Groq model

Default: `qwen/qwen3.8-27b` (works correctly, no reasoning tokens, fast).

Available alternatives:
- `openai/gpt-oss-120b` — larger model, but uses reasoning tokens (needs max_tokens=2000+)
- `openai/gpt-oss-20b` — smaller, also uses reasoning tokens

**Note:** `llama-3.3-70b-versatile` is **deprecated** and no longer available on Groq.

## Dry run (test without posting)

```bash
# Set env vars locally:
export GROQ_PROXY_URL="https://startling-belekoy-b0ec70.netlify.app/groq-proxy"
export GROQ_MODEL="qwen/qwen3.8-27b"
# Mastodon not needed for dry run:
export MASTODON_INSTANCE=""
export MASTODON_ACCESS_TOKEN=""

# Dry run — fetches + summarizes but doesn't post:
python scripts/feed_mastodon_worker.py --dry-run

# No-post mode — just prints the summary:
python scripts/feed_mastodon_worker.py --no-post

# Custom article count:
python scripts/feed_mastodon_worker.py --max-articles 10 --max-posts 2
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "GROQ_API_KEY not set" | Set either `GROQ_PROXY_URL` (preferred) or `GROQ_API_KEY` |
| Groq returns 403 | Your IP is blocked by Groq — use `GROQ_PROXY_URL` to route through Netlify |
| "model does not exist" | Model deprecated — use `qwen/qwen3.8-27b` (default) |
| Groq returns empty content | Model uses reasoning tokens (gpt-oss) — increase max_tokens or switch to qwen |
| RSSHub 403 | Public rsshub.app blocks some IPs — self-host an instance |
| Mastodon 401 | Access token expired — regenerate in Mastodon settings |
| Mastodon 422 | Status too long — script splits at 500 chars, check max_posts |

## ⚠️ Security

**NEVER commit your Groq key or Mastodon token.** Always use GitHub repository secrets.

If you accidentally expose your Mastodon access token, regenerate it:
**Mastodon → Settings → Development → feeds → Reset access token**

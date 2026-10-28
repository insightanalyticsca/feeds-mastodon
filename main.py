import os, json, time, hashlib, datetime, difflib, re, random
from typing import List, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs
import calendar
from datetime import timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


import requests, feedparser

# ---- Timezone support (pytz if present, else stdlib zoneinfo) ----
try:
    import pytz  # type: ignore
    def get_tz(name: str):
        return pytz.timezone(name)
except Exception:
    from zoneinfo import ZoneInfo  # py3.9+
    def get_tz(name: str):
        return ZoneInfo(name)

# =========================
# CONFIG (env-driven)
# =========================
MASTODON_INSTANCE = os.getenv("MASTODON_INSTANCE", "https://mastodon.social").rstrip("/")
MASTODON_TOKEN    = os.getenv("MASTODON_TOKEN", "")              # SAFE: won’t crash if missing
VISIBILITY        = os.getenv("MASTODON_VISIBILITY", "unlisted") # public / unlisted / private
POST_TZ           = os.getenv("POST_TZ", "Europe/Moscow")
STATE_PATH        = os.getenv("STATE_PATH", "data/state.json")

# Telegram RSS: allow multiple mirrors, comma-separated
def _split_csv(name: str, default: str = "") -> List[str]:
    val = os.getenv(name, default).strip()
    return [x.strip() for x in val.split(",") if x.strip()]

TELEGRAM_RSS_BASES = _split_csv("TELEGRAM_RSS_BASES", "https://rsshub.app,https://rsshub.rssforever.com,https://hub.slarker.me")
ALLOW_TME_S        = os.getenv("ALLOW_TME_S", "1") == "1"   # t.me/s is link fallback only (not RSS)
CACHE_BUST_TG      = os.getenv("CACHE_BUST_TG", "1") == "1" # per-slot cache buster for TG mirrors

# Network knobs
TELEGRAM_FETCH_TIMEOUT = float(os.getenv("TELEGRAM_FETCH_TIMEOUT", "4"))   # sec per TG request
TELEGRAM_FETCH_RETRIES = int(os.getenv("TELEGRAM_FETCH_RETRIES", "0"))     # retries per mirror (5xx)
GENERIC_FETCH_TIMEOUT  = float(os.getenv("GENERIC_FETCH_TIMEOUT", "8"))    # sec per regular RSS request
FETCH_CONCURRENCY      = int(os.getenv("FETCH_CONCURRENCY", "10"))         # threads for parallel fetch
RUN_HARD_TIMEOUT_SEC   = int(os.getenv("RUN_HARD_TIMEOUT_SEC", "90"))      # stop entire run after N sec

# Optional ZeroHedge site RSS fallback (not Telegram)
ZEROHEDGE_RSS = os.getenv("ZEROHEDGE_RSS", "http://feeds.feedburner.com/zerohedge/feed")
ZEROHEDGE_ENABLE_FALLBACKS = os.getenv("ZEROHEDGE_ENABLE_FALLBACKS", "1") == "1"

ANALYTICS_RSS_SOURCES = _split_csv(
    "ANALYTICS_RSS_SOURCES",
    ",".join([
        # NOEMA – features + tech/philosophy topics
        "https://www.noemamag.com/article-type/feature/feed/",
        "https://www.noemamag.com/article-topic/technology-and-the-human/feed/",
        # Palladium – main WP feed + Substack letter
        "https://www.palladiummag.com/feed/index.xml",
        "https://letter.palladiummag.com/feed",
        # Quanta – main + API host variant
        "https://quantamagazine.org/feed/",
        "https://api.quantamagazine.org/feed/"
    ])
)

# Run control
RUN_GROUP        = os.getenv("RUN_GROUP", "").strip().lower()    # geo_analytics | markets | russia
SLOT_OVERRIDE    = os.getenv("SLOT_OVERRIDE", "").strip()        # "0" | "1" | "2"
POSTS_PER_RUN    = int(os.getenv("POSTS_PER_RUN", "3"))          # 5–6 typical (default 6)
POST_SLEEP_SEC   = float(os.getenv("POST_SLEEP_SEC", "3"))

# Diversity controls
PER_CHANNEL_CAP        = int(os.getenv("PER_CHANNEL_CAP", "3"))  # base cap per channel per run
MIN_DISTINCT_CHANNELS  = int(os.getenv("MIN_DISTINCT_CHANNELS", "2"))
LIBERAL_MODE           = os.getenv("LIBERAL_MODE", "1") == "1"

# Dedupe TTL (keep dedupe but let old “seen” expire)
SEEN_TTL_HOURS = float(os.getenv("SEEN_TTL_HOURS", "168"))       # default 7 days

# Recency scoring window
MAX_HOURS_AGE   = float(os.getenv("MAX_HOURS_AGE", "96"))        # default 4 days for TG

# Debug
DEBUG_FEEDS = os.getenv("DEBUG_FEEDS", "0") == "1"

# Groq (optional; if unset, we use local heuristics)
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
GROQ_MODEL    = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_MAX_COMPLETION_TOKENS = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "2500"))
GROQ_SYSTEM_PROMPT = os.getenv(
    "GROQ_SYSTEM_PROMPT",
    (
        "Ты — профессиональный аналитик и редактор научно-популярных и политических текстов. "
        "Ты делаешь краткие, логично выстроенные конспекты статей.\n\n"
        "Требования к языку и стилю:\n"
        "• Пиши только на грамотном литературном русском языке.\n"
        "• Не используй английские слова, фразы и транслитерацию. "
        "Исключение: общеизвестные аббревиатуры типа ООН, ЕС, НАТО, ВТО, БРИКС, МВФ.\n"
        "• Подбирай нормальные русские термины, а не кальки с английского.\n"
        "• Не используй разговорные выражения, сленг и канцелярит.\n\n"
        "Требования к содержанию:\n"
        "• Опирайся только на факты из исходного текста; не добавляй домыслов и оценок.\n• НЕ пиши банальные вводные фразы вроде «объединяет ключевые события», «основной акцент делается на», «охватывает военную ситуацию, дипломатические контакты и экономические показатели».\n• НЕ описывай СТРУКТУРУ материала (что в нём есть) — вместо этого излагай САМИ факты.\n• Начинай сразу с факта: кто что сказал, что произошло, какие цифры. Без мета-описаний.\n• Каждое предложение должно нести конкретную информацию: имя, событие, число, позицию.\n"
        "• Не повторяй одну и ту же мысль разными словами.\n"
        "• Не обращайся к читателю во втором лице и не давай советов.\n"
        "• Не используй эмодзи, хэштеги, списки и Markdown-разметку.\n\n"
        "Формат ответа:\n"
        "• 5–6 содержательных параграфов.\n"
        "• Первое предложение — чёткая формулировка темы и основной идеи материала.\n"
        "• Следующие предложения — ключевые факты, аргументы и вывод автора.\n"
        "• Если уместно, последнее предложение аккуратно фиксирует общий вывод авторов."
    )
)

# =========================
# TELEGRAM CHANNEL LISTS
# =========================
def _env_list(name: str) -> list:
    v = os.getenv(name, "").strip()
    return [x.strip().lower() for x in v.split(",") if x.strip()]

# Baseline seeds (handles only, no @). Add more in repo Variables via EXTRA_TG_*
BASE_TG: Dict[str, List[str]] = {
    "geopolitics": [
        "ddgeopolitics","geromanat","kalibrated",
        "russiancouncil","valdaiclub","mfarussia",
        "tassagency_en","rianovostieng","rtnews",
    ],
    "markets": [
        "zerohedge_official","rbc_news","vedomosti",
        "kommersant","cbr_ru","moex",
        "sberinvestments","alfa_investments",
        "rian_ru","tassagency_en",
    ],
    "analytics": [
        "russiancouncil","valdaiclub","vedomosti",
        "kommersant","tassagency_en","rianovostieng",
        "rtnews","ria_analytics","rg_ru",
    ],
    "russia": [
        "rybar","vedomosti","kommersant",
        "rbc_news","rian_ru","tassagency_ru",
        "mfarussia","rt_russian","izvestia",
    ],
}

# Extra from env
EXTRA_TG = {
    "geopolitics": _env_list("EXTRA_TG_GEOPOLITICS"),
    "markets":     _env_list("EXTRA_TG_MARKETS"),
    "analytics":   _env_list("EXTRA_TG_ANALYTICS"),
    "russia":      _env_list("EXTRA_TG_RUSSIA"),
}

# Reserve (only used if supply is thin)
RESERVE_TG: Dict[str, List[str]] = {
    "geopolitics": [
        "intelligencebrief","geopoliticslive","conflicts_global",
        "syrianews","china3army","indian_defence_updates",
        "eurasian_analyst","iswresearch",
        "intelreform","osint_bear","osint_east"
    ],
    "markets": [
        "moneytalksnews","fxinsider","commoditiesworld","oilandgasworld",
        "business_economy","macromicro","macro_daily","bondmarket",
        "techstocksupdates","economicsbrief","investing_room","tradingfloor"
    ],
    "analytics": [
        "thinktankwatch","policybrief","globalriskinsights","econpolicyjournal",
        "macrovoices","csisorg","chathamhouseorg","cebr_uk",
        "bne_intellinews","oxfordeconomics","imfnews","oecd"
    ],
    "russia": [
        "lentach","bazabazon","shot_shot","readovkanews",
        "operdrain","svarschiki","kremlin_watch","mash",
        "ura_ru","bbbreaking","rt_russian","svtvnews"
    ],
}

NEWS_MAX_ITEMS = 80  # per feed pull cap (TG posts are short)

# Identify this bot to feed servers — use browser-y UA to avoid 403/blocked feeds (esp. ZeroHedge)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)
feedparser.USER_AGENT = BROWSER_UA
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.7",
    "Connection": "close",
}

# =========================
# Ranking/selection config
# =========================
SOURCE_WEIGHTS = {
    "t.me": 4.2, "telegram.me": 4.0, "telegram.dog": 3.8, "telegram.im": 3.6,
    "rsshub.app": 3.4,
    "zerohedge.com": 5.0,  # boosted so ZH rules market-related content
}

THEME_KEYWORDS = {
    "geopolitics": {
        "ukraine": 1.1, "gaza": 1.1, "israel": 1.1, "russia": 1.2, "kremlin": 1.1,
        "nato": 1.1, "taiwan": 1.1, "china": 1.0, "sanction": 1.1, "brics": 1.0,
    },
    "markets": {
        "инфляц": 1.6, "inflation": 1.6, "цб": 1.5, "cbr": 1.5, "ставк": 1.4,
        "fed": 1.4, "earnings": 1.3, "ipo": 1.2, "cpi": 1.2, "ppi": 1.2,
        "облигац": 1.2, "bond": 1.2, "gdp": 1.2, "нефть": 1.1, "oil": 1.1,
        "рубл": 1.0, "ruble": 1.0, "moex": 1.0
    },
    "analytics": {
        "analysis": 1.2, "аналитик": 1.2, "отчёт": 1.1, "report": 1.1,
        "outlook": 1.1, "forecast": 1.1, "эксперт": 1.0, "brief": 1.0
    },
    "russia": {
        "росси": 1.6, "russia": 1.4, "kremlin": 1.3, "санкц": 1.2, "экономик": 1.2,
        "мобилизац": 1.1, "границ": 1.0
    },
}

NEGATIVE_FLAGS = ["live blog","newsletter","subscribe","promo","contest"]

# =========================
# UTILITIES
# =========================
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def slot_key_for_20min(now_local: datetime.datetime) -> str:
    slot_index = (now_local.minute // 20)
    slot_floor_minute = slot_index * 20  # 00, 20, 40
    return f"{now_local.strftime('%Y-%m-%d %H')}:{slot_floor_minute:02d}"

def _iso_to_dt(s: str):
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def load_state() -> dict:
    state = {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    if not isinstance(state.get("feeds"), dict): state["feeds"] = {}
    if not isinstance(state.get("seen"), dict):  state["seen"] = {}
    return state

def prune_seen(state: dict, ttl_hours: float) -> None:
    if "seen" not in state or not isinstance(state["seen"], dict):
        state["seen"] = {}
        return
    cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=max(1.0, ttl_hours))
    keep = {}
    for k, v in state["seen"].items():
        dt = _iso_to_dt(v) if isinstance(v, str) else None
        if dt and dt >= cutoff:
            keep[k] = v
    removed = len(state["seen"]) - len(keep)
    state["seen"] = keep
    if removed:
        print(f"[state] pruned {removed} old seen entries (ttl={ttl_hours}h)")

def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_instance_max_chars(instance: str) -> int:
    url = f"{instance}/api/v2/instance"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        return int(r.json()["configuration"]["statuses"]["max_characters"])
    except Exception:
        return 500

def _base_domain_from_link(link: str) -> str:
    try:
        host = urlparse(link).netloc.lower()
        host = host.replace("www.", "")
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""

_UTM_KEYS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","utm_id",
             "gclid","fbclid","igshid","mc_cid","mc_eid","ref","source"}

def _canon_link(link: str) -> str:
    """Light canonicalization to help dedupe without being too strict."""
    if not link: return ""
    try:
        u = urlparse(link)
        scheme = "https"
        host = u.netloc.lower().replace("www.", "")
        path = u.path or "/"
        query = u.query

        # Normalize Telegram
        if host.endswith("telegram.me"): host = "t.me"
        if host.endswith("t.me") and ALLOW_TME_S:
            path = re.sub(r"^/s/", "/", path)

        # Strip query tracking params (keep non-tracking keys)
        if query:
            qs = parse_qs(query, keep_blank_values=False)
            qs = {k:v for k,v in qs.items() if k.lower() not in _UTM_KEYS}
            if qs:
                # preserve order deterministically
                query = "&".join([f"{k}={qs[k][0]}" for k in sorted(qs.keys()) if qs[k]])
            else:
                query = ""

        # Trim trailing slash (except root)
        if path != "/":
            path = path.rstrip("/")

        # ZeroHedge: keep only article path (strip any leftover params/fragments)
        if host.endswith("zerohedge.com"):
            query = ""

        return urlunparse((scheme, host, path, "", query, ""))
    except Exception:
        return link

# =========================
# FETCH CORE (requests + feedparser)
# =========================
def _requests_get(url: str, timeout_s: float, headers: dict) -> requests.Response:
    return requests.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)

def _robust_parse_one(url: str, *, timeout_s: float, retries: int, allow_cache_headers: bool, etag=None, modified=None):
    headers = dict(HEADERS)
    if allow_cache_headers:
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified

    last_exc = None
    for attempt in range(max(1, retries + 1)):
        try:
            resp = _requests_get(url, timeout_s, headers)
            if resp.status_code == 304:
                d = feedparser.parse(b""); setattr(d, "status", 304); return d
            if resp.status_code >= 500:
                time.sleep(0.25); continue
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            last_exc = e
            time.sleep(0.25)
    if DEBUG_FEEDS:
        print(f"[debug] parse fail {url}: {last_exc}")
    return None

# =========================
# FEED FETCHING (Telegram multibase, PARALLEL)
# =========================
def _tg_feed_urls_for_handle(handle: str, slot_key: str = "") -> List[str]:
    qb = ""
    if CACHE_BUST_TG and slot_key:
        qb = "?t=" + slot_key.replace(" ", "").replace(":", "")
    bases = list(TELEGRAM_RSS_BASES)
    random.shuffle(bases)
    return [f"{base.rstrip('/')}/telegram/channel/{handle}{qb}" for base in bases]

def _fetch_tg_handle_once(handle: str, slot_key: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for url in _tg_feed_urls_for_handle(handle, slot_key):
        d = _robust_parse_one(
            url, timeout_s=TELEGRAM_FETCH_TIMEOUT, retries=TELEGRAM_FETCH_RETRIES,
            allow_cache_headers=False
        )
        if not d or not getattr(d, "entries", None) or getattr(d, "status", None) == 304:
            if DEBUG_FEEDS:
                print(f"[debug] TG {handle} from {url.split('/telegram')[0]} → empty/304")
            continue
        for entry in d.entries[:NEWS_MAX_ITEMS]:
            title   = (entry.get("title") or "").strip()
            link    = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or "").strip()
            try:
                for l in entry.links:
                    href = l.get("href") or ""
                    if "t.me" in href: link = href; break
            except Exception:
                pass
            link = _canon_link(link)
            ts = 0
            pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            if pp: ts = int(calendar.timegm(pp))
            if not (title or summary or link): continue
            items.append({
                "title": title, "link": link, "summary": summary, "theme": "",
                "ts": ts, "domain": _base_domain_from_link(link) or _base_domain_from_link(url),
                "channel": handle.lower(), "feed_url": url,
                "entry_id": entry.get("id") or entry.get("guid") or "",
            })
        if items:
            if DEBUG_FEEDS:
                print(f"[debug] TG {handle}: {len(items)} entries")
            break
    return items

def _is_zerohedge(url: str) -> bool:
    try:
        return "zerohedge.com" in urlparse(url).netloc.lower()
    except Exception:
        return False

def _zerohedge_variants(url: str, slot_key: str) -> List[str]:
    # Try multiple plausible endpoints; add cache-bust so proxies/CDNs refresh
    base = "https://www.zerohedge.com"
    candidates = []
    # Keep the provided URL first
    candidates.append(url)
    if ZEROHEDGE_ENABLE_FALLBACKS:
        candidates += [
            f"{base}/site/feed",
            f"{base}/feed",
            f"{base}/fullrss",
            f"{base}/fullrss2.xml",
            f"{base}/rss",
        ]
    # De-dup while preserving order
    seen = set()
    out = []
    for c in candidates:
        if not c: continue
        if c in seen: continue
        seen.add(c)
        out.append(c)
    # Cache-bust
    qb = ""
    if slot_key:
        qb = ("&" if ("?" in out[0]) else "?") + "t=" + slot_key.replace(" ", "").replace(":", "")
    return [c + qb for c in out]

def _extract_tg_handle_from_link_or_feed(link: str, feed_url: str) -> str:
    try:
        for u in (link, feed_url):
            if not u:
                continue
            p = urlparse(u)
            host = p.netloc.lower()
            path = p.path.strip("/")
            if "t.me" in host or "telegram.me" in host:
                return path.split("/", 1)[0].lower()
    except Exception:
        pass
    return ""

def _fetch_rss_url(url: str, cache: dict, slot_key: str) -> List[Dict[str, Any]]:
    # ZeroHedge gets special handling (variants + browser UA + no-cache headers if needed)
    urls_to_try = _zerohedge_variants(url, slot_key) if _is_zerohedge(url) else [url]
    out: List[Dict[str, Any]] = []

    for idx, u in enumerate(urls_to_try):
        use_cache_headers = (idx == 0) and (not _is_zerohedge(u))  # for ZH prefer fresh
        d = _robust_parse_one(
            u, timeout_s=GENERIC_FETCH_TIMEOUT, retries=1,
            allow_cache_headers=use_cache_headers, etag=cache.get("etag"), modified=cache.get("modified")
        )
        if not d or getattr(d, "status", None) == 304 or not getattr(d, "entries", None):
            if DEBUG_FEEDS:
                print(f"[debug] RSS {u} → empty/304")
            continue

        # Save cache only for non-ZH (ZH variants may have divergent cache semantics)
        if not _is_zerohedge(u):
            cache["etag"] = getattr(d, "etag", None)
            cache["modified"] = getattr(d, "modified", None)

        for entry in getattr(d, "entries", [])[:NEWS_MAX_ITEMS]:
            title   = (entry.get("title") or "").strip()
            link    = _canon_link((entry.get("link") or "").strip())
            summary = (entry.get("summary") or "").strip()
            ts = 0
            pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            if pp: ts = int(calendar.timegm(pp))
            if not (title or summary or link): continue
            out.append({
                "title": title, "link": link, "summary": summary, "ts": ts,
                "domain": _base_domain_from_link(link) or _base_domain_from_link(u),
                "channel": "zerohedge" if ("zerohedge" in (link or u)) else _extract_tg_handle_from_link_or_feed(link, u),
                "feed_url": u, "entry_id": entry.get("id") or entry.get("guid") or "",
            })

        if DEBUG_FEEDS:
            print(f"[debug] RSS {u}: {len(out)} entries (cum)")

        # If we got entries from this variant, no need to try more variants
        if out:
            break

    return out

def fetch_theme_parallel(theme: str, theme_sources: List[str], state: dict, slot_key: str,
                         deadline_ts: float) -> List[Dict[str, Any]]:
    """Fetch a theme's sources in parallel with a hard deadline."""
    items: List[Dict[str, Any]] = []
    feeds_cache = state.setdefault("feeds", {})
    sources = list(theme_sources)
    random.shuffle(sources)

    def time_left() -> float:
        return max(0.0, deadline_ts - time.monotonic())

    if DEBUG_FEEDS:
        print(f"[fetch] theme={theme} sources={len(sources)} conc={FETCH_CONCURRENCY} deadline={int(time_left())}s")

    def one(src: str) -> List[Dict[str, Any]]:
        if time_left() <= 0:
            return []
        if src.startswith("tg:"):
            handle = src[3:]
            got = _fetch_tg_handle_once(handle, slot_key)
            for g in got: g["theme"] = theme
            return got
        cache = feeds_cache.setdefault(src, {})
        got = _fetch_rss_url(src, cache, slot_key)
        for g in got: g["theme"] = theme
        return got

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, FETCH_CONCURRENCY)) as ex:
        futs = {ex.submit(one, s): s for s in sources}
        try:
            for fut in as_completed(futs, timeout=max(1, int(time_left())) if time_left() > 0 else 1):
                if time_left() <= 0:
                    break
                src = futs[fut]
                try:
                    got = fut.result()
                    results.extend(got)
                    if DEBUG_FEEDS:
                        src_disp = src if not src.startswith("tg:") else f"tg:{src[3:]}"
                        print(f"[fetch/done] {theme}:{src_disp} +{len(got)} (total {len(results)})")
                except Exception as e:
                    if DEBUG_FEEDS:
                        print(f"[fetch/error] {theme}:{src} {e}")
        except Exception as e:
            if DEBUG_FEEDS:
                print(f"[fetch/timeout] theme={theme} {e}")

    if DEBUG_FEEDS:
        print(f"[fetch/summary] theme={theme} total={len(results)} time_left={int(time_left())}s")
    return results

# Backwards-compatible wrapper (used by main)
def fetch_feed_items(theme: str, theme_sources: List[str], state: dict, max_items: int, slot_key: str) -> List[Dict[str, Any]]:
    deadline_ts = time.monotonic() + RUN_HARD_TIMEOUT_SEC
    return fetch_theme_parallel(theme, theme_sources, state, slot_key, deadline_ts)

# =========================
# DEDUPE (light, not too strict)
# =========================
def dedupe_items(items: List[Dict[str, Any]], state: dict) -> List[Dict[str, Any]]:
    """
    Light dedupe: canonical link, entry_id, and channel+lower(title).
    Avoid cross-domain title folding to keep variety (esp. market stories).
    """
    seen = state.setdefault("seen", {})
    out: List[Dict[str, Any]] = []
    for it in items:
        title = (it.get("title") or "")[:220]
        link  = _canon_link(it.get("link") or "")
        entry_id = (it.get("entry_id") or "")
        channel = (it.get("channel") or it.get("domain") or "unknown").lower()

        k_link = link or ""
        k_eid  = f"eid:{entry_id}" if entry_id else ""
        k_ttl  = sha(f"{channel}|{title.lower()}")

        keys = [k for k in (k_link, k_eid, k_ttl) if k]
        if any(k in seen for k in keys):
            continue
        stamp = datetime.datetime.now(timezone.utc).isoformat()
        for k in keys:
            seen[k] = stamp
        it["link"] = link  # store canonicalized
        out.append(it)
    return out

# =========================
# SCORING / SELECTION (diversity with progressive fallback)
# =========================
def _hours_ago(ts: int) -> float:
    if not ts: return MAX_HOURS_AGE * 2
    return (time.time() - ts) / 3600.0

def _score_item(it: Dict[str, Any]) -> float:
    score = 0.0
    score += SOURCE_WEIGHTS.get(it.get("domain", ""), 1.0)
    hrs = _hours_ago(it.get("ts", 0))
    score += max(0.0, (MAX_HOURS_AGE - min(MAX_HOURS_AGE, hrs))) / MAX_HOURS_AGE * 2.5
    text = f"{it.get('title','')} {it.get('summary','')}".lower()
    for kw, w in THEME_KEYWORDS.get(it.get("theme", ""), {}).items():
        if kw in text: score += w
    for flag in NEGATIVE_FLAGS:
        if flag in text: score -= 1.0
    tl = len(it.get("title", ""))
    if 30 <= tl <= 120: score += 0.5
    return score

def _cluster_and_pick(items: list, sim_threshold: float = 0.86) -> list:
    items = sorted(items, key=lambda x: x["_score"], reverse=True)
    picked: list = []
    used = [False] * len(items)
    for i, a in enumerate(items):
        if used[i]: continue
        used[i] = True
        for j in range(i + 1, len(items)):
            if used[j]: continue
            r = difflib.SequenceMatcher(None, a["title"].lower(), items[j]["title"].lower()).ratio()
            if r >= sim_threshold: used[j] = True
        picked.append(a)
    return picked

def _rr_pick_with_cap(by_ch: Dict[str, list], cap: int, n: int) -> list:
    ch_order = sorted(by_ch.keys(), key=lambda c: by_ch[c][0]["_score"], reverse=True)
    picked: list = []
    ch_count: Dict[str, int] = {c: 0 for c in by_ch}
    idx = 0
    empties = set()
    while len(picked) < n and len(empties) < len(ch_order):
        ch = ch_order[idx % len(ch_order)]; idx += 1
        if ch in empties: continue
        if ch_count[ch] >= cap:
            empties.add(ch); continue
        bucket = by_ch[ch]
        while bucket and ch_count[ch] < cap and len(picked) < n:
            cand = bucket.pop(0)
            picked.append(cand)
            ch_count[ch] += 1
            break
        if not bucket or ch_count[ch] >= cap:
            empties.add(ch)
    return picked

def _diverse_with_progressive_cap(themed_scored: list, n: int, base_cap: int, min_distinct: int) -> list:
    by_ch: Dict[str, list] = {}
    for it in themed_scored:
        ch = (it.get("channel") or it.get("domain") or "unknown").lower()
        by_ch.setdefault(ch, []).append(it)
    for ch in by_ch:
        by_ch[ch].sort(key=lambda x: x["_score"], reverse=True)

    for cap in range(max(1, base_cap), max(1, n) + 1):
        temp = {ch: list(lst) for ch, lst in by_ch.items()}
        picks = _rr_pick_with_cap(temp, cap, n)
        distinct = len({(p.get("channel") or p.get("domain") or "unknown").lower() for p in picks})
        if len(picks) >= n and distinct >= min(1, min_distinct):
            if DEBUG_FEEDS:
                print(f"[select/diversity] cap={cap} distinct={distinct}/{min_distinct}")
            return picks[:n]
        if len(picks) >= n:
            if DEBUG_FEEDS:
                print(f"[select/diversity] cap={cap} filled count (distinct={distinct})")
            return picks[:n]

    flat = []
    for lst in by_ch.values(): flat.extend(lst)
    flat.sort(key=lambda x: x["_score"], reverse=True)
    if DEBUG_FEEDS:
        print(f"[select/diversity] fallback score-only; supply={len(flat)}")
    return flat[:n]

def _select_top_for_theme_diverse(items: list, theme: str, n: int) -> list:
    themed = [dict(it) for it in items if it.get("theme") == theme]
    if not themed:
        return []
    for it in themed:
        it["_score"] = _score_item(it)
    themed = _cluster_and_pick(themed)

    # For markets: ZeroHedge rules. Fill with zerohedge.com first, then others if any slots remain.
    if theme == "markets":
        zh = [it for it in themed if (it.get("domain") or "").lower() == "zerohedge.com"]
        non_zh = [it for it in themed if (it.get("domain") or "").lower() != "zerohedge.com"]

        zh.sort(key=lambda x: x["_score"], reverse=True)
        picks: list = []
        if zh:
            picks.extend(zh[:n])

        remaining = n - len(picks)
        if remaining > 0 and non_zh:
            picks.extend(_diverse_with_progressive_cap(non_zh, remaining, PER_CHANNEL_CAP, MIN_DISTINCT_CHANNELS))

        return picks[:n]

    # Other themes keep normal diversity logic
    return _diverse_with_progressive_cap(themed, n, PER_CHANNEL_CAP, MIN_DISTINCT_CHANNELS)

def select_top_per_theme(items: list, quotas: Dict[str, int]) -> list:
    bucketed: list = []
    for theme, n in quotas.items():
        picks = _select_top_for_theme_diverse(items, theme, n)
        bucketed.extend(picks)
    for it in bucketed:
        it["_score"] = _score_item(it)
    bucketed = _cluster_and_pick(bucketed)
    return bucketed[:sum(quotas.values())]

# =========================
# LLM PREP + SUMMARIZERS (per-item)
# =========================

def _cleanup_russian_summary(text: str, max_paragraphs: int = 4) -> str:
    """
    Нормализует ответ модели:
    - сохраняет структуру абзацев (разделённых пустой строкой)
    - убирает лишние пробелы внутри абзацев
    - удаляет повторяющиеся абзацы
    - ограничивает количество абзацев
    - следит, чтобы каждый абзац заканчивался на .!?…
    """
    if not text:
        return ""

    # Нормализуем переносы строк
    t = text.replace("\r\n", "\n").strip()

    # Делим по пустым строкам на абзацы
    raw_paras = re.split(r"\n\s*\n", t)
    paras: list[str] = []
    seen = set()

    for p in raw_paras:
        p = p.strip()
        if not p:
            continue

        # Внутри абзаца лишние пробелы схлопываем, но переносы между абзацами НЕ трогаем
        p_clean = re.sub(r"\s+", " ", p).strip()
        norm = p_clean.lower()
        if norm in seen:
            continue
        seen.add(norm)

        # Чтобы абзац не обрывался на полуслове
        if p_clean and p_clean[-1] not in ".!?…":
            p_clean += "."

        paras.append(p_clean)
        if len(paras) >= max_paragraphs:
            break

    if not paras:
        return ""

    # Соединяем абзацы через пустую строку
    return "\n\n".join(paras)
    
def build_item_context(it: Dict[str, Any], max_chars: int = 2000) -> str:
    t = (it.get("title") or "").strip()
    s = (it.get("summary") or "").strip()
    l = (it.get("link") or "").strip()
    return f"Заголовок: {t}\nКраткое описание: {s}\nURL: {l}"[:max_chars]

def summarize_item_locally(it: Dict[str, Any]) -> str:
    """
    Фоллбек на случай проблем с Groq:
    даём короткое нейтральное русское описание без перевода.
    """
    t = (it.get("title") or "").strip()
    if t:
        return _cleanup_russian_summary(
            f"Материал под заголовком «{t}». Для деталей см. оригинал по ссылке."
        )
    return "Материал без заголовка. Для деталей см. оригинал по ссылке."



def groq_summarize_item(it: Dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        return summarize_item_locally(it)

    ctx = build_item_context(it)
    url = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": GROQ_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Ниже приведены заголовок, краткое описание и ссылка на материал. "
                    "Сделай краткий логичный конспект.\n\n"
                    "СТРОГО соблюдай требования:\n"
                    "• Ответ только на русском языке.\n"
                    "• Не используй английские слова, фразы и транслитерацию "
                    "(кроме общепринятых аббревиатур вроде ООН, ЕС, НАТО, МВФ, ВТО, БРИКС).\n"
                    "• 5–6 логически правильных, емких параграфов, отражающих главные мысли публикации.\n"
                    "• Если текст короткий или неполный (часто в Telegram-постах),\n"
                    "  делай ёмкий конспект из 2-3 предложений. НЕ пиши «отсутствует текст».\n"
                    "• Не повторяй одну и ту же мысль разными словами.\n"
                    "• Только факты из текста, без домыслов и оценок.\n"
                    "• Без списков, эмодзи, хэштегов и обращений к читателю.\n\n"
                    "Контекст статьи:\n"
                    f"{ctx}"
                ),
            },
        ],
        "temperature": 0.0,
        "n": 1,
        "max_tokens": GROQ_MAX_COMPLETION_TOKENS,
        "stream": False,
        
    }

    for _ in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=45)
            if r.status_code == 429:
                # rate limit → подождать и попробовать ещё раз
                delay = min(5, max(1, int(r.headers.get("retry-after", "2"))))
                time.sleep(delay)
                continue
            r.raise_for_status()
            j = r.json()
            raw = (j.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""
            cleaned = _cleanup_russian_summary(raw)
            if cleaned:
                return cleaned
        except Exception:
            time.sleep(2)

    # Фоллбек — лаконичный русский текст без перевода
    return summarize_item_locally(it)

# =========================
# MASTODON
# =========================
def post_to_mastodon(status: str, visibility: str = "unlisted", idem_key: str = "") -> dict:
    if not MASTODON_TOKEN:
        print("[info] MASTODON_TOKEN is not set — skipping post.")
        return {}
    url = f"{MASTODON_INSTANCE}/api/v1/statuses"
    headers = {"Authorization": f"Bearer {MASTODON_TOKEN}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    r = requests.post(url, headers=headers, data={"status": status, "visibility": visibility}, timeout=10)
    r.raise_for_status()
    return r.json()

# =========================
# ROTATION (20-min slots → 3 groups)
# =========================
GROUP_TITLES = {
    "geo_analytics": "Геополитика / Аналитика",
    "markets": "Рынки",
    "russia": "Россия",
}
GROUP_QUOTAS = {
    "geo_analytics": {"geopolitics": 3, "analytics": 3},  # total 6
    "markets": {"markets": 6},
    "russia": {"russia": 6},
}
THEME_LABEL_RU = {
    "geopolitics": "Геополитика",
    "markets": "Рынки",
    "analytics": "Аналитика",
    "russia": "Россия",
}

def resolve_group(now_local: datetime.datetime) -> str:
    if RUN_GROUP in GROUP_QUOTAS: return RUN_GROUP
    slot = int(SLOT_OVERRIDE) if SLOT_OVERRIDE in {"0","1","2"} else (now_local.minute // 20) % 3
    return ["geo_analytics", "markets", "russia"][slot]

# =========================
# BUILD SOURCES
# =========================
def build_theme_sources() -> Dict[str, List[str]]:
    feeds: Dict[str, List[str]] = {}
    for theme in ("geopolitics","markets","analytics","russia"):
        handles = list(dict.fromkeys(
            [f.lower() for f in (EXTRA_TG.get(theme) or [])] +
            BASE_TG.get(theme, [])
        ))
        feeds[theme] = [f"tg:{h}" for h in handles]

        if theme == "analytics" and ANALYTICS_RSS_SOURCES:
            feeds[theme].extend(ANALYTICS_RSS_SOURCES)
        
        if theme == "markets" and ZEROHEDGE_RSS:
            feeds[theme].append(ZEROHEDGE_RSS)  # non-TG fallback (handled with variants)
            
    return feeds

def build_theme_reserves() -> Dict[str, List[str]]:
    reserves: Dict[str, List[str]] = {}
    for theme in ("geopolitics","markets","analytics","russia"):
        reserves[theme] = [f"tg:{h}" for h in RESERVE_TG.get(theme, [])]
    return reserves

# =========================
# MAIN
# =========================
def main():
    print(f"[run] start {datetime.datetime.utcnow().isoformat()}Z")
    state = load_state()
    prune_seen(state, SEEN_TTL_HOURS)

    tz = get_tz(POST_TZ); now = datetime.datetime.now(tz)
    group = resolve_group(now); quotas = GROUP_QUOTAS[group]
    slot_key = slot_key_for_20min(now)
    print(f"[slot] {now.strftime('%Y-%m-%d %H:%M %Z')} group={group} quotas={quotas} slot_key={slot_key}")

    FEEDS = build_theme_sources()
    RESV  = build_theme_reserves()

    # Per-theme hard deadline (each theme gets a slice of the global timeout)
    theme_deadline = time.monotonic() + RUN_HARD_TIMEOUT_SEC

    # 1) Fetch primary handles (PARALLEL)
    items: list = []
    per_theme_counts = {}
    for theme in quotas.keys():
        got = fetch_theme_parallel(theme, FEEDS.get(theme, []), state, slot_key, theme_deadline)
        items.extend(got); per_theme_counts[theme] = len(got)
    if DEBUG_FEEDS:
        print(f"[fetch/primary] per theme: {per_theme_counts}, total={len(items)}")

    # 2) If supply is thin, auto-expand with reserve lists
    NEED = sum(quotas.values())
    if len(items) < NEED * 2:
        if DEBUG_FEEDS:
            print(f"[fetch/reserve] supply {len(items)} < {NEED*2}, pulling reserves")
        for theme in quotas.keys():
            got2 = fetch_theme_parallel(theme, RESV.get(theme, []), state, slot_key, theme_deadline)
            if got2:
                items.extend(got2)
                if DEBUG_FEEDS:
                    print(f"[fetch/reserve] {theme}: +{len(got2)}")

    # 3) Dedupe (light)
    before = len(items)
    items = dedupe_items(items, state)
    after = len(items)
    print(f"[dedupe] {before} -> {after}")

    # 4) Score/select with diversity (ZeroHedge-dominant for markets)
    picked = select_top_per_theme(items, quotas=quotas)
    channels_in_pick = sorted(set((it.get('channel') or it.get('domain') or 'unknown') for it in picked))
    print(f"[select] picked={len(picked)} channels={channels_in_pick}")

    if DEBUG_FEEDS:
        from collections import Counter
        c = Counter([(it.get('channel') or it.get('domain') or 'unknown') for it in items])
        print("[supply] per-channel counts:", dict(c))

    if not picked:
        print("[info] no eligible items; exit 0")
        save_state(state); return

    picked = picked[:max(1, POSTS_PER_RUN)]

    max_chars = get_instance_max_chars(MASTODON_INSTANCE)
    print(f"[mastodon] max_characters={max_chars}")

    for idx, it in enumerate(picked, start=1):
        summary = groq_summarize_item(it)
        link = (it.get("link") or "").strip()
        theme_label = THEME_LABEL_RU.get(it.get("theme", ""), "Новости")
        header = f"📰 {theme_label} — {now.strftime('%d.%m.%Y %H:%M %Z')}\n\n"
        link_line = f"\nЧитать подробнее: {link}" if link else ""

        allowed_for_summary = max_chars - len(header) - len(link_line) - 1
        core = summary.strip()
        
        if len(core) > allowed_for_summary:
            # Take a safe slice
            truncated = core[:max(0, allowed_for_summary - 1)]
        
            # Try to cut at the last sentence-ending punctuation
            end_idx = -1
            for ch in ".!?…":
                idx = truncated.rfind(ch)
                if idx > end_idx:
                    end_idx = idx
        
            if end_idx >= 40:
                # Cut at the end of the last full sentence
                core = truncated[:end_idx + 1]
            else:
                # Fall back to the last space so we don't cut a word in half
                space_idx = truncated.rfind(" ")
                if space_idx > 0:
                    core = truncated[:space_idx] + "…"
                else:
                    core = truncated + "…"

        content = header + core + link_line

        idem = sha(f"{link}|{group}|{slot_key}")

        try:
            resp = post_to_mastodon(content, visibility=VISIBILITY, idem_key=idem)
            print(f"[post] {idx}/{len(picked)} {resp.get('url') if resp else '(skipped)'} "
                  f"from {it.get('channel') or it.get('domain') or 'unknown'}")
        except Exception as e:
            print("[warn] posting failed:", e)

        time.sleep(POST_SLEEP_SEC)

    save_state(state); print("[run] done")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[fatal] unhandled exception:", e)

"""
Drama Watch SG — Data Pipeline (no Reddit)
Runs every 6 hours via GitHub Actions.
Fetches Google Trends + RSS News, scores everything, writes to Supabase.

Setup:
  pip install pytrends feedparser supabase python-dotenv requests

Environment variables (set in GitHub Actions secrets):
  SUPABASE_URL  — e.g. https://abcxyz.supabase.co
  SUPABASE_KEY  — your Supabase anon/service key
"""

import os, time, logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import feedparser
from pytrends.request import TrendReq
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# Scoring weights (Reddit removed — redistribute to Trends + News)
W_TRENDS = 0.75
W_NEWS   = 0.25

# Delay between Google Trends calls (be polite, avoid rate limiting)
TRENDS_DELAY_SEC = 5

# RSS feeds — SG news sources
RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
]

# ── SHOWS MASTER LIST ─────────────────────────────────────────────────────────
SHOWS = [
    # K-Drama
    {"term": "Queen of Tears drama",       "name": "Queen of Tears",         "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "Lovely Runner Korean drama",  "name": "Lovely Runner",          "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "Marry My Husband drama",      "name": "Marry My Husband",       "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "My Demon Korean drama",       "name": "My Demon",               "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "Doctor Slump Korean drama",   "name": "Doctor Slump",           "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "Crash Course in Romance drama","name": "Crash Course in Romance","genre": "kdrama", "plat": ["netflix"]},
    {"term": "Strong Girl Nam-soon drama",  "name": "Strong Girl Nam-soon",   "genre": "kdrama",  "plat": ["netflix"]},
    {"term": "My Love from the Star drama", "name": "My Love from the Star",  "genre": "kdrama",  "plat": ["netflix"]},
    # C-Drama
    {"term": "The Story of Rose Chinese drama","name": "The Story of Rose",   "genre": "cdrama",  "plat": ["wetv"]},
    {"term": "Blossoms Shanghai drama",     "name": "Blossoms Shanghai",      "genre": "cdrama",  "plat": ["iqiyi","wetv"]},
    {"term": "The Double Chinese drama",    "name": "The Double",             "genre": "cdrama",  "plat": ["netflix"]},
    {"term": "Go Ahead Chinese drama",      "name": "Go Ahead",               "genre": "cdrama",  "plat": ["iqiyi"]},
    {"term": "Nirvana in Fire drama",       "name": "Nirvana in Fire",        "genre": "cdrama",  "plat": ["iqiyi"]},
    {"term": "Love Like the Galaxy drama",  "name": "Love Like the Galaxy",   "genre": "cdrama",  "plat": ["wetv"]},
    # Local SG
    {"term": "Code of Law 5 Singapore drama","name": "Code of Law 5",         "genre": "local",   "plat": ["ch5","mewatch"]},
    {"term": "Little Nyonya 2 Channel 8",   "name": "Little Nyonya 2",        "genre": "local",   "plat": ["ch8","mewatch"]},
    {"term": "Kitchen Musical 2 Singapore", "name": "The Kitchen Musical 2",  "genre": "local",   "plat": ["ch8","mewatch"]},
    {"term": "Tanglin Channel 5 drama",     "name": "Tanglin",                "genre": "local",   "plat": ["ch5","mewatch"]},
    # Thai
    {"term": "Only Friends Thai drama",     "name": "Only Friends",           "genre": "thai",    "plat": ["gmmtv","wetv"]},
    {"term": "Hidden Agenda Thai drama",    "name": "Hidden Agenda",          "genre": "thai",    "plat": ["gmmtv","wetv"]},
    {"term": "2gether Series Thai drama",   "name": "2gether The Series",     "genre": "thai",    "plat": ["gmmtv","wetv"]},
    {"term": "Enchanted Thai drama GMMTV",  "name": "Enchanted",              "genre": "thai",    "plat": ["gmmtv","wetv"]},
    # Western
    {"term": "Bridgerton Season 3 Netflix", "name": "Bridgerton S3",          "genre": "western", "plat": ["netflix"]},
    {"term": "The Crown Season 6 Netflix",  "name": "The Crown S6",           "genre": "western", "plat": ["netflix"]},
    {"term": "Outlander Season 7",          "name": "Outlander S7",           "genre": "western", "plat": ["netflix"]},
    {"term": "Suits Season 10 Netflix",     "name": "Suits S10",              "genre": "western", "plat": ["netflix"]},
    # Others
    {"term": "Dirilis Ertugrul Turkish drama","name": "Dirilis: Ertugrul",    "genre": "others",  "plat": ["netflix","youtube"]},
    {"term": "Scam 1992 Indian drama",      "name": "Scam 1992",              "genre": "others",  "plat": ["zee5"]},
    {"term": "Oshin Japanese drama",        "name": "Oshin",                  "genre": "others",  "plat": ["youtube"]},
    {"term": "Hana Yori Dango Japanese drama","name": "Hana Yori Dango",      "genre": "others",  "plat": ["netflix","viu"]},
]

ARTISTS = [
    {"term": "Kim Soo-hyun actor 2024",    "name": "Kim Soo-hyun",        "role": "Actor",   "show": "Queen of Tears",    "genre": "kdrama"},
    {"term": "Kim Ji-won actress 2024",    "name": "Kim Ji-won",          "role": "Actress", "show": "Queen of Tears",    "genre": "kdrama"},
    {"term": "Byeon Woo-seok actor",       "name": "Byeon Woo-seok",      "role": "Actor",   "show": "Lovely Runner",     "genre": "kdrama"},
    {"term": "Park Min-young actress",     "name": "Park Min-young",      "role": "Actress", "show": "Marry My Husband",  "genre": "kdrama"},
    {"term": "Liu Yifei actress drama",    "name": "Liu Yifei",           "role": "Actress", "show": "The Story of Rose", "genre": "cdrama"},
    {"term": "Hu Ge Chinese actor",        "name": "Hu Ge",               "role": "Actor",   "show": "Nirvana in Fire",   "genre": "cdrama"},
    {"term": "Zoe Tay Singapore actress",  "name": "Zoe Tay",             "role": "Actress", "show": "Little Nyonya 2",   "genre": "local"},
    {"term": "Tay Ping Hui actor Singapore","name": "Tay Ping Hui",       "role": "Actor",   "show": "Code of Law 5",     "genre": "local"},
    {"term": "Joss Wachirawit Thai actor", "name": "Joss Wachirawit",     "role": "Actor",   "show": "Only Friends",      "genre": "thai"},
    {"term": "Bright Vachirawit actor",    "name": "Bright Vachirawit",   "role": "Actor",   "show": "2gether The Series","genre": "thai"},
    {"term": "Nicola Coughlan actress",    "name": "Nicola Coughlan",     "role": "Actress", "show": "Bridgerton S3",     "genre": "western"},
    {"term": "Engin Altan Duzyatan actor", "name": "Engin Altan Düzyatan","role": "Actor",   "show": "Dirilis: Ertugrul", "genre": "others"},
]

# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────────

def fetch_trends_score(pytrends: TrendReq, term: str) -> float:
    """Returns a 0–100 score for a search term in Singapore over the past 7 days."""
    try:
        pytrends.build_payload([term], geo="SG", timeframe="now 7-d")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns:
            log.warning(f"  No Trends data for '{term}'")
            return 0.0
        score = float(df[term].mean())
        return round(score, 1)
    except Exception as e:
        log.warning(f"  Trends error for '{term}': {e}")
        return 0.0


def fetch_all_trends(items: list) -> dict:
    """Fetch Google Trends scores for all items. Returns {name: score}."""
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))
    scores = {}
    for item in items:
        score = fetch_trends_score(pytrends, item["term"])
        scores[item["name"]] = score
        log.info(f"  Trends [{item['name']}]: {score}")
        time.sleep(TRENDS_DELAY_SEC)
    return scores


# ── RSS / NEWS ────────────────────────────────────────────────────────────────

def fetch_all_entries() -> list:
    """Fetch all RSS entries once, reuse for all items."""
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
            log.info(f"  RSS [{url}]: {len(feed.entries)} entries")
        except Exception as e:
            log.warning(f"  RSS error [{url}]: {e}")
    return entries


def fetch_news_mentions(items: list, entries: list) -> dict:
    """Count news articles mentioning each item. Returns {name: normalised_score}."""
    scores = {}
    for item in items:
        name = item["name"].lower()
        count = sum(
            1 for e in entries
            if name in (e.get("title", "") + " " + e.get("summary", "")).lower()
        )
        scores[item["name"]] = count
        log.info(f"  News [{item['name']}]: {count} articles")

    # Normalise to 0–100
    mx = max(scores.values(), default=1) or 1
    return {k: round((v / mx) * 100, 1) for k, v in scores.items()}


# ── SCORING ───────────────────────────────────────────────────────────────────

def compute_score(trends: float, news: float) -> float:
    return round(trends * W_TRENDS + news * W_NEWS, 1)


def score_to_status(score: float, prev_score: float | None = None) -> str:
    if score >= 80: return "Viral"
    if score >= 60: return "Hot"
    if score >= 40:
        if prev_score is not None and score > prev_score + 3:
            return "Rising"
        return "Stable"
    return "Fading"


def score_to_trend(score: float, prev_score: float | None) -> str:
    if prev_score is None: return "→"
    delta = score - prev_score
    if delta > 3:  return "↑"
    if delta < -3: return "↓"
    return "→"


def build_sparkline(score: float, prev_sparkline: list) -> list:
    """
    Append today's score to historical sparkline, keep last 7 points.
    prev_sparkline = existing array from Supabase (up to 7 points).
    """
    history = (prev_sparkline or [])[-6:]  # keep last 6
    while len(history) < 6:
        history.insert(0, round(score * 0.75, 1))  # estimate missing history
    return history + [score]


# ── SUPABASE ──────────────────────────────────────────────────────────────────

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_previous(supabase: Client, table: str) -> dict:
    """Returns {name: {score, sparkline}} from last pipeline run."""
    try:
        res = supabase.table(table).select("name,score,sparkline").execute()
        return {row["name"]: row for row in (res.data or [])}
    except Exception as e:
        log.warning(f"Could not fetch previous data from '{table}': {e}")
        return {}


def upsert_shows(supabase: Client, shows: list, trends: dict, news: dict):
    prev = fetch_previous(supabase, "shows")
    rows = []
    for s in shows:
        name        = s["name"]
        t           = trends.get(name, 0)
        n           = news.get(name, 0)
        score       = compute_score(t, n)
        prev_data   = prev.get(name, {})
        prev_score  = prev_data.get("score")
        prev_spark  = prev_data.get("sparkline") or []
        rows.append({
            "name":         name,
            "genre":        s["genre"],
            "platforms":    s["plat"],
            "score":        score,
            "trends_score": t,
            "news_score":   n,
            "status":       score_to_status(score, prev_score),
            "trend":        score_to_trend(score, prev_score),
            "sparkline":    build_sparkline(score, prev_spark),
            "updated_at":   datetime.now(timezone.utc).isoformat(),
        })
    supabase.table("shows").upsert(rows, on_conflict="name").execute()
    log.info(f"Upserted {len(rows)} shows to Supabase")


def upsert_artists(supabase: Client, artists: list, trends: dict, news: dict):
    prev = fetch_previous(supabase, "artists")
    rows = []
    for a in artists:
        name        = a["name"]
        t           = trends.get(name, 0)
        n           = news.get(name, 0)
        score       = compute_score(t, n)
        prev_data   = prev.get(name, {})
        prev_score  = prev_data.get("score")
        prev_spark  = prev_data.get("sparkline") or []
        rows.append({
            "name":       name,
            "role":       a["role"],
            "show":       a["show"],
            "genre":      a["genre"],
            "score":      score,
            "trend":      score_to_trend(score, prev_score),
            "sparkline":  build_sparkline(score, prev_spark),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    supabase.table("artists").upsert(rows, on_conflict="name").execute()
    log.info(f"Upserted {len(rows)} artists to Supabase")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — Pipeline starting (Trends + News only) ===")
    supabase = get_supabase()

    # 1. Google Trends
    log.info("--- Google Trends: shows ---")
    show_trends = fetch_all_trends(SHOWS)

    log.info("--- Google Trends: artists ---")
    artist_trends = fetch_all_trends(ARTISTS)

    # 2. RSS News (fetch once, reuse)
    log.info("--- RSS News feeds ---")
    entries = fetch_all_entries()

    log.info("--- News mentions: shows ---")
    show_news = fetch_news_mentions(SHOWS, entries)

    log.info("--- News mentions: artists ---")
    artist_news = fetch_news_mentions(ARTISTS, entries)

    # 3. Score and write to Supabase
    log.info("--- Writing to Supabase ---")
    upsert_shows(supabase, SHOWS, show_trends, show_news)
    upsert_artists(supabase, ARTISTS, artist_trends, artist_news)

    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()

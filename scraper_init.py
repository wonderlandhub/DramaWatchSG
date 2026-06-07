"""
Drama Watch SG — scraper_init.py
Run ONCE via GitHub Actions to bootstrap the database.

Revised approach:
- Seed list of known 2026 SG drama shows/artists/events
- Google Trends direct scoring (reliable) — not related_queries
- TMDB fills all content — description, Chinese title, platforms
- RSS discovers any additional shows from news articles
- Fetches daily scores from 1st May to today (~30 days)
- Inserts ~30 history rows per item — real sparklines from day one
- Genre ranking derived from actual search scores

Environment variables (GitHub Actions secrets):
  SUPABASE_URL
  SUPABASE_KEY   — service_role key
  TMDB_API_KEY   — free from themoviedb.org
"""

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import feedparser
from pytrends.request import TrendReq
from supabase import create_client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TMDB_API_KEY = os.environ["TMDB_API_KEY"]
TMDB_BASE    = "https://api.themoviedb.org/3"
WIKI_API     = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKI_HEADERS = {"User-Agent": "DramaWatchSG/1.0"}
TRENDS_DELAY = 8  # seconds between Google Trends calls — be polite

# SG streaming provider mapping from TMDB provider IDs
TMDB_PROVIDER_MAP = {
    8:    "netflix",
    337:  "disney",
    96:   "iqiyi",
    422:  "wetv",
    458:  "viu",
    2018: "mewatch",
    290:  "youtube",
    167:  "gmmtv",
    232:  "zee5",
    119:  "amazon",
}

# Genre colours — assigned by rank
GENRE_COLORS = [
    {"dot_color":"#7F77DD","bg_color":"#EEEDFE","text_color":"#3C3489"},  # rank 1
    {"dot_color":"#D85A30","bg_color":"#FAECE7","text_color":"#993C1D"},  # rank 2
    {"dot_color":"#1D9E75","bg_color":"#E1F5EE","text_color":"#085041"},  # rank 3
    {"dot_color":"#BA7517","bg_color":"#FAEEDA","text_color":"#633806"},  # rank 4
    {"dot_color":"#378ADD","bg_color":"#E6F1FB","text_color":"#0C447C"},  # rank 5
    {"dot_color":"#9C27B0","bg_color":"#F3E5F5","text_color":"#4A148C"},  # others
]

GENRE_LABELS = {
    "kdrama":   "K-Drama",
    "cdrama":   "C-Drama",
    "local":    "Local",
    "thai":     "Thai",
    "western":  "Western",
    "japanese": "J-Drama",
    "turkish":  "Turkish",
    "indian":   "Indian",
    "others":   "Others",
}

# RSS feeds for additional show discovery
RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
]

# ── 2026 SEED DATA ────────────────────────────────────────────────────────
# Sources: Her World SG, Harper's Bazaar SG, Singapore Women's Weekly,
#          Star Awards 2026, Disney+ SG, Netflix SG
# search_term = what SG people search on Google for this show
# tmdb_id = TMDB TV show ID for accurate content lookup
# context = SG-specific one-liner (only we can write this)

SEED_SHOWS = [

    # ── K-DRAMA ──────────────────────────────────────────────────────────
    {
        "name":        "Perfect Crown",
        "genre":       "kdrama",
        "context":     "Byeon Woo-seok and IU — most anticipated K-Drama of 2026 in SG",
        "search_term": "Perfect Crown Korean drama 2026",
        "tmdb_id":     None,  # search by name
    },
    {
        "name":        "Tempest",
        "genre":       "kdrama",
        "context":     "Gianna Jun political thriller — SG fans waiting eagerly",
        "search_term": "Tempest Korean drama 2026 Gianna Jun",
        "tmdb_id":     None,
    },
    {
        "name":        "My Royal Nemesis",
        "genre":       "kdrama",
        "context":     "Time-travel enemies-to-lovers — trending on Netflix SG",
        "search_term": "My Royal Nemesis Korean drama Netflix 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "The Remarried Empress",
        "genre":       "kdrama",
        "context":     "Shin Min-A fantasy romance — highly anticipated in SG",
        "search_term": "The Remarried Empress Korean drama 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "Bloodhounds Season 2",
        "genre":       "kdrama",
        "context":     "Action sequel — SG fans of Season 1 waiting",
        "search_term": "Bloodhounds Season 2 Korean drama Disney Plus",
        "tmdb_id":     None,
    },
    {
        "name":        "Bad Guys Reign of Chaos",
        "genre":       "kdrama",
        "context":     "Crime action — Ma Dong-Seok on Viu SG",
        "search_term": "Bad Guys Reign of Chaos Korean drama Viu 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "Karma Korean Drama",
        "genre":       "kdrama",
        "context":     "Slow-burn thriller — Park Hae-soo gaining SG traction",
        "search_term": "Karma Korean drama 2026 Park Hae-soo",
        "tmdb_id":     None,
    },
    {
        "name":        "Queen of Tears",
        "genre":       "kdrama",
        "context":     "2024 classic — still heavily rewatched by SG fans",
        "search_term": "Queen of Tears Korean drama Netflix",
        "tmdb_id":     202431,
    },

    # ── C-DRAMA ──────────────────────────────────────────────────────────
    {
        "name":        "How Dare You",
        "genre":       "cdrama",
        "context":     "Transmigration comedy — dominating SG C-Drama discussions",
        "search_term": "How Dare You Chinese drama 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "My Page in the 90s",
        "genre":       "cdrama",
        "context":     "Chen Xingxu time-travel romance — SG fans loving the 90s nostalgia",
        "search_term": "My Page in the 90s Chinese drama 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "Shine on Me",
        "genre":       "cdrama",
        "context":     "Song Weilong and Zhao Jinmai youth romance on Netflix SG",
        "search_term": "Shine on Me Chinese drama Netflix 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "The Story of Rose",
        "genre":       "cdrama",
        "context":     "2024 classic — still actively discussed in SG",
        "search_term": "The Story of Rose Chinese drama WeTV",
        "tmdb_id":     None,
    },
    {
        "name":        "Nirvana in Fire",
        "genre":       "cdrama",
        "context":     "Timeless C-Drama classic — steady SG rewatch community",
        "search_term": "Nirvana in Fire Chinese drama iQIYI",
        "tmdb_id":     None,
    },
    {
        "name":        "Blossoms Shanghai",
        "genre":       "cdrama",
        "context":     "Wong Kar-wai masterpiece — SG fans still recommending",
        "search_term": "Blossoms Shanghai Chinese drama 繁花",
        "tmdb_id":     None,
    },

    # ── LOCAL SG ─────────────────────────────────────────────────────────
    {
        "name":        "Emerald Hill The Little Nyonya Story",
        "genre":       "local",
        "context":     "Star Awards 2026 biggest winner — 6 awards including Best Drama",
        "search_term": "Emerald Hill Little Nyonya Story Channel 8 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "Pure Vanilla",
        "genre":       "local",
        "context":     "New 2026 SG drama — gaining local viewership",
        "search_term": "Pure Vanilla Singapore drama Mediacorp 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "People Like Us",
        "genre":       "local",
        "context":     "SG community drama — relatable local stories",
        "search_term": "People Like Us Singapore drama 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "128 Circle",
        "genre":       "local",
        "context":     "Singapore first multilingual drama — hawker centre stories",
        "search_term": "128 Circle Singapore multilingual drama",
        "tmdb_id":     None,
    },

    # ── THAI ─────────────────────────────────────────────────────────────
    {
        "name":        "Hometown Romance",
        "genre":       "thai",
        "context":     "Thai countryside rom-com — SG BL fans following",
        "search_term": "Hometown Romance Thai drama 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "Moonshadow Thai Drama",
        "genre":       "thai",
        "context":     "Thai BL sequel — Emi and Bonnie returning",
        "search_term": "Moonshadow Thai drama 2026 BL",
        "tmdb_id":     None,
    },
    {
        "name":        "Only Friends",
        "genre":       "thai",
        "context":     "GMMTV BL — still active SG fan community",
        "search_term": "Only Friends Thai drama GMMTV",
        "tmdb_id":     None,
    },
    {
        "name":        "Hidden Agenda Thai Drama",
        "genre":       "thai",
        "context":     "Thai BL — word-of-mouth growing in SG",
        "search_term": "Hidden Agenda Thai drama GMMTV 2024",
        "tmdb_id":     None,
    },

    # ── WESTERN ──────────────────────────────────────────────────────────
    {
        "name":        "Bridgerton Season 4",
        "genre":       "western",
        "context":     "Benedict Bridgerton arc — SG fans eagerly waiting",
        "search_term": "Bridgerton Season 4 Netflix 2026",
        "tmdb_id":     92783,
    },
    {
        "name":        "Suits LA",
        "genre":       "western",
        "context":     "Suits spinoff — SG legal drama fans following",
        "search_term": "Suits LA Netflix 2026",
        "tmdb_id":     None,
    },
    {
        "name":        "The White Lotus Season 3",
        "genre":       "western",
        "context":     "Thailand setting — SG viewers very engaged",
        "search_term": "White Lotus Season 3 HBO 2025",
        "tmdb_id":     None,
    },

    # ── OTHERS ───────────────────────────────────────────────────────────
    {
        "name":        "Dirilis Ertugrul",
        "genre":       "others",
        "context":     "Turkish epic — loyal SG Malay community still active",
        "search_term": "Dirilis Ertugrul Turkish drama Netflix",
        "tmdb_id":     None,
    },
    {
        "name":        "Scam 1992",
        "genre":       "others",
        "context":     "Indian drama — SG viewers calling it essential viewing",
        "search_term": "Scam 1992 Indian drama ZEE5",
        "tmdb_id":     None,
    },
    {
        "name":        "Oshin",
        "genre":       "others",
        "context":     "J-Drama classic — nostalgia wave among older SG viewers",
        "search_term": "Oshin Japanese drama NHK",
        "tmdb_id":     None,
    },
]

# ── 2026 SEED ARTISTS ─────────────────────────────────────────────────────
# Top searched drama artists in SG 2026
# search_term = what SG people actually search
SEED_ARTISTS = [
    # K-Drama
    {"name":"Byeon Woo-seok",    "show":"Perfect Crown",         "genre":"kdrama",  "search_term":"Byeon Woo-seok actor 2026"},
    {"name":"IU",                "show":"Perfect Crown",         "genre":"kdrama",  "search_term":"IU actress Korean drama 2026"},
    {"name":"Gianna Jun",        "show":"Tempest",               "genre":"kdrama",  "search_term":"Gianna Jun actress Tempest"},
    {"name":"Shin Min-A",        "show":"The Remarried Empress",  "genre":"kdrama",  "search_term":"Shin Min-A actress 2026"},
    {"name":"Song Hye-kyo",      "show":"Various",               "genre":"kdrama",  "search_term":"Song Hye-kyo actress 2026"},
    {"name":"Gong Yoo",          "show":"Various",               "genre":"kdrama",  "search_term":"Gong Yoo actor 2026"},
    {"name":"Kim Soo-hyun",      "show":"Queen of Tears",        "genre":"kdrama",  "search_term":"Kim Soo-hyun actor"},
    {"name":"Park Hae-soo",      "show":"Karma Korean Drama",    "genre":"kdrama",  "search_term":"Park Hae-soo actor Karma"},
    # C-Drama
    {"name":"Cheng Lei",         "show":"How Dare You",          "genre":"cdrama",  "search_term":"Cheng Lei Chinese actor How Dare You"},
    {"name":"Zhang Linghe",      "show":"Various",               "genre":"cdrama",  "search_term":"Zhang Linghe Chinese actor 2026"},
    {"name":"Chen Xingxu",       "show":"My Page in the 90s",    "genre":"cdrama",  "search_term":"Chen Xingxu Chinese actor 2026"},
    {"name":"Wang Churan",       "show":"How Dare You",          "genre":"cdrama",  "search_term":"Wang Churan Chinese actress 2026"},
    {"name":"Liu Yifei",         "show":"The Story of Rose",     "genre":"cdrama",  "search_term":"Liu Yifei actress drama"},
    {"name":"Hu Ge",             "show":"Nirvana in Fire",       "genre":"cdrama",  "search_term":"Hu Ge Chinese actor"},
    # Local SG
    {"name":"Carrie Wong",       "show":"Emerald Hill The Little Nyonya Story", "genre":"local", "search_term":"Carrie Wong Singapore actress 2026"},
    {"name":"Zoe Tay",           "show":"Emerald Hill The Little Nyonya Story", "genre":"local", "search_term":"Zoe Tay Singapore actress"},
    {"name":"Desmond Tan",       "show":"Various",               "genre":"local",   "search_term":"Desmond Tan Singapore actor"},
    {"name":"Tay Ping Hui",      "show":"Various",               "genre":"local",   "search_term":"Tay Ping Hui Singapore actor"},
    # Thai
    {"name":"Joss Wachirawit",   "show":"Only Friends",          "genre":"thai",    "search_term":"Joss Wachirawit Thai actor"},
    {"name":"Bright Vachirawit", "show":"Various",               "genre":"thai",    "search_term":"Bright Vachirawit Thai actor 2026"},
    # Western
    {"name":"Nicola Coughlan",   "show":"Bridgerton Season 4",   "genre":"western", "search_term":"Nicola Coughlan actress Bridgerton"},
    {"name":"Jonathan Bailey",   "show":"Bridgerton Season 4",   "genre":"western", "search_term":"Jonathan Bailey actor Bridgerton"},
    # Others
    {"name":"Engin Altan",       "show":"Dirilis Ertugrul",      "genre":"others",  "search_term":"Engin Altan Duzyatan Turkish actor"},
    {"name":"Pratik Gandhi",     "show":"Scam 1992",             "genre":"others",  "search_term":"Pratik Gandhi Indian actor Scam 1992"},
]

# ── 2026 SEED EVENTS ──────────────────────────────────────────────────────
# Known SG drama events in 2026
# Google Trends will score these directly
SEED_EVENTS = [
    {
        "title":       "Star Awards 2026 Singapore",
        "genre":       "local",
        "type":        "Awards",
        "venue":       "MES Theatre Mediacorp",
        "event_date":  "Apr 19 2026",
        "description": "Annual Star Awards ceremony celebrating excellence in Singapore TV drama. Emerald Hill won 6 awards including Best Drama.",
        "search_term": "Star Awards 2026 Singapore",
        "links":       [{"l":"Mediacorp","u":"https://www.mediacorp.sg"}],
    },
    {
        "title":       "K-Drama fan meet Singapore 2026",
        "genre":       "kdrama",
        "type":        "Fan Meet",
        "venue":       "Singapore",
        "event_date":  "2026",
        "description": "K-Drama fan meet events in Singapore 2026 — cast appearances and fan gatherings.",
        "search_term": "K-Drama fan meet Singapore 2026",
        "links":       [{"l":"SISTIC","u":"https://www.sistic.com.sg"}],
    },
    {
        "title":       "Netflix drama event Singapore",
        "genre":       "kdrama",
        "type":        "Pop-Up",
        "venue":       "Singapore",
        "event_date":  "2026",
        "description": "Netflix drama activations and pop-up events in Singapore 2026.",
        "search_term": "Netflix drama event Singapore 2026",
        "links":       [{"l":"Netflix SG","u":"https://www.netflix.com/sg"}],
    },
    {
        "title":       "GMMTV fan event Singapore 2026",
        "genre":       "thai",
        "type":        "Fan Event",
        "venue":       "Singapore",
        "event_date":  "2026",
        "description": "GMMTV Thai drama fan events and gatherings in Singapore 2026.",
        "search_term": "GMMTV fan event Singapore 2026",
        "links":       [{"l":"GMMTV","u":"https://www.youtube.com/@GMMTV"}],
    },
]

# ── TMDB HELPERS ──────────────────────────────────────────────────────────

def tmdb_search_show(name: str, tmdb_id: int = None) -> dict:
    """Fetch show details from TMDB. Use tmdb_id if provided, else search by name."""
    try:
        if tmdb_id:
            url = f"{TMDB_BASE}/tv/{tmdb_id}"
            params = {
                "api_key": TMDB_API_KEY,
                "language": "en-SG",
                "append_to_response": "watch/providers,alternative_titles",
            }
            res = requests.get(url, params=params, timeout=10)
            if not res.ok:
                return {}
            d = res.json()
        else:
            # Search by name
            url = f"{TMDB_BASE}/search/tv"
            params = {"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"}
            res = requests.get(url, params=params, timeout=10)
            if not res.ok:
                return {}
            results = res.json().get("results", [])
            if not results:
                return {}
            found_id = results[0]["id"]
            # Get full details
            detail_url = f"{TMDB_BASE}/tv/{found_id}"
            detail_params = {
                "api_key": TMDB_API_KEY,
                "language": "en-SG",
                "append_to_response": "watch/providers,alternative_titles",
            }
            detail_res = requests.get(detail_url, params=detail_params, timeout=10)
            if not detail_res.ok:
                return {"tmdb_id": found_id, "description": results[0].get("overview", "")}
            d = detail_res.json()

        # SG streaming platforms
        sg = d.get("watch/providers", {}).get("results", {}).get("SG", {})
        platforms = []
        for p in sg.get("flatrate", []):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms:
                platforms.append(code)

        # Chinese/Korean original title
        alt_titles = d.get("alternative_titles", {}).get("results", [])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN", "TW", "HK", "KR"]:
                chinese_title = t.get("title")
                break

        # is_new from last air date
        last_air = d.get("last_air_date", "")
        status   = d.get("status", "")
        is_new   = status in ["Returning Series", "In Production"]
        if last_air:
            try:
                days_ago = (datetime.now() - datetime.strptime(last_air, "%Y-%m-%d")).days
                is_new = days_ago <= 14
            except Exception:
                pass

        return {
            "tmdb_id":       d.get("id"),
            "description":   d.get("overview", ""),
            "chinese_title": chinese_title,
            "platforms":     platforms,
            "is_new":        is_new,
        }
    except Exception as e:
        log.warning(f"TMDB show error for '{name}': {e}")
        return {}


def tmdb_search_person(name: str) -> dict:
    """Search TMDB for an artist."""
    try:
        url = f"{TMDB_BASE}/search/person"
        params = {"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"}
        res = requests.get(url, params=params, timeout=10)
        if not res.ok:
            return {}
        results = res.json().get("results", [])
        if not results:
            return {}
        person = results[0]
        known_for = person.get("known_for", [])
        show_name = ""
        if known_for:
            show_name = known_for[0].get("name") or known_for[0].get("title", "")
        role = "Actress" if person.get("gender") == 1 else "Actor"
        return {
            "tmdb_id":  person.get("id"),
            "role":     role,
            "show_name": show_name,
        }
    except Exception as e:
        log.warning(f"TMDB person error for '{name}': {e}")
        return {}


def wiki_lookup(name: str) -> dict:
    """Wikipedia fallback for shows not found on TMDB."""
    try:
        for suffix in ["", "_TV_series", "_drama"]:
            wiki_name = name.replace(" ", "_") + suffix
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc = res.json().get("extract", "")
                sentences = desc.split(". ")
                return {
                    "description": ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
                }
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error for '{name}': {e}")
        return {}

# ── RSS HELPERS ───────────────────────────────────────────────────────────

def fetch_rss_entries() -> list:
    """Fetch all RSS entries from SG news sources."""
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
            log.info(f"  RSS: {len(feed.entries)} entries from {url}")
        except Exception as e:
            log.warning(f"  RSS error [{url}]: {e}")
    return entries


def discover_shows_from_rss(entries: list, known_names: set, genre_map: dict) -> list:
    """
    Scan RSS articles for drama show mentions not yet in seed list.
    Returns list of new show dicts to insert.
    """
    drama_keywords = ["drama","series","kdrama","k-drama","c-drama","cdrama",
                      "netflix","disney+","viu","wetv","iqiyi","mediacorp",
                      "channel 8","channel 5","mewatch"]
    sg_keywords    = ["singapore","sg","singaporean","local"]
    new_shows = []

    for entry in entries:
        title   = entry.get("title", "").strip()
        summary = entry.get("summary", "").lower()
        text    = (title + " " + summary).lower()

        # Must mention drama AND Singapore
        if not any(kw in text for kw in drama_keywords): continue
        if not any(kw in text for kw in sg_keywords):    continue
        if title.lower() in known_names:                 continue
        if len(title) > 100:                             continue  # too long — generic

        # Determine genre from content
        if any(w in text for w in ["korean drama","k-drama","kdrama"]):
            genre = "kdrama"
        elif any(w in text for w in ["chinese drama","c-drama","cdrama"]):
            genre = "cdrama"
        elif any(w in text for w in ["thai drama","thailand drama"]):
            genre = "thai"
        elif any(w in text for w in ["singapore drama","local drama","channel 8","channel 5","mediacorp"]):
            genre = "local"
        else:
            genre = "others"

        genre_id = genre_map.get(genre) or genre_map.get("others")

        new_shows.append({
            "name":          title,
            "genre":         genre,
            "genre_id":      genre_id,
            "context":       "Discovered from SG news",
            "search_term":   title,
            "description":   "",
            "chinese_title": None,
            "platforms":     [],
            "is_new":        False,
            "has_description": False,
        })
        known_names.add(title.lower())
        log.info(f"  RSS discovery: '{title}' ({genre})")

    return new_shows

# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

def fetch_daily_scores(pytrends, term: str, name: str) -> list:
    """
    Fetch daily Google Trends scores from 1st May to today.
    Returns list of {date, score} dicts.
    """
    try:
        pytrends.build_payload([term], geo="SG", timeframe="today 1-m")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns:
            log.warning(f"  No Trends data for '{name}'")
            return []
        results = [
            {
                "date":  ts.to_pydatetime().replace(tzinfo=timezone.utc),
                "score": float(df.loc[ts, term]),
            }
            for ts in df.index
        ]
        log.info(f"  Trends [{name}]: {len(results)} days, max={max(r['score'] for r in results):.1f}")
        return results
    except Exception as e:
        log.warning(f"  Trends error for '{name}': {e}")
        return []

# ── SCORING HELPERS ───────────────────────────────────────────────────────

def normalise_daily(daily_scores: list) -> list:
    """Normalise daily scores to 0-100 within the item's own history."""
    if not daily_scores:
        return daily_scores
    mx = max(s["score"] for s in daily_scores) or 1
    for s in daily_scores:
        s["normalised"] = round((s["score"] / mx) * 100, 1)
    return daily_scores


def score_to_status(score: float, prev: float = None) -> str:
    if score >= 80: return "Viral"
    if score >= 60: return "Hot"
    if score >= 40:
        if prev and score > prev + 3: return "Rising"
        return "Stable"
    return "Fading"


def score_to_trend(score: float, prev: float = None) -> str:
    if prev is None: return "→"
    if score > prev + 5: return "↑"
    if score < prev - 5: return "↓"
    return "→"


def build_sparkline(scores: list) -> list:
    """Last 7 scores as sparkline array."""
    return [round(s, 1) for s in scores[-7:]]

# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — INIT scraper v2 ===")
    log.info(f"    {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M SGT')}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # ── PHASE 1: Fetch Google Trends scores for seed shows ────────────────
    log.info("--- Phase 1: Fetching Google Trends scores for seed shows ---")
    show_scores    = {}  # name → [{date, score, normalised}]
    artist_scores  = {}
    event_scores   = {}
    genre_totals   = {}  # genre_code → total score

    for s in SEED_SHOWS:
        daily = fetch_daily_scores(pytrends, s["search_term"], s["name"])
        if daily:
            daily = normalise_daily(daily)
            show_scores[s["name"]] = daily
            total = sum(d["score"] for d in daily)
            genre_totals[s["genre"]] = genre_totals.get(s["genre"], 0) + total
        time.sleep(TRENDS_DELAY)

    log.info(f"  Shows with Trends data: {len(show_scores)}/{len(SEED_SHOWS)}")

    # ── PHASE 2: Fetch scores for seed artists ────────────────────────────
    log.info("--- Phase 2: Fetching Google Trends scores for seed artists ---")
    for a in SEED_ARTISTS:
        daily = fetch_daily_scores(pytrends, a["search_term"], a["name"])
        if daily:
            daily = normalise_daily(daily)
            artist_scores[a["name"]] = daily
        time.sleep(TRENDS_DELAY)

    log.info(f"  Artists with Trends data: {len(artist_scores)}/{len(SEED_ARTISTS)}")

    # ── PHASE 3: Fetch scores for seed events ────────────────────────────
    log.info("--- Phase 3: Fetching Google Trends scores for seed events ---")
    for e in SEED_EVENTS:
        daily = fetch_daily_scores(pytrends, e["search_term"], e["title"])
        if daily:
            daily = normalise_daily(daily)
            event_scores[e["title"]] = daily
        time.sleep(TRENDS_DELAY)

    log.info(f"  Events with Trends data: {len(event_scores)}/{len(SEED_EVENTS)}")

    # ── PHASE 4: Genre ranking from real scores ───────────────────────────
    log.info("--- Phase 4: Ranking genres from real search scores ---")
    sorted_genres = sorted(genre_totals.items(), key=lambda x: x[1], reverse=True)
    top5 = [g[0] for g in sorted_genres[:5]]
    log.info(f"  Top 5 genres by SG search volume: {top5}")

    # Insert genres — always ensure "others" exists
    for i, (code, total) in enumerate(sorted_genres):
        is_top5 = i < 5
        rank    = i + 1 if is_top5 else 99
        colors  = GENRE_COLORS[i] if i < 5 else GENRE_COLORS[5]
        sb.table("genres").upsert({
            "code":       code,
            "label":      GENRE_LABELS.get(code, code.title()),
            "dot_color":  colors["dot_color"],
            "bg_color":   colors["bg_color"],
            "text_color": colors["text_color"],
            "is_top5":    is_top5,
            "rank":       rank,
            "is_active":  True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="code").execute()
        log.info(f"  Genre {rank}: {code} (total score {total:.0f}) is_top5={is_top5}")

    # Always ensure Others exists
    others_colors = GENRE_COLORS[5]
    sb.table("genres").upsert({
        "code": "others", "label": "Others",
        "dot_color": others_colors["dot_color"],
        "bg_color":  others_colors["bg_color"],
        "text_color":others_colors["text_color"],
        "is_top5": False, "rank": 99,
        "is_active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="code").execute()

    # Build genre_id lookup
    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}
    log.info(f"  Genre map: {genre_map}")

    # ── PHASE 5: TMDB lookup + insert shows_master ────────────────────────
    log.info("--- Phase 5: TMDB lookup and inserting shows_master ---")
    show_id_map = {}  # name → id

    for s in SEED_SHOWS:
        if s["name"] not in show_scores:
            log.warning(f"  Skipping {s['name']} — no Trends data")
            continue

        # TMDB lookup
        tmdb = tmdb_search_show(s["name"], s.get("tmdb_id"))

        # Wikipedia fallback if no description
        if not tmdb.get("description"):
            log.info(f"  No TMDB description for {s['name']} — trying Wikipedia")
            wiki = wiki_lookup(s["name"])
            if wiki.get("description"):
                tmdb["description"] = wiki["description"]

        genre_id = genre_map.get(s["genre"]) or genre_map.get("others")
        has_desc = bool(tmdb.get("description", "").strip())

        row = {
            "name":          s["name"],
            "chinese_title": tmdb.get("chinese_title"),
            "genre_id":      genre_id,
            "platforms":     tmdb.get("platforms", []),
            "description":   tmdb.get("description", ""),
            "search_term":   s["search_term"],
            "tmdb_id":       tmdb.get("tmdb_id") or s.get("tmdb_id"),
            "has_description": has_desc,
            "is_active":     True,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("shows_master").upsert(row, on_conflict="name").execute()
        if res.data:
            show_id_map[s["name"]] = res.data[0]["id"]
            log.info(f"  ✅ Show: {s['name']} (genre={s['genre']}, has_desc={has_desc}, platforms={tmdb.get('platforms',[])})")
        time.sleep(0.3)

    # ── PHASE 6: RSS discovery — additional shows ─────────────────────────
    log.info("--- Phase 6: RSS discovery for additional shows ---")
    rss_entries = fetch_rss_entries()
    known_names = {s["name"].lower() for s in SEED_SHOWS}
    rss_shows   = discover_shows_from_rss(rss_entries, known_names, genre_map)

    for s in rss_shows:
        # TMDB lookup for RSS-discovered shows
        tmdb = tmdb_search_show(s["name"])
        if tmdb:
            s["description"]   = tmdb.get("description", "")
            s["chinese_title"] = tmdb.get("chinese_title")
            s["platforms"]     = tmdb.get("platforms", [])
            s["has_description"] = bool(s["description"].strip())

        row = {
            "name":          s["name"],
            "chinese_title": s.get("chinese_title"),
            "genre_id":      s["genre_id"],
            "platforms":     s.get("platforms", []),
            "description":   s.get("description", ""),
            "search_term":   s["search_term"],
            "has_description": s.get("has_description", False),
            "is_active":     True,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        try:
            res = sb.table("shows_master").upsert(row, on_conflict="name").execute()
            if res.data:
                # Also fetch 30-day Trends for RSS-discovered shows
                daily = fetch_daily_scores(pytrends, s["search_term"], s["name"])
                if daily:
                    daily = normalise_daily(daily)
                    show_scores[s["name"]] = daily
                    show_id_map[s["name"]] = res.data[0]["id"]
                time.sleep(TRENDS_DELAY)
        except Exception as e:
            log.warning(f"  RSS show insert error: {e}")

    # ── PHASE 7: TMDB lookup + insert artists_master ──────────────────────
    log.info("--- Phase 7: Inserting artists_master ---")
    artist_id_map = {}

    for a in SEED_ARTISTS:
        if a["name"] not in artist_scores:
            log.warning(f"  Skipping {a['name']} — no Trends data")
            continue

        tmdb = tmdb_search_person(a["name"])
        genre_id = genre_map.get(a["genre"]) or genre_map.get("others")

        # Find linked show_id
        show_id = None
        for sname, sid in show_id_map.items():
            if a["show"].lower() in sname.lower():
                show_id = sid
                break

        row = {
            "name":           a["name"],
            "role":           tmdb.get("role", "Actor"),
            "show_name":      a["show"],
            "genre_id":       genre_id,
            "search_term":    a["search_term"],
            "tmdb_id":        tmdb.get("tmdb_id"),
            "has_description": bool(tmdb.get("role") and a["show"]),
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("artists_master").upsert(row, on_conflict="name").execute()
        if res.data:
            artist_id_map[a["name"]] = res.data[0]["id"]
            log.info(f"  ✅ Artist: {a['name']} (role={tmdb.get('role','Actor')}, show={a['show']})")
        time.sleep(0.3)

    # ── PHASE 8: Insert events_master ─────────────────────────────────────
    log.info("--- Phase 8: Inserting events_master ---")
    event_id_map = {}

    for e in SEED_EVENTS:
        genre_id = genre_map.get(e["genre"]) or genre_map.get("others")
        has_desc = bool(e.get("description", "").strip())

        row = {
            "title":          e["title"],
            "genre_id":       genre_id,
            "type":           e["type"],
            "venue":          e.get("venue", ""),
            "event_date":     e.get("event_date", ""),
            "description":    e.get("description", ""),
            "links":          json.dumps(e.get("links", [])),
            "search_term":    e["search_term"],
            "has_description": has_desc,
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("events_master").upsert(row, on_conflict="title").execute()
        if res.data:
            event_id_map[e["title"]] = res.data[0]["id"]
            log.info(f"  ✅ Event: {e['title']}")
        time.sleep(0.2)

    # ── PHASE 9: Insert history rows ──────────────────────────────────────
    log.info("--- Phase 9: Inserting history rows ---")

    # Shows history
    log.info("  Inserting shows_history...")
    for name, show_id in show_id_map.items():
        daily = show_scores.get(name, [])
        if not daily:
            continue
        scores_so_far, rows, prev = [], [], None
        for d in daily:
            score = d["normalised"]
            scores_so_far.append(score)
            rows.append({
                "show_id":      show_id,
                "score":        score,
                "trends_score": round(d["score"], 1),
                "status":       score_to_status(score, prev),
                "trend":        score_to_trend(score, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  d["date"].isoformat(),
            })
            prev = score
        sb.table("shows_history").insert(rows).execute()
        log.info(f"    {name}: {len(rows)} history rows")

    # Artists history
    log.info("  Inserting artists_history...")
    for name, artist_id in artist_id_map.items():
        daily = artist_scores.get(name, [])
        if not daily:
            continue
        scores_so_far, rows, prev = [], [], None
        for d in daily:
            score = d["normalised"]
            scores_so_far.append(score)
            rows.append({
                "artist_id":    artist_id,
                "score":        score,
                "trends_score": round(d["score"], 1),
                "status":       score_to_status(score, prev),
                "trend":        score_to_trend(score, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  d["date"].isoformat(),
            })
            prev = score
        sb.table("artists_history").insert(rows).execute()
        log.info(f"    {name}: {len(rows)} history rows")

    # Events history
    log.info("  Inserting events_history...")
    for title, event_id in event_id_map.items():
        daily = event_scores.get(title, [])
        if not daily:
            continue
        scores_so_far, rows, prev = [], [], None
        for d in daily:
            score = d["normalised"]
            scores_so_far.append(score)
            rows.append({
                "event_id":     event_id,
                "score":        score,
                "trends_score": round(d["score"], 1),
                "status":       "Upcoming",
                "trend":        score_to_trend(score, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  d["date"].isoformat(),
            })
            prev = score
        sb.table("events_history").insert(rows).execute()
        log.info(f"    {title[:40]}: {len(rows)} history rows")

    # ── DONE ──────────────────────────────────────────────────────────────
    log.info("=== INIT complete ===")
    log.info(f"  Shows:   {len(show_id_map)}")
    log.info(f"  Artists: {len(artist_id_map)}")
    log.info(f"  Events:  {len(event_id_map)}")
    log.info(f"  Top 5 genres: {top5}")
    log.info("  Database bootstrapped with real Google Trends data from 1st May")


if __name__ == "__main__":
    main()

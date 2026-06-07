"""
Drama Watch SG — scraper_init.py
Run ONCE via GitHub Actions to bootstrap the database.

Flow:
  1. Fetch Google Trends related queries for all discovery terms
     geo=SG, timeframe=today 1-m (1st May → today)
  2. Classify results into shows / artists / events
  3. TMDB fills show/artist content
  4. Wikipedia fallback for shows not on TMDB
  5. RSS fills event details
  6. Rank genres by search volume → top 5 get own tab
  7. Insert into master tables
  8. Fetch 30-day daily scores per item
  9. Normalise 0-100 per day
 10. Insert history rows

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
TRENDS_DELAY = 6  # seconds between Google Trends calls

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

# Genre colour config — assigned dynamically based on rank
GENRE_COLORS = [
    {"dot_color":"#7F77DD","bg_color":"#EEEDFE","text_color":"#3C3489"},  # rank 1
    {"dot_color":"#D85A30","bg_color":"#FAECE7","text_color":"#993C1D"},  # rank 2
    {"dot_color":"#1D9E75","bg_color":"#E1F5EE","text_color":"#085041"},  # rank 3
    {"dot_color":"#BA7517","bg_color":"#FAEEDA","text_color":"#633806"},  # rank 4
    {"dot_color":"#378ADD","bg_color":"#E6F1FB","text_color":"#0C447C"},  # rank 5
    {"dot_color":"#9C27B0","bg_color":"#F3E5F5","text_color":"#4A148C"},  # others
]

# RSS feeds for event detail enrichment
RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
]

# ── DISCOVERY TERMS ───────────────────────────────────────────────────────
DISCOVERY_TERMS = {
    "shows": [
        "Korean drama Singapore",
        "Chinese drama Singapore",
        "Singapore drama",
        "Thai drama Singapore",
        "Western series Singapore",
        "Japanese drama Singapore",
        "Turkish drama Singapore",
        "Indian drama Singapore",
    ],
    "artists": [
        "Korean drama artists Singapore",
        "Chinese drama artists Singapore",
        "Singapore drama artists",
        "Thai drama artists Singapore",
        "Western series artists Singapore",
        "Japanese drama artists Singapore",
        "Turkish drama artists Singapore",
        "Indian drama artists Singapore",
    ],
    "events": [
        "Korean drama event Singapore",
        "Chinese drama event Singapore",
        "Singapore drama event",
        "Thai drama event Singapore",
        "drama fan meet Singapore",
        "drama concert Singapore",
        "drama screening Singapore",
        "drama awards Singapore",
    ],
}

# Genre code mapping from discovery term
TERM_TO_GENRE = {
    "Korean drama":   "kdrama",
    "Chinese drama":  "cdrama",
    "Singapore drama":"local",
    "Thai drama":     "thai",
    "Western series": "western",
    "Japanese drama": "japanese",
    "Turkish drama":  "turkish",
    "Indian drama":   "indian",
    "drama":          "others",
}

def term_to_genre_code(term: str) -> str:
    for key, code in TERM_TO_GENRE.items():
        if key.lower() in term.lower():
            return code
    return "others"

# ── GENRE LABEL MAPPING ───────────────────────────────────────────────────
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
    "anime":    "Anime",
    "filipino": "Filipino",
}

# ── TMDB HELPERS ──────────────────────────────────────────────────────────

def tmdb_search_show(name: str) -> dict:
    """Search TMDB for a TV show. Returns enriched data or empty dict."""
    try:
        url = f"{TMDB_BASE}/search/tv"
        params = {"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"}
        res = requests.get(url, params=params, timeout=10)
        if not res.ok:
            return {}
        results = res.json().get("results", [])
        if not results:
            return {}
        show = results[0]
        tmdb_id = show["id"]

        # Get full details + SG watch providers + alternative titles
        detail_url = f"{TMDB_BASE}/tv/{tmdb_id}"
        detail_params = {
            "api_key": TMDB_API_KEY,
            "language": "en-SG",
            "append_to_response": "watch/providers,alternative_titles,credits",
        }
        detail = requests.get(detail_url, params=detail_params, timeout=10)
        if not detail.ok:
            return {"tmdb_id": tmdb_id, "description": show.get("overview", "")}
        d = detail.json()

        # SG streaming platforms
        sg = d.get("watch/providers", {}).get("results", {}).get("SG", {})
        platforms = []
        for p in sg.get("flatrate", []):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms:
                platforms.append(code)

        # Chinese/Korean/original title
        alt_titles = d.get("alternative_titles", {}).get("results", [])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN", "TW", "HK", "KR"]:
                chinese_title = t.get("title")
                break

        # Determine genre code from origin country
        origin = d.get("origin_country", [])
        if "KR" in origin:          genre_code = "kdrama"
        elif "CN" in origin or "TW" in origin: genre_code = "cdrama"
        elif "TH" in origin:        genre_code = "thai"
        elif "SG" in origin:        genre_code = "local"
        elif "JP" in origin:        genre_code = "japanese"
        elif "TR" in origin:        genre_code = "turkish"
        elif "IN" in origin:        genre_code = "indian"
        else:                       genre_code = "western"

        # is_new based on status and last air date
        status = d.get("status", "")
        last_air = d.get("last_air_date", "")
        is_new = status in ["Returning Series", "In Production"]
        if last_air:
            try:
                last_air_date = datetime.strptime(last_air, "%Y-%m-%d")
                days_ago = (datetime.now() - last_air_date).days
                is_new = days_ago <= 14
            except Exception:
                pass

        return {
            "tmdb_id":       tmdb_id,
            "description":   d.get("overview", ""),
            "chinese_title": chinese_title,
            "platforms":     platforms,
            "genre_code":    genre_code,
            "is_new":        is_new,
            "search_term":   f"{name} drama",
        }
    except Exception as e:
        log.warning(f"TMDB show search error for '{name}': {e}")
        return {}


def tmdb_search_person(name: str) -> dict:
    """Search TMDB for a person. Returns role, linked show, genre."""
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
        genre_code = "others"
        if known_for:
            show = known_for[0]
            show_name = show.get("name") or show.get("title", "")
            origin = show.get("origin_country", [])
            if isinstance(origin, list):
                if "KR" in origin:   genre_code = "kdrama"
                elif "CN" in origin: genre_code = "cdrama"
                elif "TH" in origin: genre_code = "thai"
                elif "SG" in origin: genre_code = "local"
                elif "JP" in origin: genre_code = "japanese"
                elif "TR" in origin: genre_code = "turkish"
                elif "IN" in origin: genre_code = "indian"
                else:                genre_code = "western"

        gender = person.get("gender", 0)
        role = "Actress" if gender == 1 else "Actor"

        return {
            "tmdb_id":    person.get("id"),
            "role":       role,
            "show_name":  show_name,
            "genre_code": genre_code,
            "search_term": name,
        }
    except Exception as e:
        log.warning(f"TMDB person search error for '{name}': {e}")
        return {}


def wiki_lookup(name: str) -> dict:
    """Wikipedia fallback for shows not found on TMDB."""
    try:
        wiki_name = name.replace(" ", "_")
        url = f"{WIKI_API}/{requests.utils.quote(wiki_name)}"
        res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
        if not res.ok:
            url2 = f"{WIKI_API}/{requests.utils.quote(wiki_name + '_TV_series')}"
            res = requests.get(url2, timeout=10, headers=WIKI_HEADERS)
        if not res.ok:
            return {}
        data = res.json()
        if data.get("type") == "disambiguation":
            return {}
        description = data.get("extract", "")
        sentences = description.split(". ")
        short_desc = ". ".join(sentences[:2])
        if len(sentences) > 1:
            short_desc += "."
        return {"description": short_desc}
    except Exception as e:
        log.warning(f"Wikipedia lookup error for '{name}': {e}")
        return {}

# ── RSS HELPERS ───────────────────────────────────────────────────────────

def fetch_rss_entries() -> list:
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
            log.info(f"  RSS: {len(feed.entries)} entries from {url}")
        except Exception as e:
            log.warning(f"  RSS error [{url}]: {e}")
    return entries


def enrich_event_from_rss(title: str, entries: list) -> dict:
    """Search RSS entries for event details matching the title."""
    title_lower = title.lower()
    for entry in entries:
        entry_text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
        if any(word in entry_text for word in title_lower.split() if len(word) > 4):
            # Extract event type
            ev_type = "Event"
            for t in ["fan meet","concert","screening","pop-up","awards",
                      "exhibition","festival","showcase","premiere"]:
                if t in entry_text:
                    ev_type = t.title()
                    break
            return {
                "description": entry.get("summary", "")[:500],
                "type":        ev_type,
                "link":        entry.get("link", ""),
            }
    return {}

# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

def fetch_related_queries(pytrends, term: str) -> list:
    """Fetch top related queries for a term in SG."""
    try:
        pytrends.build_payload([term], geo="SG", timeframe="today 1-m")
        related = pytrends.related_queries()
        if term not in related:
            return []
        top = related[term].get("top")
        if top is None or top.empty:
            return []
        return top["query"].tolist()[:10]
    except Exception as e:
        log.warning(f"  Related queries error for '{term}': {e}")
        return []


def fetch_daily_scores(pytrends, term: str) -> list:
    """
    Fetch daily Google Trends scores from 1st May to today.
    Returns list of {date, score} dicts.
    """
    try:
        pytrends.build_payload([term], geo="SG", timeframe="today 1-m")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns:
            return []
        return [
            {
                "date":  ts.to_pydatetime().replace(tzinfo=timezone.utc),
                "score": float(df.loc[ts, term]),
            }
            for ts in df.index
        ]
    except Exception as e:
        log.warning(f"  Daily scores error for '{term}': {e}")
        return []


def normalise_scores(items_with_scores: list, score_key: str = "score") -> list:
    """Normalise scores to 0-100 relative to max."""
    if not items_with_scores:
        return items_with_scores
    mx = max(i[score_key] for i in items_with_scores) or 1
    for item in items_with_scores:
        item["normalised"] = round((item[score_key] / mx) * 100, 1)
    return items_with_scores


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
    log.info("=== Drama Watch SG — INIT scraper starting ===")
    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # ── STEP 1: Fetch related queries for all discovery terms ─────────────
    log.info("--- Step 1: Fetching Google Trends related queries ---")
    discovered = {"shows": {}, "artists": {}, "events": {}}
    genre_scores = {}  # genre_code → total score

    for category, terms in DISCOVERY_TERMS.items():
        log.info(f"  Category: {category}")
        for term in terms:
            genre_code = term_to_genre_code(term)
            queries = fetch_related_queries(pytrends, term)
            log.info(f"    '{term}' → {len(queries)} related queries")

            for query in queries:
                if query not in discovered[category]:
                    discovered[category][query] = {
                        "genre_code": genre_code,
                        "discovery_term": term,
                    }
            time.sleep(TRENDS_DELAY)

    log.info(f"  Discovered: {len(discovered['shows'])} shows, "
             f"{len(discovered['artists'])} artists, "
             f"{len(discovered['events'])} events")

    # ── STEP 2: Fetch daily scores for all discovered items ───────────────
    log.info("--- Step 2: Fetching 30-day daily scores ---")
    show_scores_map    = {}  # name → [{date, score, normalised}]
    artist_scores_map  = {}
    event_scores_map   = {}

    for name in discovered["shows"]:
        scores = fetch_daily_scores(pytrends, name)
        if scores:
            show_scores_map[name] = scores
            genre_code = discovered["shows"][name]["genre_code"]
            total = sum(s["score"] for s in scores)
            genre_scores[genre_code] = genre_scores.get(genre_code, 0) + total
            log.info(f"  Show '{name}': {len(scores)} days")
        time.sleep(TRENDS_DELAY)

    for name in discovered["artists"]:
        scores = fetch_daily_scores(pytrends, name)
        if scores:
            artist_scores_map[name] = scores
            log.info(f"  Artist '{name}': {len(scores)} days")
        time.sleep(TRENDS_DELAY)

    for name in discovered["events"]:
        scores = fetch_daily_scores(pytrends, name)
        if scores:
            event_scores_map[name] = scores
            genre_code = discovered["events"][name]["genre_code"]
            total = sum(s["score"] for s in scores)
            genre_scores[genre_code] = genre_scores.get(genre_code, 0) + total
            log.info(f"  Event '{name}': {len(scores)} days")
        time.sleep(TRENDS_DELAY)

    # ── STEP 3: Rank genres → top 5 get own tab ───────────────────────────
    log.info("--- Step 3: Ranking genres ---")
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    top5 = [g[0] for g in sorted_genres[:5]]
    log.info(f"  Top 5 genres: {top5}")

    # Insert genres into database
    # Always ensure "others" exists
    all_genre_codes = set(g[0] for g in sorted_genres) | {"others"}
    for i, (code, score) in enumerate(sorted_genres):
        is_top5 = i < 5
        rank    = i + 1 if is_top5 else 99
        colors  = GENRE_COLORS[i] if i < 5 else GENRE_COLORS[5]
        genre_row = {
            "code":       code,
            "label":      GENRE_LABELS.get(code, code.title()),
            "dot_color":  colors["dot_color"],
            "bg_color":   colors["bg_color"],
            "text_color": colors["text_color"],
            "is_top5":    is_top5,
            "rank":       rank,
            "is_active":  True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("genres").upsert(genre_row, on_conflict="code").execute()
        log.info(f"  Genre {rank}: {code} ({score:.0f} total score) is_top5={is_top5}")

    # Always ensure Others exists
    others_colors = GENRE_COLORS[5]
    sb.table("genres").upsert({
        "code": "others", "label": "Others",
        "dot_color": others_colors["dot_color"],
        "bg_color":  others_colors["bg_color"],
        "text_color":others_colors["text_color"],
        "is_top5":   False, "rank": 99,
        "is_active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="code").execute()

    # Build genre_id lookup
    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}

    # ── STEP 4: TMDB / Wikipedia lookup + insert master tables ───────────
    log.info("--- Step 4: Enriching and inserting master tables ---")

    # Fetch RSS entries once for event enrichment
    rss_entries = fetch_rss_entries()

    # Shows
    show_id_map = {}  # name → id
    for name, info in discovered["shows"].items():
        if name not in show_scores_map:
            continue  # skip if no Trends data
        genre_code = info["genre_code"]

        # TMDB lookup
        tmdb = tmdb_search_show(name)
        if tmdb:
            genre_code = tmdb.get("genre_code", genre_code)

        # Wikipedia fallback if no TMDB description
        if not tmdb.get("description"):
            wiki = wiki_lookup(name)
            tmdb["description"] = wiki.get("description", "")

        genre_id = genre_map.get(genre_code) or genre_map.get("others")
        has_desc = bool(tmdb.get("description", "").strip())

        row = {
            "name":          name,
            "chinese_title": tmdb.get("chinese_title"),
            "genre_id":      genre_id,
            "platforms":     tmdb.get("platforms", []),
            "description":   tmdb.get("description", ""),
            "search_term":   tmdb.get("search_term", name),
            "tmdb_id":       tmdb.get("tmdb_id"),
            "has_description": has_desc,
            "is_active":     True,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("shows_master").upsert(row, on_conflict="name").execute()
        if res.data:
            show_id_map[name] = res.data[0]["id"]
            log.info(f"  Show inserted: {name} (genre={genre_code}, has_desc={has_desc})")
        time.sleep(0.3)

    # Artists
    artist_id_map = {}
    for name, info in discovered["artists"].items():
        if name not in artist_scores_map:
            continue
        tmdb = tmdb_search_person(name)
        genre_code = tmdb.get("genre_code", info["genre_code"])
        genre_id = genre_map.get(genre_code) or genre_map.get("others")

        row = {
            "name":           name,
            "role":           tmdb.get("role", "Actor"),
            "show_name":      tmdb.get("show_name", ""),
            "genre_id":       genre_id,
            "search_term":    name,
            "tmdb_id":        tmdb.get("tmdb_id"),
            "has_description": bool(tmdb.get("role") and tmdb.get("show_name")),
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("artists_master").upsert(row, on_conflict="name").execute()
        if res.data:
            artist_id_map[name] = res.data[0]["id"]
            log.info(f"  Artist inserted: {name} (role={tmdb.get('role')}, show={tmdb.get('show_name')})")
        time.sleep(0.3)

    # Events
    event_id_map = {}
    for name, info in discovered["events"].items():
        if name not in event_scores_map:
            continue
        genre_code = info["genre_code"]
        genre_id = genre_map.get(genre_code) or genre_map.get("others")

        # RSS enrichment
        rss = enrich_event_from_rss(name, rss_entries)
        has_desc = bool(rss.get("description", "").strip())
        links = []
        if rss.get("link"):
            links = [{"l": "More info", "u": rss["link"]}]

        row = {
            "title":          name,
            "genre_id":       genre_id,
            "type":           rss.get("type", "Event"),
            "venue":          "",
            "event_date":     "",
            "description":    rss.get("description", ""),
            "links":          json.dumps(links),
            "search_term":    name,
            "has_description": has_desc,
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("events_master").upsert(row, on_conflict="title").execute()
        if res.data:
            event_id_map[name] = res.data[0]["id"]
            log.info(f"  Event inserted: {name[:50]} (has_desc={has_desc})")
        time.sleep(0.3)

    # ── STEPS 5-6: Normalise scores and insert history rows ───────────────
    log.info("--- Steps 5-6: Normalising and inserting history rows ---")

    # Shows history
    log.info("  Inserting shows_history...")
    for name, show_id in show_id_map.items():
        daily_scores = show_scores_map.get(name, [])
        if not daily_scores:
            continue

        # Normalise within this show's own history
        mx = max(s["score"] for s in daily_scores) or 1
        scores_so_far = []
        rows = []
        prev_score = None

        for day in daily_scores:
            norm = round((day["score"] / mx) * 100, 1)
            scores_so_far.append(norm)
            rows.append({
                "show_id":      show_id,
                "score":        norm,
                "trends_score": round(day["score"], 1),
                "status":       score_to_status(norm, prev_score),
                "trend":        score_to_trend(norm, prev_score),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  day["date"].isoformat(),
            })
            prev_score = norm

        sb.table("shows_history").insert(rows).execute()
        log.info(f"    {name}: {len(rows)} history rows")

    # Artists history
    log.info("  Inserting artists_history...")
    for name, artist_id in artist_id_map.items():
        daily_scores = artist_scores_map.get(name, [])
        if not daily_scores:
            continue

        mx = max(s["score"] for s in daily_scores) or 1
        scores_so_far = []
        rows = []
        prev_score = None

        for day in daily_scores:
            norm = round((day["score"] / mx) * 100, 1)
            scores_so_far.append(norm)
            rows.append({
                "artist_id":    artist_id,
                "score":        norm,
                "trends_score": round(day["score"], 1),
                "status":       score_to_status(norm, prev_score),
                "trend":        score_to_trend(norm, prev_score),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  day["date"].isoformat(),
            })
            prev_score = norm

        sb.table("artists_history").insert(rows).execute()
        log.info(f"    {name}: {len(rows)} history rows")

    # Events history
    log.info("  Inserting events_history...")
    for name, event_id in event_id_map.items():
        daily_scores = event_scores_map.get(name, [])
        if not daily_scores:
            continue

        mx = max(s["score"] for s in daily_scores) or 1
        scores_so_far = []
        rows = []
        prev_score = None

        for day in daily_scores:
            norm = round((day["score"] / mx) * 100, 1)
            scores_so_far.append(norm)
            rows.append({
                "event_id":     event_id,
                "score":        norm,
                "trends_score": round(day["score"], 1),
                "status":       "Upcoming",
                "trend":        score_to_trend(norm, prev_score),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  day["date"].isoformat(),
            })
            prev_score = norm

        sb.table("events_history").insert(rows).execute()
        log.info(f"    {name[:50]}: {len(rows)} history rows")

    # ── DONE ──────────────────────────────────────────────────────────────
    log.info("=== INIT complete ===")
    log.info(f"  Shows:   {len(show_id_map)}")
    log.info(f"  Artists: {len(artist_id_map)}")
    log.info(f"  Events:  {len(event_id_map)}")
    log.info(f"  Genres:  {len(genre_map)} (top 5: {top5})")
    log.info("  Database bootstrapped with real Google Trends data from 1st May")


if __name__ == "__main__":
    main()

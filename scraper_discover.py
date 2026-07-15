"""
Drama Watch SG — scraper_discover.py

Runs daily at 11pm SGT (15:00 UTC) via GitHub Actions.

Discovery strategy — uses TMDB APIs directly (no pytrends, no 429s):
  1. TMDB Trending TV (week) — globally trending shows right now
  2. TMDB Discover — new shows by country aired in last 60 days
     Covers: KR, CN, TW, TH, SG, JP, TR, IN
  3. RSS event discovery from SG entertainment news feeds
"""

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta, date
from dotenv import load_dotenv
import feedparser
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TMDB_API_KEY = os.environ["TMDB_API_KEY"]
TMDB_BASE    = "https://api.themoviedb.org/3"
WIKI_API     = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKI_HEADERS = {"User-Agent": "DramaWatchSG/1.0"}

TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5",
    119: "amazon",
}

GENRE_PLATFORM_FALLBACK = {
    "kdrama":["viu"], "cdrama":["wetv","iqiyi"],
    "thai":["viu"],   "local":["mewatch"],
    "western":["netflix"], "others":["viu"],
    "japanese":["netflix"], "turkish":["netflix"], "indian":["amazon"],
}

# Countries to discover — mapped to genre codes
DISCOVER_COUNTRIES = {
    "KR": "kdrama",
    "CN": "cdrama",
    "TW": "cdrama",
    "TH": "thai",
    "SG": "local",
    "JP": "japanese",
    "TR": "turkish",
    "IN": "indian",
}

EVENT_TYPE_MAP = {
    "fan meet": "Fan Meet", "fan meeting": "Fan Meet", "fansign": "Fan Meet",
    "fan sign": "Fan Meet", "meet and greet": "Fan Meet", "fan party": "Fan Party",
    "concert": "Concert", "showcase": "Showcase", "pop-up": "Pop-Up",
    "pop up": "Pop-Up", "premiere": "Premiere", "screening": "Screening",
    "press conference": "Event", "media call": "Event", "brand event": "Event",
    "tour": "Concert", "appearance": "Event",
}

RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
    "https://www.asiaone.com/rss/entertainment",
    "https://www.straitstimes.com/news/life/rss.xml",
    "https://www.soompi.com/feed",
]


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_sgt() -> str:
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%Y-%m-%d %H:%M SGT")


# ── TMDB DISCOVERY ────────────────────────────────────────────────────────

def tmdb_trending_shows() -> list:
    """
    Fetch globally trending TV shows this week from TMDB.
    Returns list of show dicts with id, name, origin_country.
    """
    try:
        res = requests.get(
            f"{TMDB_BASE}/trending/tv/week",
            params={"api_key": TMDB_API_KEY, "language": "en-SG"},
            timeout=10
        )
        if not res.ok: return []
        results = res.json().get("results", [])
        log.info(f"  TMDB Trending: {len(results)} shows")
        return results
    except Exception as e:
        log.warning(f"TMDB trending error: {e}"); return []


def tmdb_discover_by_country(country: str, days_back: int = 60) -> list:
    """
    Fetch recently aired shows from a specific country using TMDB Discover.
    Sorted by popularity — catches new shows as soon as they appear on TMDB.
    """
    try:
        since = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        res = requests.get(
            f"{TMDB_BASE}/discover/tv",
            params={
                "api_key":              TMDB_API_KEY,
                "language":             "en-SG",
                "with_origin_country":  country,
                "sort_by":              "popularity.desc",
                "first_air_date.gte":   since,
                "page":                 1,
            },
            timeout=10
        )
        if not res.ok: return []
        results = res.json().get("results", [])
        log.info(f"  TMDB Discover {country}: {len(results)} shows since {since}")
        return results
    except Exception as e:
        log.warning(f"TMDB discover error ({country}): {e}"); return []


def tmdb_get_detail(tmdb_id: int) -> dict:
    """Fetch full TV show detail including providers and alternative titles."""
    try:
        res = requests.get(
            f"{TMDB_BASE}/tv/{tmdb_id}",
            params={
                "api_key":              TMDB_API_KEY,
                "language":             "en-SG",
                "append_to_response":   "watch/providers,alternative_titles",
            },
            timeout=10
        )
        if not res.ok: return {}
        return res.json()
    except Exception as e:
        log.warning(f"TMDB detail error ({tmdb_id}): {e}"); return {}


def build_show_row(name: str, detail: dict, genre_map: dict,
                   fallback_genre: str = "others") -> dict | None:
    """
    Build a shows_master row from TMDB detail.
    Returns None if show fails validation (movie, no episodes, etc.)
    """
    num_seasons  = detail.get("number_of_seasons") or 0
    num_episodes = detail.get("number_of_episodes") or 0
    if num_seasons == 0 or num_episodes == 0:
        log.info(f"  ⏭  '{name}' — no TV structure (seasons={num_seasons}, eps={num_episodes})")
        return None
    if detail.get("type","").lower() == "movie":
        log.info(f"  ⏭  '{name}' — TMDB type=movie")
        return None

    # Platforms available in SG
    sg = detail.get("watch/providers",{}).get("results",{}).get("SG",{})
    platforms = []
    for p in sg.get("flatrate",[]):
        code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
        if code and code not in platforms: platforms.append(code)

    # Chinese/Korean alternative title
    alt_titles    = detail.get("alternative_titles",{}).get("results",[])
    chinese_title = None
    for t in alt_titles:
        if t.get("iso_3166_1") in ["CN","TW","HK","KR"]:
            chinese_title = t.get("title"); break

    # Genre from origin country
    origin = detail.get("origin_country",[])
    if   "KR" in origin: genre_code = "kdrama"
    elif "CN" in origin or "TW" in origin: genre_code = "cdrama"
    elif "TH" in origin: genre_code = "thai"
    elif "SG" in origin: genre_code = "local"
    elif "JP" in origin: genre_code = "japanese"
    elif "TR" in origin: genre_code = "turkish"
    elif "IN" in origin: genre_code = "indian"
    else:                genre_code = fallback_genre

    gid       = genre_map.get(genre_code) or genre_map.get("others")
    platforms = platforms or GENRE_PLATFORM_FALLBACK.get(genre_code, ["viu"])

    return {
        "name":            name,
        "chinese_title":   chinese_title,
        "genre_id":        gid,
        "platforms":       platforms,
        "description":     detail.get("overview",""),
        "search_term":     f"{name} drama",
        "tmdb_id":         detail.get("id"),
        "has_description": bool(detail.get("overview","").strip()),
        "is_active":       True,
        "updated_at":      now_utc(),
    }


# ── WIKIPEDIA FALLBACK ────────────────────────────────────────────────────

def wiki_lookup(name: str) -> str:
    try:
        wiki_name = name.replace(" ","_")
        for suffix in ["","_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name+suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc      = res.json().get("extract","")
                sentences = desc.split(". ")
                return ". ".join(sentences[:2]) + ("." if len(sentences)>1 else "")
        return ""
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}"); return ""


# ── RSS EVENT DISCOVERY ───────────────────────────────────────────────────

def fetch_rss_entries() -> list:
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
        except Exception as e:
            log.warning(f"RSS error [{url}]: {e}")
    return entries


def discover_events_from_rss(entries, artists, shows, known_events, genre_map) -> list:
    discovered = []
    names  = [(a["name"], genre_map.get(a.get("genre_id",""),"others")) for a in artists[:40]]
    names += [(s["name"], s.get("genre","others")) for s in shows[:30]]

    for entry in entries:
        title_text   = entry.get("title","")
        summary_text = entry.get("summary","")
        full_text    = (title_text+" "+summary_text).lower()
        link         = entry.get("link","")

        if "singapore" not in full_text: continue
        ev_type = None
        for kw, t in EVENT_TYPE_MAP.items():
            if kw in full_text: ev_type = t; break
        if not ev_type: continue

        for name, genre_code in names:
            if len(name) < 4 or name.lower() not in full_text: continue
            event_title = title_text[:100].strip()
            search_term = f"{name} Singapore"
            if event_title.lower() in known_events or search_term.lower() in known_events:
                continue
            log.info(f"  RSS event found: '{event_title}' (artist: {name})")
            discovered.append({
                "title":event_title,"search_term":search_term,
                "type":ev_type,"description":summary_text[:500],
                "link":link,"genre_code":genre_code,
            })
            known_events.add(event_title.lower())
            break
    return discovered


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — DISCOVER scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]:r["id"] for r in genre_rows}

    shows   = sb.table("shows_master").select("id,name,search_term,tmdb_id").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active",True).execute().data or []
    log.info(f"Active: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    # Build lookup sets — by name, search_term AND tmdb_id to avoid duplicates
    known_names  = {s["name"].lower() for s in shows}
    known_names |= {s["search_term"].lower() for s in shows if s.get("search_term")}
    known_tmdb   = {s["tmdb_id"] for s in shows if s.get("tmdb_id")}
    known_events = {e["title"].lower() for e in events}

    rss_entries = fetch_rss_entries()
    new_shows   = 0

    # ── STEP 1: TMDB Trending TV this week ───────────────────────────────
    log.info("--- Step 1: TMDB Trending TV (week) ---")
    trending = tmdb_trending_shows()

    for show in trending:
        tmdb_id    = show.get("id")
        name       = show.get("name","").strip()
        origin     = show.get("origin_country",[])
        name_lower = name.lower()

        if not name or not tmdb_id: continue
        if tmdb_id in known_tmdb:
            log.info(f"  Already known: '{name}'")
            continue
        if name_lower in known_names: continue

        # Only keep Asian + Western dramas, skip reality/talk shows
        # Check origin country is one we track
        tracked = any(c in origin for c in DISCOVER_COUNTRIES.keys())
        if not tracked:
            log.info(f"  ⏭  '{name}' — origin {origin} not tracked")
            continue

        log.info(f"  Trending candidate: '{name}' ({origin})")
        detail = tmdb_get_detail(tmdb_id)
        if not detail: continue

        row = build_show_row(name, detail, genre_map)
        if not row: continue

        if not row["description"]:
            row["description"]     = wiki_lookup(name)
            row["has_description"] = bool(row["description"])

        try:
            sb.table("shows_master").insert(row).execute()
            known_names.add(name_lower)
            known_tmdb.add(tmdb_id)
            new_shows += 1
            log.info(f"  ✅ New show added: {name} ({row.get('genre_id')})")
        except Exception as e:
            log.warning(f"  ❌ Insert failed '{name}': {e}")

        time.sleep(0.3)

    # ── STEP 2: TMDB Discover by country — new shows last 60 days ────────
    log.info("--- Step 2: TMDB Discover by country ---")

    for country, fallback_genre in DISCOVER_COUNTRIES.items():
        candidates = tmdb_discover_by_country(country, days_back=60)

        for show in candidates:
            tmdb_id    = show.get("id")
            name       = show.get("name","").strip()
            name_lower = name.lower()

            if not name or not tmdb_id: continue
            if tmdb_id in known_tmdb:
                continue
            if name_lower in known_names: continue

            log.info(f"  Discover candidate [{country}]: '{name}'")
            detail = tmdb_get_detail(tmdb_id)
            if not detail: continue

            row = build_show_row(name, detail, genre_map, fallback_genre)
            if not row: continue

            if not row["description"]:
                row["description"]     = wiki_lookup(name)
                row["has_description"] = bool(row["description"])

            try:
                sb.table("shows_master").insert(row).execute()
                known_names.add(name_lower)
                known_tmdb.add(tmdb_id)
                new_shows += 1
                log.info(f"  ✅ New show added: {name}")
            except Exception as e:
                log.warning(f"  ❌ Insert failed '{name}': {e}")

            time.sleep(0.3)

    log.info(f"--- Discovery complete: {new_shows} new shows added ---")

    # ── STEP 3: RSS event discovery ───────────────────────────────────────
    log.info("--- Step 3: Discovering events from RSS feeds ---")
    rss_shows   = sb.table("shows_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    rss_artists = sb.table("artists_master").select("id,name,genre_id").eq("is_active",True).execute().data or []

    for ev in discover_events_from_rss(rss_entries, rss_artists, rss_shows, known_events, genre_map):
        gid   = genre_map.get(ev["genre_code"]) or genre_map.get("others")
        links = [{"l":"More info","u":ev["link"]}] if ev.get("link") else []
        row   = {
            "title":           ev["title"],
            "genre_id":        gid,
            "type":            ev["type"],
            "venue":           "Singapore",
            "event_date":      "",
            "description":     ev["description"],
            "links":           json.dumps(links),
            "search_term":     ev["search_term"],
            "has_description": bool(ev["description"].strip()),
            "is_active":       True,
            "updated_at":      now_utc(),
        }
        try:
            sb.table("events_master").insert(row).execute()
            log.info(f"  ✅ RSS event added: {ev['title'][:60]}")
        except Exception as e:
            log.warning(f"  ❌ RSS event insert failed: {e}")

    log.info("=== Discovery complete ===")


if __name__ == "__main__":
    main()

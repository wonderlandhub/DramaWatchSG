"""
Drama Watch SG — scraper_discover.py

Runs daily at 11pm SGT (15:00 UTC) via GitHub Actions.
Uses Google Trends RSS feed instead of pytrends for discovery —
no API key, no rate limiting, no 429s.

RSS feed: https://trends.google.com/trending/rss?geo=SG
"""

import os, time, json, logging, requests, re
from datetime import datetime, timezone, timedelta
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

# Google Trends RSS — Singapore daily trending searches (no auth, no rate limit)
TRENDS_RSS_SG = "https://trends.google.com/trending/rss?geo=SG"

TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5",
    119: "amazon",
}

GENRE_PLATFORM_FALLBACK = {
    "kdrama":["viu"],"cdrama":["wetv","iqiyi"],
    "thai":["viu"],"local":["mewatch"],
    "western":["netflix"],"others":["viu"],
}

# Keywords to identify drama-related trending terms
DRAMA_KEYWORDS = [
    "drama", "kdrama", "k-drama", "series", "episode", "netflix",
    "viu", "iqiyi", "wetv", "mewatch", "disney+",
    "korean", "chinese", "thai", "singapore", "japanese",
    "actor", "actress", "cast", "season",
]

# Keywords to immediately discard non-drama trending terms
DISCARD_KEYWORDS = [
    "stock", "price", "weather", "score", "match", "game",
    "iphone", "samsung", "covid", "election", "minister",
    "budget", "hdb", "mrt", "airline", "flight", "hotel",
    "recipe", "food", "restaurant", "hawker", "sgd", "forex",
    "crypto", "bitcoin", "nft", "giveaway", "sale", "promo",
]

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


# ── GOOGLE TRENDS RSS ─────────────────────────────────────────────────────

def fetch_trending_sg() -> list:
    """
    Fetch Singapore daily trending searches from Google Trends RSS.
    Returns list of trending terms — no pytrends, no auth, no 429s.
    """
    try:
        res = requests.get(TRENDS_RSS_SG, timeout=15, headers=WIKI_HEADERS)
        if not res.ok:
            log.warning(f"Trends RSS error: {res.status_code}")
            return []
        feed    = feedparser.parse(res.content)
        entries = feed.entries
        terms   = []
        for entry in entries:
            title = entry.get("title","").strip()
            if title:
                terms.append(title)
                # Also grab related queries from ht:approx_traffic or description
                desc = entry.get("summary","")
                # Extract any related searches from description if present
                related = re.findall(r'<ht:related_queries>(.*?)</ht:related_queries>',
                                     desc, re.DOTALL)
                for r in related:
                    queries = re.findall(r'<ht:query>(.*?)</ht:query>', r)
                    terms.extend(queries)
        log.info(f"  Fetched {len(terms)} trending terms from Google Trends RSS")
        return terms
    except Exception as e:
        log.warning(f"Trends RSS fetch error: {e}")
        return []


def is_drama_related(term: str, known_shows: set, known_artists: set) -> bool:
    """
    Check if a trending term could be a drama show or artist.
    Uses TMDB to verify — only called for plausible candidates.
    """
    term_lower = term.lower()

    # Discard obvious non-drama terms
    if any(kw in term_lower for kw in DISCARD_KEYWORDS):
        return False

    # Accept if contains drama keywords
    if any(kw in term_lower for kw in DRAMA_KEYWORDS):
        return True

    # Accept if matches a known artist name pattern (2+ words, title case)
    words = term.split()
    if len(words) >= 2 and all(w[0].isupper() for w in words if w):
        return True

    return False


# ── TMDB ──────────────────────────────────────────────────────────────────

def is_movie_title(name: str) -> bool:
    try:
        movie_res = requests.get(f"{TMDB_BASE}/search/movie",
                                 params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"},
                                 timeout=10)
        tv_res    = requests.get(f"{TMDB_BASE}/search/tv",
                                 params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"},
                                 timeout=10)
        movie_hits = movie_res.json().get("results",[]) if movie_res.ok else []
        tv_hits    = tv_res.json().get("results",[])    if tv_res.ok    else []

        if not movie_hits: return False
        top_movie  = movie_hits[0].get("title","").lower()
        name_lower = name.lower()
        if name_lower not in top_movie and top_movie not in name_lower: return False
        if tv_hits:
            top_tv = tv_hits[0].get("name","").lower()
            if name_lower in top_tv or top_tv in name_lower: return False
        log.info(f"  ⏭  '{name}' identified as movie — skipping")
        return True
    except Exception as e:
        log.warning(f"is_movie_title error '{name}': {e}")
        return False


def tmdb_search_show(name: str) -> dict:
    try:
        res = requests.get(f"{TMDB_BASE}/search/tv",
                           params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"},
                           timeout=10)
        if not res.ok: return {}
        results = res.json().get("results",[])
        if not results: return {}
        show    = results[0]
        tmdb_id = show["id"]

        detail = requests.get(f"{TMDB_BASE}/tv/{tmdb_id}",
                              params={"api_key":TMDB_API_KEY,"language":"en-SG",
                                      "append_to_response":"watch/providers,alternative_titles"},
                              timeout=10)
        if not detail.ok:
            return {"tmdb_id":tmdb_id,"description":show.get("overview","")}

        d = detail.json()

        num_seasons  = d.get("number_of_seasons") or 0
        num_episodes = d.get("number_of_episodes") or 0
        if num_seasons == 0 or num_episodes == 0:
            log.info(f"  ⏭  '{name}' — no TV structure (seasons={num_seasons}, eps={num_episodes})")
            return {}
        if d.get("type","").lower() == "movie":
            log.info(f"  ⏭  '{name}' — TMDB type=movie")
            return {}

        sg = d.get("watch/providers",{}).get("results",{}).get("SG",{})
        platforms = []
        for p in sg.get("flatrate",[]):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms: platforms.append(code)

        alt_titles    = d.get("alternative_titles",{}).get("results",[])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN","TW","HK","KR"]:
                chinese_title = t.get("title"); break

        origin = d.get("origin_country",[])
        if   "KR" in origin: genre_code = "kdrama"
        elif "CN" in origin or "TW" in origin: genre_code = "cdrama"
        elif "TH" in origin: genre_code = "thai"
        elif "SG" in origin: genre_code = "local"
        elif "JP" in origin: genre_code = "japanese"
        elif "TR" in origin: genre_code = "turkish"
        elif "IN" in origin: genre_code = "indian"
        else:                genre_code = "western"

        last_air = d.get("last_air_date","")
        is_new   = d.get("status","") in ["Returning Series","In Production"]
        if last_air:
            try:
                days_ago = (datetime.now()-datetime.strptime(last_air,"%Y-%m-%d")).days
                is_new   = days_ago <= 14
            except: pass

        return {"tmdb_id":tmdb_id,"description":d.get("overview",""),
                "chinese_title":chinese_title,"platforms":platforms,
                "genre_code":genre_code,"is_new":is_new,"search_term":f"{name} drama"}
    except Exception as e:
        log.warning(f"TMDB show error '{name}': {e}"); return {}


def tmdb_search_person(name: str) -> dict:
    try:
        res = requests.get(f"{TMDB_BASE}/search/person",
                           params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"},
                           timeout=10)
        if not res.ok: return {}
        results = res.json().get("results",[])
        if not results: return {}
        person    = results[0]
        # Only accept if person is known for Asian dramas
        known_for = person.get("known_for",[])
        if not known_for: return {}

        show      = known_for[0]
        show_name = show.get("name") or show.get("title","")
        origin    = show.get("origin_country",[])
        genre_code = "others"
        if isinstance(origin,list):
            if   "KR" in origin: genre_code = "kdrama"
            elif "CN" in origin: genre_code = "cdrama"
            elif "TH" in origin: genre_code = "thai"
            elif "SG" in origin: genre_code = "local"
            elif "JP" in origin: genre_code = "japanese"
            elif "TR" in origin: genre_code = "turkish"
            elif "IN" in origin: genre_code = "indian"
            else:                genre_code = "western"

        # Only insert if from Asian drama origin
        if genre_code == "western":
            return {}

        role = "Actress" if person.get("gender")==1 else "Actor"
        return {"tmdb_id":person.get("id"),"role":role,
                "show_name":show_name,"genre_code":genre_code,"search_term":name}
    except Exception as e:
        log.warning(f"TMDB person error '{name}': {e}"); return {}


# ── WIKIPEDIA ─────────────────────────────────────────────────────────────

def wiki_lookup(name: str) -> dict:
    try:
        wiki_name = name.replace(" ","_")
        for suffix in ["","_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name+suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc      = res.json().get("extract","")
                sentences = desc.split(". ")
                return {"description":". ".join(sentences[:2])+("." if len(sentences)>1 else "")}
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}"); return {}


# ── RSS ───────────────────────────────────────────────────────────────────

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
            discovered.append({"title":event_title,"search_term":search_term,
                                "type":ev_type,"description":summary_text[:500],
                                "link":link,"genre_code":genre_code})
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

    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active",True).execute().data or []
    log.info(f"Active: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    known_shows   = {s["name"].lower() for s in shows}
    known_shows  |= {s["search_term"].lower() for s in shows if s.get("search_term")}
    known_artists = {a["name"].lower() for a in artists}
    known_artists |= {a["search_term"].lower() for a in artists if a.get("search_term")}
    known_events  = {e["title"].lower() for e in events}

    rss_entries = fetch_rss_entries()

    # ── STEP 1: Fetch Singapore trending terms from Google Trends RSS ─────
    log.info("--- Step 1: Fetching Singapore trending terms ---")
    trending_terms = fetch_trending_sg()

    if not trending_terms:
        log.warning("  No trending terms fetched — skipping discovery")
    else:
        new_shows   = 0
        new_artists = 0

        for term in trending_terms:
            term_lower = term.lower()
            log.info(f"  Trending: '{term}'")

            # Try as a show first
            if term_lower not in known_shows:
                if is_movie_title(term):
                    continue

                tmdb_show = tmdb_search_show(term)
                if tmdb_show:
                    if not tmdb_show.get("description"):
                        wiki = wiki_lookup(term)
                        tmdb_show["description"] = wiki.get("description","")

                    gc        = tmdb_show.get("genre_code","others")
                    gid       = genre_map.get(gc) or genre_map.get("others")
                    platforms = tmdb_show.get("platforms",[]) or GENRE_PLATFORM_FALLBACK.get(gc,["viu"])
                    row = {
                        "name":            term,
                        "chinese_title":   tmdb_show.get("chinese_title"),
                        "genre_id":        gid,
                        "platforms":       platforms,
                        "description":     tmdb_show.get("description",""),
                        "search_term":     tmdb_show.get("search_term", term),
                        "tmdb_id":         tmdb_show.get("tmdb_id"),
                        "has_description": bool(tmdb_show.get("description","").strip()),
                        "is_active":       True,
                        "updated_at":      now_utc(),
                    }
                    try:
                        sb.table("shows_master").insert(row).execute()
                        known_shows.add(term_lower)
                        new_shows += 1
                        log.info(f"  ✅ New show added: {term}")
                        continue  # found as show, skip artist check
                    except Exception as e:
                        log.warning(f"  ❌ Show insert failed '{term}': {e}")

            # Try as an artist if not found as show
            if term_lower not in known_artists:
                tmdb_person = tmdb_search_person(term)
                if tmdb_person:
                    gc  = tmdb_person.get("genre_code","others")
                    gid = genre_map.get(gc) or genre_map.get("others")
                    row = {
                        "name":            term,
                        "role":            tmdb_person.get("role","Actor"),
                        "show_name":       tmdb_person.get("show_name",""),
                        "genre_id":        gid,
                        "search_term":     term,
                        "tmdb_id":         tmdb_person.get("tmdb_id"),
                        "has_description": bool(tmdb_person.get("role") and tmdb_person.get("show_name")),
                        "is_active":       True,
                        "updated_at":      now_utc(),
                    }
                    try:
                        sb.table("artists_master").insert(row).execute()
                        known_artists.add(term_lower)
                        new_artists += 1
                        log.info(f"  ✅ New artist added: {term}")
                    except Exception as e:
                        log.warning(f"  ❌ Artist insert failed '{term}': {e}")

            time.sleep(0.5)  # small delay between TMDB calls only

        log.info(f"  Discovery complete: {new_shows} shows, {new_artists} artists added")

    # ── STEP 2: Discover events from RSS feeds ────────────────────────────
    log.info("--- Step 2: Discovering events from RSS feeds ---")
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

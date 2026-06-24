"""
Drama Watch SG — scraper_update.py
Runs daily at midnight SGT (16:00 UTC) via GitHub Actions.

Flow:
  0a. Auto-discover new SHOWS from FlixPatrol Netflix+Disney+ SG Top 10
  0b. Auto-discover new ARTISTS from TMDB cast of tracked shows
  0c. Auto-discover new EVENTS from Ticketmaster SG
  1.  Google Trends related queries — additional discovery
  2.  Score all active items via Google Trends
  3.  Normalise 0-100 and append history rows
  4.  has_description retry via Wikipedia
  5.  Deactivate past events

Environment variables (GitHub Actions secrets):
  SUPABASE_URL
  SUPABASE_KEY  — service_role key
  TMDB_API_KEY  — free from themoviedb.org
"""

import os, time, json, re, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import feedparser
from bs4 import BeautifulSoup
from pytrends.request import TrendReq
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
TRENDS_DELAY = 6

TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5", 119: "amazon",
}

GENRE_PLATFORM_FALLBACK = {
    "kdrama": ["netflix"], "cdrama": ["netflix"], "thai": ["netflix"],
    "local": ["mewatch"], "western": ["netflix"], "others": ["netflix"],
}

ORIGIN_TO_GENRE = {
    "KR": "kdrama", "CN": "cdrama", "TW": "cdrama", "HK": "cdrama",
    "TH": "thai", "SG": "local", "JP": "others", "TR": "others", "IN": "others",
}

GENRE_LABELS = {
    "kdrama":"K-Drama","cdrama":"C-Drama","local":"Local","thai":"Thai",
    "western":"Western","japanese":"J-Drama","turkish":"Turkish",
    "indian":"Indian","others":"Others",
}

RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
    "https://www.asiaone.com/rss/entertainment",
    "https://www.straitstimes.com/news/life/rss.xml",
    "https://www.soompi.com/feed",
]

EVENT_TYPE_MAP = {
    "fan meet": "Fan Meet", "fan meeting": "Fan Meet", "fansign": "Fan Meet",
    "fan sign": "Fan Meet", "meet and greet": "Fan Meet", "fan party": "Fan Party",
    "concert": "Concert", "showcase": "Showcase", "pop-up": "Pop-Up",
    "pop up": "Pop-Up", "premiere": "Premiere", "screening": "Screening",
    "press conference": "Event", "media call": "Event", "brand event": "Event",
    "tour": "Concert", "appearance": "Event", "awards": "Awards",
}

DISCOVERY_TERMS = {
    "shows": [
        "Korean drama Singapore","Chinese drama Singapore","Singapore drama",
        "Thai drama Singapore","Western series Singapore",
    ],
    "artists": [
        "Korean drama artists Singapore","Chinese drama artists Singapore",
        "Singapore drama artists","Thai drama artists Singapore",
    ],
}

TERM_TO_GENRE = {
    "Korean drama":"kdrama","Chinese drama":"cdrama","Singapore drama":"local",
    "Thai drama":"thai","Western series":"western","drama":"others",
}

# Non-drama content to skip from FlixPatrol
SKIP_KEYWORDS = [
    "documentary","standup","stand-up","comedy special","reality","game show",
    "news","talk show","wwe","sport","formula","football","wrestling",
]

# K-pop/drama artist keywords to identify relevant Ticketmaster events
DRAMA_EVENT_KEYWORDS = [
    "kdrama","k-drama","korean drama","cdrama","c-drama","chinese drama",
    "thai drama","kpop","k-pop","jpop","j-pop","hallyu",
    "fan meet","fan meeting","fansign","showcase","fan party",
    # Artist name fragments — checked dynamically from artists_master
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_sgt() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M SGT")

def term_to_genre_code(term: str) -> str:
    for key, code in TERM_TO_GENRE.items():
        if key.lower() in term.lower():
            return code
    return "others"


# ══════════════════════════════════════════════════════════════════════════
# STEP 0a — SHOW DISCOVERY: FlixPatrol SG Top 10
# ══════════════════════════════════════════════════════════════════════════

FLIXPATROL_PAGES = [
    ("https://flixpatrol.com/top10/netflix/singapore/", "netflix"),
    ("https://flixpatrol.com/top10/disney/singapore/",  "disney"),
]

def scrape_flixpatrol_sg(url: str) -> list:
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        soup  = BeautifulSoup(r.text, "html.parser")
        shows = []
        seen  = set()
        for a in soup.select("table a[href^='/title/']"):
            name = a.get_text(strip=True)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                shows.append(name)
        return shows[:10]
    except Exception as e:
        log.warning(f"FlixPatrol scrape failed [{url}]: {e}")
        return []

def discover_shows_from_flixpatrol(sb, genre_map, known_shows):
    log.info("--- Step 0a: FlixPatrol show discovery ---")
    candidates = {}
    for url, platform in FLIXPATROL_PAGES:
        names = scrape_flixpatrol_sg(url)
        log.info(f"  {platform}: {names}")
        for name in names:
            if name.lower() not in candidates:
                candidates[name] = platform
        time.sleep(3)

    for name, platform in candidates.items():
        if name.lower() in known_shows:
            continue
        if any(kw in name.lower() for kw in SKIP_KEYWORDS):
            log.info(f"  Skipping non-drama: {name}")
            continue
        log.info(f"  New show: '{name}' via {platform}")
        meta = tmdb_search_show(name)
        time.sleep(2)
        gc          = meta.get("genre_code", "western")
        gid         = genre_map.get(gc) or genre_map.get("others")
        platforms   = meta.get("platforms") or GENRE_PLATFORM_FALLBACK.get(gc, ["netflix"])
        description = meta.get("description", "") or wiki_lookup(name).get("description", "")
        row = {
            "name": name, "chinese_title": meta.get("chinese_title"),
            "genre_id": gid, "platforms": platforms, "description": description,
            "search_term": meta.get("search_term", f"{name} drama"),
            "tmdb_id": meta.get("tmdb_id"),
            "has_description": bool(description.strip()),
            "is_active": True, "updated_at": now_utc(),
        }
        try:
            sb.table("shows_master").insert(row).execute()
            known_shows.add(name.lower())
            log.info(f"  ✅ Show added: {name} [{gc}]")
        except Exception as e:
            if "duplicate" in str(e).lower():
                known_shows.add(name.lower())
            else:
                log.warning(f"  Show insert failed '{name}': {e}")


# ══════════════════════════════════════════════════════════════════════════
# STEP 0b — ARTIST DISCOVERY: TMDB cast of tracked shows
# ══════════════════════════════════════════════════════════════════════════

def discover_artists_from_tmdb(sb, genre_map, known_artists):
    """
    For every show in shows_master that has a tmdb_id,
    fetch the top 5 cast members from TMDB and auto-add any not yet tracked.
    """
    log.info("--- Step 0b: TMDB artist discovery ---")

    shows = sb.table("shows_master") \
        .select("id,name,tmdb_id,genre_id") \
        .eq("is_active", True) \
        .not_.is_("tmdb_id", "null") \
        .execute().data or []

    log.info(f"  Checking cast for {len(shows)} shows with TMDB IDs")
    added = 0

    for show in shows:
        tmdb_id  = show["tmdb_id"]
        genre_id = show["genre_id"]
        show_name = show["name"]

        try:
            r = requests.get(
                f"{TMDB_BASE}/tv/{tmdb_id}/credits",
                params={"api_key": TMDB_API_KEY, "language": "en-SG"},
                timeout=10,
            )
            if not r.ok:
                continue
            cast = r.json().get("cast", [])[:8]  # top 8 billed cast

            for person in cast:
                name = person.get("name", "").strip()
                if not name or name.lower() in known_artists:
                    continue

                role      = "Actress" if person.get("gender") == 1 else "Actor"
                tmdb_pid  = person.get("id")
                character = person.get("character", "")

                row = {
                    "name":       name,
                    "role":       role,
                    "show_name":  show_name,
                    "genre_id":   genre_id,
                    "search_term": name,
                    "tmdb_id":    tmdb_pid,
                    "has_description": True,  # we have role + show
                    "is_active":  True,
                    "updated_at": now_utc(),
                }
                try:
                    sb.table("artists_master").insert(row).execute()
                    known_artists.add(name.lower())
                    log.info(f"  ✅ Artist added: {name} ({role} in {show_name})")
                    added += 1
                except Exception as e:
                    if "duplicate" in str(e).lower():
                        known_artists.add(name.lower())
                    else:
                        log.warning(f"  Artist insert failed '{name}': {e}")

            time.sleep(1)  # be polite to TMDB

        except Exception as e:
            log.warning(f"  TMDB credits error for '{show_name}': {e}")

    log.info(f"--- Step 0b: {added} new artists added ---")


# ══════════════════════════════════════════════════════════════════════════
# STEP 0c — EVENT DISCOVERY: Ticketmaster SG
# ══════════════════════════════════════════════════════════════════════════

TICKETMASTER_URL = "https://ticketmaster.sg/activity"

def scrape_ticketmaster_sg(known_artist_names: set) -> list:
    """
    Scrape Ticketmaster SG listing page for upcoming K-drama/K-pop/Asian events.
    Returns list of event dicts.
    """
    log.info("  Scraping Ticketmaster SG...")
    try:
        r = requests.get(TICKETMASTER_URL, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        soup   = BeautifulSoup(r.text, "html.parser")
        events = []
        seen   = set()

        # Ticketmaster SG lists events as <a> links with date + title
        # Pattern varies — try multiple selectors
        candidates = (
            soup.select("a[href*='/activity/detail/']") +
            soup.select(".event-listing a") +
            soup.select("li a")
        )

        for a in candidates:
            title = a.get_text(separator=" ", strip=True)
            link  = a.get("href", "")
            if not title or len(title) < 5 or title.lower() in seen:
                continue
            seen.add(title.lower())

            title_lower = title.lower()

            # Check if event is relevant — mentions known artist OR drama keywords
            is_relevant = any(kw in title_lower for kw in [
                "kpop","k-pop","kdrama","k-drama","fan meet","fan meeting",
                "showcase","fansign","concert","world tour","fan party",
            ])
            # Also check if any tracked artist name appears in the title
            if not is_relevant:
                for artist_name in known_artist_names:
                    if len(artist_name) >= 4 and artist_name in title_lower:
                        is_relevant = True
                        break

            if not is_relevant:
                continue

            # Detect event type
            ev_type = "Concert"
            for kw, t in EVENT_TYPE_MAP.items():
                if kw in title_lower:
                    ev_type = t
                    break

            # Extract date if present in nearby text
            date_str = ""
            parent = a.parent
            if parent:
                parent_text = parent.get_text(" ", strip=True)
                date_match  = re.search(r"\d{1,2}\s+\w+\s+\d{4}", parent_text)
                if date_match:
                    date_str = date_match.group(0)

            if link and not link.startswith("http"):
                link = "https://ticketmaster.sg" + link

            events.append({
                "title":       title[:120],
                "type":        ev_type,
                "date_str":    date_str,
                "link":        link,
                "description": f"Upcoming event in Singapore. {title}",
            })

        log.info(f"  Ticketmaster: {len(events)} relevant events found")
        return events

    except Exception as e:
        log.warning(f"Ticketmaster scrape failed: {e}")
        return []

def discover_events_from_ticketmaster(sb, genre_map, known_events, known_artist_names):
    log.info("--- Step 0c: Ticketmaster SG event discovery ---")

    raw_events = scrape_ticketmaster_sg(known_artist_names)

    added = 0
    for ev in raw_events:
        title = ev["title"]
        if title.lower() in known_events:
            continue

        # Guess genre from title
        title_lower = title.lower()
        gc = "kdrama"  # default for Asian events
        if any(w in title_lower for w in ["chinese","mandarin","cdrama","c-drama"]):
            gc = "cdrama"
        elif any(w in title_lower for w in ["thai","thailand"]):
            gc = "thai"
        elif any(w in title_lower for w in ["singapore","sg","local"]):
            gc = "local"
        elif any(w in title_lower for w in ["japanese","japan","jpop"]):
            gc = "others"

        gid   = genre_map.get(gc) or genre_map.get("others")
        links = [{"l": "Ticketmaster SG", "u": ev["link"]}] if ev.get("link") else []

        row = {
            "title":           title,
            "genre_id":        gid,
            "type":            ev["type"],
            "venue":           "Singapore",
            "event_date":      ev.get("date_str", ""),
            "description":     ev.get("description", ""),
            "links":           json.dumps(links),
            "search_term":     f"{title} Singapore",
            "has_description": bool(ev.get("description", "").strip()),
            "is_active":       True,
            "updated_at":      now_utc(),
        }
        try:
            sb.table("events_master").insert(row).execute()
            known_events.add(title.lower())
            log.info(f"  ✅ Event added: {title[:60]}")
            added += 1
        except Exception as e:
            if "duplicate" in str(e).lower():
                known_events.add(title.lower())
            else:
                log.warning(f"  Event insert failed '{title[:40]}': {e}")

    log.info(f"--- Step 0c: {added} new events added ---")


# ══════════════════════════════════════════════════════════════════════════
# TMDB + WIKI HELPERS
# ══════════════════════════════════════════════════════════════════════════

def tmdb_search_show(name: str) -> dict:
    try:
        res = requests.get(f"{TMDB_BASE}/search/tv",
            params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"}, timeout=10)
        if not res.ok: return {}
        results = res.json().get("results",[])
        if not results: return {}
        show    = results[0]
        tmdb_id = show["id"]
        detail  = requests.get(f"{TMDB_BASE}/tv/{tmdb_id}",
            params={"api_key":TMDB_API_KEY,"language":"en-SG",
                    "append_to_response":"watch/providers,alternative_titles"}, timeout=10)
        if not detail.ok:
            return {"tmdb_id":tmdb_id,"description":show.get("overview","")}
        d = detail.json()
        sg = d.get("watch/providers",{}).get("results",{}).get("SG",{})
        platforms = []
        for p in sg.get("flatrate",[]):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms: platforms.append(code)
        alt_titles    = d.get("alternative_titles",{}).get("results",[])
        chinese_title = next((t["title"] for t in alt_titles
                              if t.get("iso_3166_1") in ["CN","TW","HK","KR"]), None)
        origin     = d.get("origin_country",[])
        genre_code = next((ORIGIN_TO_GENRE[c] for c in origin if c in ORIGIN_TO_GENRE), "western")
        return {
            "tmdb_id": tmdb_id, "description": d.get("overview",""),
            "chinese_title": chinese_title, "platforms": platforms,
            "genre_code": genre_code, "search_term": f"{name} drama",
        }
    except Exception as e:
        log.warning(f"TMDB show error '{name}': {e}"); return {}

def tmdb_search_person(name: str) -> dict:
    try:
        res = requests.get(f"{TMDB_BASE}/search/person",
            params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"}, timeout=10)
        if not res.ok: return {}
        results = res.json().get("results",[])
        if not results: return {}
        person    = results[0]
        known_for = person.get("known_for",[])
        show_name = ""; genre_code = "others"
        if known_for:
            show      = known_for[0]
            show_name = show.get("name") or show.get("title","")
            origin    = show.get("origin_country",[])
            genre_code = next((ORIGIN_TO_GENRE[c] for c in origin if c in ORIGIN_TO_GENRE), "others")
        role = "Actress" if person.get("gender")==1 else "Actor"
        return {"tmdb_id":person.get("id"),"role":role,
                "show_name":show_name,"genre_code":genre_code,"search_term":name}
    except Exception as e:
        log.warning(f"TMDB person error '{name}': {e}"); return {}

def wiki_lookup(name: str) -> dict:
    try:
        for suffix in ["", "_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(name.replace(' ','_')+suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc      = res.json().get("extract","")
                sentences = desc.split(". ")
                return {"description": ". ".join(sentences[:2]) + ("." if len(sentences)>1 else "")}
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}"); return {}


# ══════════════════════════════════════════════════════════════════════════
# RSS HELPERS
# ══════════════════════════════════════════════════════════════════════════

def fetch_rss_entries() -> list:
    entries = []
    for url in RSS_FEEDS:
        try:
            entries.extend(feedparser.parse(url).entries)
        except Exception as e:
            log.warning(f"RSS error [{url}]: {e}")
    return entries

def discover_events_from_rss(entries, artists, shows, known_events, genre_map):
    discovered = []
    names = [(a["name"], "kdrama") for a in artists[:40]]
    names += [(s["name"], "kdrama") for s in shows[:30]]
    for entry in entries:
        title_text   = entry.get("title","")
        summary_text = entry.get("summary","")
        full_text    = (title_text+" "+summary_text).lower()
        link         = entry.get("link","")
        if "singapore" not in full_text: continue
        ev_type = next((t for kw,t in EVENT_TYPE_MAP.items() if kw in full_text), None)
        if not ev_type: continue
        for name, genre_code in names:
            if len(name) < 4 or name.lower() not in full_text: continue
            event_title = title_text[:100].strip()
            search_term = f"{name} Singapore"
            if event_title.lower() in known_events or search_term.lower() in known_events: continue
            discovered.append({
                "title": event_title, "search_term": search_term,
                "type": ev_type, "description": summary_text[:500],
                "link": link, "genre_code": genre_code,
            })
            known_events.add(event_title.lower())
            break
    return discovered


# ══════════════════════════════════════════════════════════════════════════
# GOOGLE TRENDS HELPERS
# ══════════════════════════════════════════════════════════════════════════

def fetch_related_queries(pytrends, term: str) -> list:
    try:
        pytrends.build_payload([term], geo="SG", timeframe="now 7-d")
        related = pytrends.related_queries()
        if term not in related: return []
        top = related[term].get("top")
        if top is None or top.empty: return []
        return top["query"].tolist()[:10]
    except Exception as e:
        log.warning(f"Related queries error '{term}': {e}"); return []

def fetch_yesterday_score(pytrends, term: str) -> float:
    try:
        pytrends.build_payload([term], geo="SG", timeframe="today 1-m")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns: return 0.0
        if len(df) < 2: return float(df[term].iloc[-1])
        return float(df[term].iloc[-2])
    except Exception as e:
        log.warning(f"Score error '{term}': {e}"); return 0.0

def score_to_status(score, prev=None):
    if score >= 80: return "Viral"
    if score >= 60: return "Hot"
    if score >= 40: return "Rising" if prev and score > prev+3 else "Stable"
    return "Fading"

def score_to_trend(score, prev=None):
    if prev is None: return "→"
    if score > prev+5: return "↑"
    if score < prev-5: return "↓"
    return "→"

def build_sparkline(prev_sparkline, new_score):
    history = list(prev_sparkline or [])[-6:]
    history.append(round(new_score, 1))
    return history

def get_prev_scores(sb, view, id_field):
    try:
        res = sb.table(view).select(f"{id_field},score_today,sparkline").execute()
        return {r[id_field]: {"score": r.get("score_today",0), "sparkline": r.get("sparkline",[])}
                for r in (res.data or [])}
    except Exception as e:
        log.warning(f"Could not fetch prev scores from {view}: {e}"); return {}


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    log.info("=== Drama Watch SG — UPDATE scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # Load genre map
    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}

    # Load active items
    shows   = sb.table("shows_master").select("id,name,search_term,tmdb_id,genre_id").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,event_date").eq("is_active",True).execute().data or []
    log.info(f"Active: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    known_shows   = {s["name"].lower() for s in shows}
    known_artists = {a["name"].lower() for a in artists}
    known_events  = {e["title"].lower() for e in events}

    # ── STEP 0a: Show discovery via FlixPatrol ────────────────────────────
    discover_shows_from_flixpatrol(sb, genre_map, known_shows)

    # ── STEP 0b: Artist discovery via TMDB cast ───────────────────────────
    discover_artists_from_tmdb(sb, genre_map, known_artists)

    # ── STEP 0c: Event discovery via Ticketmaster SG ──────────────────────
    discover_events_from_ticketmaster(sb, genre_map, known_events, known_artists)

    # Reload all after auto-discovery
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,event_date").eq("is_active",True).execute().data or []
    known_shows   = {s["name"].lower() for s in shows}
    known_artists = {a["name"].lower() for a in artists}
    known_events  = {e["title"].lower() for e in events}

    # ── STEP 1: Google Trends related queries (additional discovery) ───────
    log.info("--- Step 1: Google Trends additional discovery ---")
    rss_entries = fetch_rss_entries()

    for category, terms in DISCOVERY_TERMS.items():
        for term in terms:
            genre_code = term_to_genre_code(term)
            queries    = fetch_related_queries(pytrends, term)
            for query in queries:
                query_lower = query.lower()
                if category == "shows" and query_lower not in known_shows:
                    tmdb = tmdb_search_show(query)
                    if not tmdb.get("description"):
                        tmdb["description"] = wiki_lookup(query).get("description","")
                    gc  = tmdb.get("genre_code", genre_code)
                    gid = genre_map.get(gc) or genre_map.get("others")
                    row = {
                        "name": query, "chinese_title": tmdb.get("chinese_title"),
                        "genre_id": gid,
                        "platforms": tmdb.get("platforms",[]) or GENRE_PLATFORM_FALLBACK.get(gc,["netflix"]),
                        "description": tmdb.get("description",""),
                        "search_term": tmdb.get("search_term", query),
                        "tmdb_id": tmdb.get("tmdb_id"),
                        "has_description": bool(tmdb.get("description","").strip()),
                        "is_active": True, "updated_at": now_utc(),
                    }
                    try:
                        sb.table("shows_master").insert(row).execute()
                        known_shows.add(query_lower)
                        log.info(f"  ✅ Trends show added: {query}")
                    except: pass

                elif category == "artists" and query_lower not in known_artists:
                    tmdb = tmdb_search_person(query)
                    gc   = tmdb.get("genre_code", genre_code)
                    gid  = genre_map.get(gc) or genre_map.get("others")
                    row  = {
                        "name": query, "role": tmdb.get("role","Actor"),
                        "show_name": tmdb.get("show_name",""), "genre_id": gid,
                        "search_term": query, "tmdb_id": tmdb.get("tmdb_id"),
                        "has_description": bool(tmdb.get("role") and tmdb.get("show_name")),
                        "is_active": True, "updated_at": now_utc(),
                    }
                    try:
                        sb.table("artists_master").insert(row).execute()
                        known_artists.add(query_lower)
                        log.info(f"  ✅ Trends artist added: {query}")
                    except: pass

            time.sleep(TRENDS_DELAY)

    # RSS event discovery
    log.info("--- Step 1b: RSS event discovery ---")
    rss_shows   = sb.table("shows_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    rss_artists = sb.table("artists_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    for ev in discover_events_from_rss(rss_entries, rss_artists, rss_shows, known_events, genre_map):
        gid   = genre_map.get(ev["genre_code"]) or genre_map.get("others")
        links = [{"l":"More info","u":ev["link"]}] if ev.get("link") else []
        row   = {
            "title": ev["title"], "genre_id": gid, "type": ev["type"],
            "venue": "Singapore", "event_date": "", "description": ev["description"],
            "links": json.dumps(links), "search_term": ev["search_term"],
            "has_description": bool(ev["description"].strip()),
            "is_active": True, "updated_at": now_utc(),
        }
        try:
            sb.table("events_master").insert(row).execute()
            log.info(f"  ✅ RSS event added: {ev['title'][:60]}")
        except Exception as e:
            log.warning(f"  RSS event insert failed: {e}")

    # Final reload before scoring
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active",True).execute().data or []

    # ── STEP 2: Score all active items ────────────────────────────────────
    log.info("--- Step 2: Scoring all active items ---")
    show_raw, artist_raw, event_raw = {}, {}, {}

    for s in shows:
        show_raw[s["name"]] = fetch_yesterday_score(pytrends, s.get("search_term") or s["name"])
        log.info(f"  show {s['name']}: {show_raw[s['name']]:.1f}")
        time.sleep(TRENDS_DELAY)

    for a in artists:
        artist_raw[a["name"]] = fetch_yesterday_score(pytrends, a.get("search_term") or a["name"])
        log.info(f"  artist {a['name']}: {artist_raw[a['name']]:.1f}")
        time.sleep(TRENDS_DELAY)

    for e in events:
        event_raw[e["title"]] = fetch_yesterday_score(pytrends, e.get("search_term") or e["title"])
        log.info(f"  event {e['title'][:40]}: {event_raw[e['title']]:.1f}")
        time.sleep(TRENDS_DELAY)

    all_raw   = {**show_raw, **artist_raw, **event_raw}
    max_score = max(all_raw.values()) if all_raw else 1
    if max_score == 0: max_score = 1

    def norm(scores):
        return {k: round((v/max_score)*100, 1) for k,v in scores.items()}

    show_norm   = norm(show_raw)
    artist_norm = norm(artist_raw)
    event_norm  = norm(event_raw)

    show_prev   = get_prev_scores(sb, "shows_scores",   "id")
    artist_prev = get_prev_scores(sb, "artists_scores", "id")
    event_prev  = get_prev_scores(sb, "events_scores",  "id")

    # ── STEP 3: Append history rows ───────────────────────────────────────
    log.info("--- Step 3: Appending history rows ---")
    sgt           = timezone(timedelta(hours=8))
    yesterday_sgt = (datetime.now(sgt) - timedelta(days=1)).date()
    recorded_at   = datetime(yesterday_sgt.year, yesterday_sgt.month, yesterday_sgt.day,
                             4, 0, 0, tzinfo=timezone.utc).isoformat()

    show_rows = []
    for s in shows:
        score = show_norm.get(s["name"], 0)
        prev  = show_prev.get(s["id"], {})
        show_rows.append({
            "show_id": s["id"], "score": score,
            "trends_score": show_raw.get(s["name"], 0),
            "status": score_to_status(score, prev.get("score")),
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if show_rows:
        sb.table("shows_history").insert(show_rows).execute()
        log.info(f"  {len(show_rows)} show history rows inserted")

    artist_rows = []
    for a in artists:
        score = artist_norm.get(a["name"], 0)
        prev  = artist_prev.get(a["id"], {})
        artist_rows.append({
            "artist_id": a["id"], "score": score,
            "trends_score": artist_raw.get(a["name"], 0),
            "status": score_to_status(score, prev.get("score")),
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if artist_rows:
        sb.table("artists_history").insert(artist_rows).execute()
        log.info(f"  {len(artist_rows)} artist history rows inserted")

    event_rows = []
    for e in events:
        score = event_norm.get(e["title"], 0)
        prev  = event_prev.get(e["id"], {})
        event_rows.append({
            "event_id": e["id"], "score": score,
            "trends_score": event_raw.get(e["title"], 0),
            "status": "Upcoming",
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if event_rows:
        sb.table("events_history").insert(event_rows).execute()
        log.info(f"  {len(event_rows)} event history rows inserted")

    # ── STEP 4: Fill missing descriptions ────────────────────────────────
    log.info("--- Step 4: Filling missing descriptions ---")
    for table, name_field in [("shows_master","name"), ("events_master","title")]:
        try:
            pending = sb.table(table).select(f"id,{name_field}") \
                .eq("is_active",True).eq("has_description",False).execute().data or []
            for item in pending:
                desc = wiki_lookup(item[name_field]).get("description","").strip()
                if desc:
                    sb.table(table).update({
                        "description": desc, "has_description": True, "updated_at": now_utc(),
                    }).eq("id", item["id"]).execute()
                    log.info(f"  ✅ Description filled: {item[name_field]}")
                time.sleep(1)
        except Exception as e:
            log.warning(f"  Description retry error for {table}: {e}")

    # ── STEP 5: Deactivate past events ────────────────────────────────────
    log.info("--- Step 5: Deactivating past events ---")
    now_sgt_dt    = datetime.now(timezone(timedelta(hours=8)))
    past_months   = [datetime(now_sgt_dt.year, m, 1).strftime("%b").lower()
                     for m in range(1, now_sgt_dt.month)]
    all_events    = sb.table("events_master").select("id,title,event_date").eq("is_active",True).execute().data or []
    for e in all_events:
        date_str = (e.get("event_date") or "").lower()
        if any(m in date_str for m in past_months):
            sb.table("events_master").update({"is_active":False,"updated_at":now_utc()}).eq("id",e["id"]).execute()
            log.info(f"  Deactivated: {e['title'][:50]}")

    log.info("=== Update complete ===")
    log.info(f"  Shows: {len(show_rows)} | Artists: {len(artist_rows)} | Events: {len(event_rows)}")


if __name__ == "__main__":
    main()

"""
Drama Watch SG — scraper_update.py
Runs daily at midnight SGT (16:00 UTC) via GitHub Actions.

Flow:
  0. Auto-discover new shows from FlixPatrol SG Top 10 (Netflix + Disney+)
  1. Fetch Google Trends related queries — discover new items
  2. TMDB / Wikipedia / RSS enrich new items
  3. Insert new items into master tables
  4. Score all active items — fetch yesterday's Trends data
  5. Normalise 0-100 per category
  6. Append one history row per item
  7. Re-rank genres → update is_top5
  8. has_description retry via Wikipedia
  9. Deactivate past events

Environment variables (GitHub Actions secrets):
  SUPABASE_URL
  SUPABASE_KEY  — service_role key
  TMDB_API_KEY  — free from themoviedb.org
"""

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import feedparser
from bs4 import BeautifulSoup
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

TMDB_BASE   = "https://api.themoviedb.org/3"
WIKI_API    = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKI_HEADERS = {"User-Agent": "DramaWatchSG/1.0"}
TRENDS_DELAY = 6

TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5",
    119: "amazon",
}

GENRE_COLORS = [
    {"dot_color":"#7F77DD","bg_color":"#EEEDFE","text_color":"#3C3489"},
    {"dot_color":"#D85A30","bg_color":"#FAECE7","text_color":"#993C1D"},
    {"dot_color":"#1D9E75","bg_color":"#E1F5EE","text_color":"#085041"},
    {"dot_color":"#BA7517","bg_color":"#FAEEDA","text_color":"#633806"},
    {"dot_color":"#378ADD","bg_color":"#E6F1FB","text_color":"#0C447C"},
    {"dot_color":"#9C27B0","bg_color":"#F3E5F5","text_color":"#4A148C"},
]

GENRE_LABELS = {
    "kdrama":"K-Drama","cdrama":"C-Drama","local":"Local",
    "thai":"Thai","western":"Western","japanese":"J-Drama",
    "turkish":"Turkish","indian":"Indian","others":"Others",
    "anime":"Anime","filipino":"Filipino",
}

RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
    "https://www.asiaone.com/rss/entertainment",
    "https://www.straitstimes.com/news/life/rss.xml",
    "https://www.soompi.com/feed",
]

EVENT_RSS_KEYWORDS = [
    "singapore", "fan meet", "fan meeting", "concert", "showcase",
    "pop-up", "pop up", "premiere", "screening", "press conference",
    "media call", "brand event", "fan sign", "fan party", "meet and greet",
    "fansign", "tour", "appearance", "visit", "touch down", "touches down",
    "lands in", "arrives in", "coming to singapore", "in singapore",
]

EVENT_TYPE_MAP = {
    "fan meet": "Fan Meet", "fan meeting": "Fan Meet", "fansign": "Fan Meet",
    "fan sign": "Fan Meet", "meet and greet": "Fan Meet", "fan party": "Fan Party",
    "concert": "Concert", "showcase": "Showcase", "pop-up": "Pop-Up",
    "pop up": "Pop-Up", "premiere": "Premiere", "screening": "Screening",
    "press conference": "Event", "media call": "Event", "brand event": "Event",
    "tour": "Concert", "appearance": "Event",
}

DISCOVERY_TERMS = {
    "shows": [
        "Korean drama Singapore","Chinese drama Singapore",
        "Singapore drama","Thai drama Singapore",
        "Western series Singapore","Japanese drama Singapore",
        "Turkish drama Singapore","Indian drama Singapore",
    ],
    "artists": [
        "Korean drama artists Singapore","Chinese drama artists Singapore",
        "Singapore drama artists","Thai drama artists Singapore",
        "Western series artists Singapore","Japanese drama artists Singapore",
        "Turkish drama artists Singapore","Indian drama artists Singapore",
    ],
}

EVENT_SUFFIXES = [
    "Singapore concert", "Singapore fan meet", "Singapore tour",
    "Singapore fan meeting", "Singapore showcase",
]

TERM_TO_GENRE = {
    "Korean drama":"kdrama","Chinese drama":"cdrama",
    "Singapore drama":"local","Thai drama":"thai",
    "Western series":"western","Japanese drama":"japanese",
    "Turkish drama":"turkish","Indian drama":"indian","drama":"others",
}

GENRE_PLATFORM_FALLBACK = {
    "kdrama":  ["netflix"],
    "cdrama":  ["netflix"],
    "thai":    ["netflix"],
    "local":   ["mewatch"],
    "western": ["netflix"],
    "others":  ["netflix"],
}

ORIGIN_TO_GENRE = {
    "KR": "kdrama", "CN": "cdrama", "TW": "cdrama",
    "HK": "cdrama", "TH": "thai",   "SG": "local",
    "JP": "others", "TR": "others", "IN": "others",
}

# Non-drama keywords — skip these from FlixPatrol
SKIP_KEYWORDS = [
    "documentary", "standup", "stand-up", "comedy special",
    "reality", "game show", "news", "talk show",
    "wwe", "sport", "formula", "football",
]

FLIXPATROL_PAGES = [
    ("https://flixpatrol.com/top10/netflix/singapore/", "netflix"),
    ("https://flixpatrol.com/top10/disney/singapore/",  "disney"),
]

FLIXPATROL_HEADERS = {
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
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%Y-%m-%d %H:%M SGT")

def term_to_genre_code(term: str) -> str:
    for key, code in TERM_TO_GENRE.items():
        if key.lower() in term.lower():
            return code
    return "others"

def build_event_terms(shows: list, artists: list, limit: int = 8) -> list:
    terms = []
    names = [a["name"] for a in artists[:6]] + [s["name"] for s in shows[:4]]
    for i, name in enumerate(names):
        suffix = EVENT_SUFFIXES[i % len(EVENT_SUFFIXES)]
        terms.append(f"{name} {suffix}")
    return terms[:limit]


# ── FLIXPATROL AUTO-DISCOVERY ─────────────────────────────────────────────

def scrape_flixpatrol_sg(url: str) -> list:
    """Scrape FlixPatrol SG Top 10 TV shows. Returns list of show names."""
    try:
        r = requests.get(url, headers=FLIXPATROL_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        shows = []
        seen = set()
        for a in soup.select("table a[href^='/title/']"):
            name = a.get_text(strip=True)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                shows.append(name)
        return shows[:10]
    except Exception as e:
        log.warning(f"FlixPatrol scrape failed [{url}]: {e}")
        return []

def discover_from_flixpatrol(sb, genre_map: dict, known_shows: set) -> None:
    """
    Scrape FlixPatrol Netflix + Disney+ SG Top 10 daily.
    Auto-insert any new TV shows not already in shows_master.
    """
    log.info("--- Step 0: FlixPatrol SG auto-discovery ---")
    candidates = {}
    for url, platform in FLIXPATROL_PAGES:
        names = scrape_flixpatrol_sg(url)
        log.info(f"  {platform}: {names}")
        for name in names:
            if name.lower() not in candidates:
                candidates[name] = platform
        time.sleep(3)

    for name, platform in candidates.items():
        name_lower = name.lower()
        if name_lower in known_shows:
            continue
        # Skip obvious non-dramas
        if any(kw in name_lower for kw in SKIP_KEYWORDS):
            log.info(f"  Skipping non-drama: {name}")
            continue

        log.info(f"  New show from FlixPatrol: '{name}' (via {platform})")
        meta = tmdb_search_show(name)
        time.sleep(2)

        gc = meta.get("genre_code", "western")
        gid = genre_map.get(gc) or genre_map.get("others")
        platforms = meta.get("platforms") or GENRE_PLATFORM_FALLBACK.get(gc, ["netflix"])
        description = meta.get("description", "")

        if not description:
            wiki = wiki_lookup(name)
            description = wiki.get("description", "")

        row = {
            "name":          name,
            "chinese_title": meta.get("chinese_title"),
            "genre_id":      gid,
            "platforms":     platforms,
            "description":   description,
            "search_term":   meta.get("search_term", f"{name} drama"),
            "tmdb_id":       meta.get("tmdb_id"),
            "has_description": bool(description.strip()),
            "is_active":     True,
            "updated_at":    now_utc(),
        }
        try:
            sb.table("shows_master").insert(row).execute()
            known_shows.add(name_lower)
            log.info(f"  ✅ Auto-added: {name} [{gc}]")
        except Exception as e:
            if "duplicate" in str(e).lower():
                known_shows.add(name_lower)
            else:
                log.warning(f"  Insert failed for '{name}': {e}")

    log.info("--- Step 0: FlixPatrol discovery complete ---")


# ── TMDB HELPERS ──────────────────────────────────────────────────────────

def tmdb_search_show(name: str) -> dict:
    try:
        res = requests.get(f"{TMDB_BASE}/search/tv",
            params={"api_key":TMDB_API_KEY,"query":name,"language":"en-SG"},
            timeout=10)
        if not res.ok: return {}
        results = res.json().get("results",[])
        if not results: return {}
        show = results[0]
        tmdb_id = show["id"]
        detail = requests.get(f"{TMDB_BASE}/tv/{tmdb_id}",
            params={"api_key":TMDB_API_KEY,"language":"en-SG",
                    "append_to_response":"watch/providers,alternative_titles"},
            timeout=10)
        if not detail.ok:
            return {"tmdb_id":tmdb_id,"description":show.get("overview","")}
        d = detail.json()
        sg = d.get("watch/providers",{}).get("results",{}).get("SG",{})
        platforms = []
        for p in sg.get("flatrate",[]):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms: platforms.append(code)
        alt_titles = d.get("alternative_titles",{}).get("results",[])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN","TW","HK","KR"]:
                chinese_title = t.get("title"); break
        origin = d.get("origin_country",[])
        genre_code = "western"
        for country, code in ORIGIN_TO_GENRE.items():
            if country in origin:
                genre_code = code
                break
        status   = d.get("status","")
        last_air = d.get("last_air_date","")
        is_new   = status in ["Returning Series","In Production"]
        if last_air:
            try:
                days_ago = (datetime.now()-datetime.strptime(last_air,"%Y-%m-%d")).days
                is_new = days_ago <= 14
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
        person = results[0]
        known_for = person.get("known_for",[])
        show_name = ""; genre_code = "others"
        if known_for:
            show = known_for[0]
            show_name = show.get("name") or show.get("title","")
            origin = show.get("origin_country",[])
            if isinstance(origin,list):
                for country, code in ORIGIN_TO_GENRE.items():
                    if country in origin:
                        genre_code = code
                        break
        role = "Actress" if person.get("gender")==1 else "Actor"
        return {"tmdb_id":person.get("id"),"role":role,
                "show_name":show_name,"genre_code":genre_code,"search_term":name}
    except Exception as e:
        log.warning(f"TMDB person error '{name}': {e}"); return {}

def wiki_lookup(name: str) -> dict:
    try:
        wiki_name = name.replace(" ","_")
        for suffix in ["", "_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name+suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc = res.json().get("extract","")
                sentences = desc.split(". ")
                return {"description": ". ".join(sentences[:2]) + ("." if len(sentences)>1 else "")}
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}"); return {}


# ── RSS HELPERS ───────────────────────────────────────────────────────────

def fetch_rss_entries() -> list:
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
        except Exception as e:
            log.warning(f"RSS error [{url}]: {e}")
    return entries

def enrich_event_from_rss(title: str, entries: list) -> dict:
    title_lower = title.lower()
    for entry in entries:
        entry_text = (entry.get("title","")+" "+entry.get("summary","")).lower()
        if any(w in entry_text for w in title_lower.split() if len(w)>4):
            ev_type = "Event"
            for t in ["fan meet","concert","screening","pop-up","awards",
                      "exhibition","festival","showcase","premiere"]:
                if t in entry_text: ev_type = t.title(); break
            return {"description":entry.get("summary","")[:500],
                    "type":ev_type,"link":entry.get("link","")}
    return {}

def discover_events_from_rss(entries: list, artists: list, shows: list,
                              known_events: set, genre_map: dict) -> list:
    discovered = []
    names = [(a["name"], genre_map.get(a.get("genre_id",""), "others")) for a in artists[:40]]
    names += [(s["name"], s.get("genre","others")) for s in shows[:30]]
    for entry in entries:
        title_text   = entry.get("title", "")
        summary_text = entry.get("summary", "")
        full_text    = (title_text + " " + summary_text).lower()
        link         = entry.get("link", "")
        if "singapore" not in full_text:
            continue
        ev_type = None
        for kw, t in EVENT_TYPE_MAP.items():
            if kw in full_text:
                ev_type = t; break
        if not ev_type:
            continue
        for name, genre_code in names:
            if len(name) < 4: continue
            if name.lower() not in full_text: continue
            event_title = title_text[:100].strip()
            search_term = f"{name} Singapore"
            if event_title.lower() in known_events or search_term.lower() in known_events:
                continue
            log.info(f"  RSS event found: '{event_title}' (artist: {name})")
            discovered.append({
                "title": event_title, "search_term": search_term,
                "type": ev_type, "description": summary_text[:500],
                "link": link, "genre_code": genre_code,
            })
            known_events.add(event_title.lower())
            break
    return discovered


# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

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

def score_to_status(score: float, prev: float = None) -> str:
    if score >= 80: return "Viral"
    if score >= 60: return "Hot"
    if score >= 40:
        if prev and score > prev+3: return "Rising"
        return "Stable"
    return "Fading"

def score_to_trend(score: float, prev: float = None) -> str:
    if prev is None: return "→"
    if score > prev+5: return "↑"
    if score < prev-5: return "↓"
    return "→"

def build_sparkline(prev_sparkline: list, new_score: float) -> list:
    history = list(prev_sparkline or [])[-6:]
    history.append(round(new_score, 1))
    return history

def get_prev_scores(sb, view: str, id_field: str) -> dict:
    try:
        res = sb.table(view).select(f"{id_field},score_today,sparkline").execute()
        return {
            r[id_field]: {"score": r.get("score_today", 0), "sparkline": r.get("sparkline", [])}
            for r in (res.data or [])
        }
    except Exception as e:
        log.warning(f"Could not fetch prev scores from {view}: {e}")
        return {}


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — UPDATE scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # Load genre map
    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}

    # Load active items
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,event_date").eq("is_active",True).execute().data or []
    log.info(f"Active: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    known_shows   = {s["name"].lower() for s in shows}
    known_artists = {a["name"].lower() for a in artists}
    known_events  = {e["title"].lower() for e in events}

    # ── STEP 0: FlixPatrol auto-discovery ────────────────────────────────
    discover_from_flixpatrol(sb, genre_map, known_shows)

    # Reload shows after FlixPatrol discovery
    shows = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    known_shows = {s["name"].lower() for s in shows}

    # ── STEP 1: Google Trends related queries discovery ───────────────────
    log.info("--- Step 1: Discovering new items via Google Trends ---")
    rss_entries = fetch_rss_entries()

    for category, terms in DISCOVERY_TERMS.items():
        for term in terms:
            genre_code = term_to_genre_code(term)
            queries    = fetch_related_queries(pytrends, term)
            for query in queries:
                query_lower = query.lower()
                if category == "shows" and query_lower not in known_shows:
                    log.info(f"  New show candidate: '{query}'")
                    tmdb = tmdb_search_show(query)
                    if not tmdb.get("description"):
                        wiki = wiki_lookup(query)
                        tmdb["description"] = wiki.get("description","")
                    gc  = tmdb.get("genre_code", genre_code)
                    gid = genre_map.get(gc) or genre_map.get("others")
                    platforms = tmdb.get("platforms",[]) or GENRE_PLATFORM_FALLBACK.get(gc, ["netflix"])
                    row = {
                        "name": query,
                        "chinese_title": tmdb.get("chinese_title"),
                        "genre_id": gid,
                        "platforms": platforms,
                        "description": tmdb.get("description",""),
                        "search_term": tmdb.get("search_term", query),
                        "tmdb_id": tmdb.get("tmdb_id"),
                        "has_description": bool(tmdb.get("description","").strip()),
                        "is_active": True,
                        "updated_at": now_utc(),
                    }
                    try:
                        sb.table("shows_master").insert(row).execute()
                        known_shows.add(query_lower)
                        log.info(f"  ✅ New show added: {query}")
                    except: pass

                elif category == "artists" and query_lower not in known_artists:
                    log.info(f"  New artist candidate: '{query}'")
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
                        log.info(f"  ✅ New artist added: {query}")
                    except: pass

                elif category == "events" and query_lower not in known_events:
                    log.info(f"  New event candidate: '{query}'")
                    rss = enrich_event_from_rss(query, rss_entries)
                    gid = genre_map.get(genre_code) or genre_map.get("others")
                    links = [{"l":"More info","u":rss["link"]}] if rss.get("link") else []
                    row = {
                        "title": query, "genre_id": gid, "type": rss.get("type","Event"),
                        "venue": "", "event_date": "", "description": rss.get("description",""),
                        "links": json.dumps(links), "search_term": query,
                        "has_description": bool(rss.get("description","").strip()),
                        "is_active": True, "updated_at": now_utc(),
                    }
                    try:
                        sb.table("events_master").insert(row).execute()
                        known_events.add(query_lower)
                        log.info(f"  ✅ New event added: {query[:50]}")
                    except: pass

            time.sleep(TRENDS_DELAY)

    # ── Step 1b: Event discovery from top shows/artists ───────────────────
    log.info("--- Step 1b: Discovering events from top shows/artists ---")
    sorted_artists = sorted(artists, key=lambda a: a.get("name",""))
    sorted_shows   = sorted(shows,   key=lambda s: s.get("name",""))
    event_terms    = build_event_terms(sorted_shows, sorted_artists)
    for term in event_terms:
        queries = fetch_related_queries(pytrends, term)
        for query in queries:
            query_lower = query.lower()
            if query_lower in known_events: continue
            rss  = enrich_event_from_rss(query, rss_entries)
            gid  = genre_map.get("others")
            links = [{"l":"More info","u":rss["link"]}] if rss.get("link") else []
            row  = {
                "title": query, "genre_id": gid, "type": rss.get("type","Event"),
                "venue": "", "event_date": "", "description": rss.get("description",""),
                "links": json.dumps(links), "search_term": query,
                "has_description": bool(rss.get("description","").strip()),
                "is_active": True, "updated_at": now_utc(),
            }
            try:
                sb.table("events_master").insert(row).execute()
                known_events.add(query_lower)
                log.info(f"  ✅ New event added: {query[:50]}")
            except: pass
        time.sleep(TRENDS_DELAY)

    # ── Step 1c: Event discovery from RSS feeds ───────────────────────────
    log.info("--- Step 1c: Discovering events from RSS feeds ---")
    rss_shows   = sb.table("shows_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    rss_artists = sb.table("artists_master").select("id,name,genre_id").eq("is_active",True).execute().data or []
    rss_discovered = discover_events_from_rss(rss_entries, rss_artists, rss_shows, known_events, genre_map)
    for ev in rss_discovered:
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

    # Reload all after discoveries
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active",True).execute().data or []

    # ── STEP 2: Score all active items ────────────────────────────────────
    log.info("--- Step 2: Scoring all active items ---")
    show_raw, artist_raw, event_raw = {}, {}, {}

    log.info("  Scoring shows...")
    for s in shows:
        term = s.get("search_term") or s["name"]
        show_raw[s["name"]] = fetch_yesterday_score(pytrends, term)
        log.info(f"    {s['name']}: {show_raw[s['name']]:.1f}")
        time.sleep(TRENDS_DELAY)

    log.info("  Scoring artists...")
    for a in artists:
        term = a.get("search_term") or a["name"]
        artist_raw[a["name"]] = fetch_yesterday_score(pytrends, term)
        log.info(f"    {a['name']}: {artist_raw[a['name']]:.1f}")
        time.sleep(TRENDS_DELAY)

    log.info("  Scoring events...")
    for e in events:
        term = e.get("search_term") or e["title"]
        event_raw[e["title"]] = fetch_yesterday_score(pytrends, term)
        log.info(f"    {e['title'][:40]}: {event_raw[e['title']]:.1f}")
        time.sleep(TRENDS_DELAY)

    # Normalise across all items together
    all_raw   = {**show_raw, **artist_raw, **event_raw}
    max_score = max(all_raw.values()) if all_raw else 1
    if max_score == 0: max_score = 1

    def normalise_all(scores: dict) -> dict:
        return {k: round((v / max_score) * 100, 1) for k, v in scores.items()}

    show_norm   = normalise_all(show_raw)
    artist_norm = normalise_all(artist_raw)
    event_norm  = normalise_all(event_raw)

    show_prev   = get_prev_scores(sb, "shows_scores",   "id")
    artist_prev = get_prev_scores(sb, "artists_scores", "id")
    event_prev  = get_prev_scores(sb, "events_scores",  "id")

    # ── STEP 3: Append history rows ───────────────────────────────────────
    log.info("--- Step 3: Appending history rows ---")
    sgt          = timezone(timedelta(hours=8))
    yesterday_sgt = (datetime.now(sgt) - timedelta(days=1)).date()
    recorded_at  = datetime(
        yesterday_sgt.year, yesterday_sgt.month, yesterday_sgt.day,
        4, 0, 0, tzinfo=timezone.utc
    ).isoformat()

    show_rows = []
    for s in shows:
        name  = s["name"]; sid = s["id"]
        score = show_norm.get(name, 0)
        prev  = show_prev.get(sid, {})
        show_rows.append({
            "show_id": sid, "score": score,
            "trends_score": show_raw.get(name, 0),
            "status": score_to_status(score, prev.get("score")),
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if show_rows:
        sb.table("shows_history").insert(show_rows).execute()
        log.info(f"  Inserted {len(show_rows)} show history rows")

    artist_rows = []
    for a in artists:
        name  = a["name"]; aid = a["id"]
        score = artist_norm.get(name, 0)
        prev  = artist_prev.get(aid, {})
        artist_rows.append({
            "artist_id": aid, "score": score,
            "trends_score": artist_raw.get(name, 0),
            "status": score_to_status(score, prev.get("score")),
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if artist_rows:
        sb.table("artists_history").insert(artist_rows).execute()
        log.info(f"  Inserted {len(artist_rows)} artist history rows")

    event_rows = []
    for e in events:
        title = e["title"]; eid = e["id"]
        score = event_norm.get(title, 0)
        prev  = event_prev.get(eid, {})
        event_rows.append({
            "event_id": eid, "score": score,
            "trends_score": event_raw.get(title, 0),
            "status": "Upcoming",
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline",[]), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if event_rows:
        sb.table("events_history").insert(event_rows).execute()
        log.info(f"  Inserted {len(event_rows)} event history rows")

    # ── STEP 4: Genre ranking fixed ───────────────────────────────────────
    log.info("--- Step 4: Genre ranking fixed — skipping re-rank ---")

    # ── STEP 5: has_description retry via Wikipedia ───────────────────────
    log.info("--- Step 5: Filling missing descriptions ---")
    for table, name_field in [("shows_master","name"), ("events_master","title")]:
        try:
            pending = sb.table(table).select(f"id,{name_field}") \
                .eq("is_active", True).eq("has_description", False).execute().data or []
            log.info(f"  {table}: {len(pending)} items missing description")
            for item in pending:
                name = item[name_field]
                wiki = wiki_lookup(name)
                desc = wiki.get("description","").strip()
                if desc:
                    sb.table(table).update({
                        "description": desc, "has_description": True, "updated_at": now_utc(),
                    }).eq("id", item["id"]).execute()
                    log.info(f"  ✅ Description filled: {name}")
                time.sleep(1)
        except Exception as e:
            log.warning(f"  has_description retry error for {table}: {e}")

    # ── STEP 6: Deactivate past events ────────────────────────────────────
    log.info("--- Step 6: Deactivating past events ---")
    now_sgt_date  = datetime.now(timezone(timedelta(hours=8)))
    current_month = now_sgt_date.month
    current_year  = now_sgt_date.year
    past_months   = [datetime(current_year, m, 1).strftime("%b").lower() for m in range(1, current_month)]
    all_events    = sb.table("events_master").select("id,title,event_date").eq("is_active",True).execute().data or []
    for e in all_events:
        date_str = (e.get("event_date") or "").lower()
        if any(m in date_str for m in past_months):
            sb.table("events_master").update({"is_active": False, "updated_at": now_utc()}).eq("id", e["id"]).execute()
            log.info(f"  Deactivated past event: {e['title'][:50]}")

    log.info("=== Update complete ===")
    log.info(f"  Shows scored:   {len(show_rows)}")
    log.info(f"  Artists scored: {len(artist_rows)}")
    log.info(f"  Events scored:  {len(event_rows)}")


if __name__ == "__main__":
    main()

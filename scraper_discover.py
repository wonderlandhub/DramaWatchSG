"""
Drama Watch SG — scraper_discover.py

Runs Mon/Wed/Fri at midnight SGT (16:00 UTC) via GitHub Actions.
Only discovers new shows, artists, and events — no scoring.
Keeping this separate from scraper_update.py reduces daily Trends API calls.
"""

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import feedparser
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
TRENDS_DELAY = 15  # longer delay — discovery only, fewer total calls

TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5",
    119: "amazon",
}

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
    "Singapore concert","Singapore fan meet","Singapore tour",
    "Singapore fan meeting","Singapore showcase",
]

TERM_TO_GENRE = {
    "Korean drama":"kdrama","Chinese drama":"cdrama",
    "Singapore drama":"local","Thai drama":"thai",
    "Western series":"western","Japanese drama":"japanese",
    "Turkish drama":"turkish","Indian drama":"indian","drama":"others",
}

GENRE_PLATFORM_FALLBACK = {
    "kdrama":["viu"],"cdrama":["wetv","iqiyi"],
    "thai":["viu"],"local":["mewatch"],
    "western":["netflix"],"others":["viu"],
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

def term_to_genre_code(term: str) -> str:
    for key, code in TERM_TO_GENRE.items():
        if key.lower() in term.lower():
            return code
    return "others"

def build_event_terms(shows: list, artists: list, limit: int = 6) -> list:
    names = [a["name"] for a in artists[:4]] + [s["name"] for s in shows[:3]]
    terms = []
    for i, name in enumerate(names):
        suffix = EVENT_SUFFIXES[i % len(EVENT_SUFFIXES)]
        terms.append(f"{name} {suffix}")
    return terms[:limit]


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
        known_for = person.get("known_for",[])
        show_name = ""; genre_code = "others"
        if known_for:
            show      = known_for[0]
            show_name = show.get("name") or show.get("title","")
            origin    = show.get("origin_country",[])
            if isinstance(origin,list):
                if   "KR" in origin: genre_code = "kdrama"
                elif "CN" in origin: genre_code = "cdrama"
                elif "TH" in origin: genre_code = "thai"
                elif "SG" in origin: genre_code = "local"
                elif "JP" in origin: genre_code = "japanese"
                elif "TR" in origin: genre_code = "turkish"
                elif "IN" in origin: genre_code = "indian"
                else:                genre_code = "western"
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


# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

def fetch_related_queries(pytrends, term: str) -> list:
    try:
        pytrends.build_payload([term], geo="SG", timeframe="now 7-d")
        related = pytrends.related_queries()
        if term not in related:
            log.info(f"  No related queries returned for '{term}'")
            return []
        top = related[term].get("top")
        if top is None or top.empty:
            log.info(f"  Empty related queries for '{term}'")
            return []
        return top["query"].tolist()[:10]
    except Exception as e:
        log.warning(f"  Related queries error '{term}': {e}")
        return []


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — DISCOVER scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10,25))

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

    # ── STEP 1: Discover new shows and artists ────────────────────────────
    log.info("--- Step 1: Discovering new shows and artists ---")
    for category, terms in DISCOVERY_TERMS.items():
        for term in terms:
            genre_code = term_to_genre_code(term)
            queries    = fetch_related_queries(pytrends, term)

            for query in queries:
                query_lower = query.lower()

                if category == "shows" and query_lower not in known_shows:
                    log.info(f"  New show candidate: '{query}'")
                    if is_movie_title(query): continue
                    tmdb = tmdb_search_show(query)
                    if not tmdb:
                        log.info(f"  ⏭  '{query}' — no valid TV result, skipping")
                        continue
                    if not tmdb.get("description"):
                        wiki = wiki_lookup(query)
                        tmdb["description"] = wiki.get("description","")

                    gc        = tmdb.get("genre_code", genre_code)
                    gid       = genre_map.get(gc) or genre_map.get("others")
                    platforms = tmdb.get("platforms",[]) or GENRE_PLATFORM_FALLBACK.get(gc,["viu"])
                    row = {
                        "name":            query,
                        "chinese_title":   tmdb.get("chinese_title"),
                        "genre_id":        gid,
                        "platforms":       platforms,
                        "description":     tmdb.get("description",""),
                        "search_term":     tmdb.get("search_term", query),
                        "tmdb_id":         tmdb.get("tmdb_id"),
                        "has_description": bool(tmdb.get("description","").strip()),
                        "is_active":       True,
                        "updated_at":      now_utc(),
                    }
                    try:
                        sb.table("shows_master").insert(row).execute()
                        known_shows.add(query_lower)
                        log.info(f"  ✅ New show added: {query}")
                    except Exception as e:
                        log.warning(f"  ❌ Show insert failed '{query}': {e}")

                elif category == "artists" and query_lower not in known_artists:
                    log.info(f"  New artist candidate: '{query}'")
                    tmdb = tmdb_search_person(query)
                    gc   = tmdb.get("genre_code", genre_code)
                    gid  = genre_map.get(gc) or genre_map.get("others")
                    row  = {
                        "name":            query,
                        "role":            tmdb.get("role","Actor"),
                        "show_name":       tmdb.get("show_name",""),
                        "genre_id":        gid,
                        "search_term":     query,
                        "tmdb_id":         tmdb.get("tmdb_id"),
                        "has_description": bool(tmdb.get("role") and tmdb.get("show_name")),
                        "is_active":       True,
                        "updated_at":      now_utc(),
                    }
                    try:
                        sb.table("artists_master").insert(row).execute()
                        known_artists.add(query_lower)
                        log.info(f"  ✅ New artist added: {query}")
                    except Exception as e:
                        log.warning(f"  ❌ Artist insert failed '{query}': {e}")

            time.sleep(TRENDS_DELAY)

    # ── STEP 2: Discover events from Trends ──────────────────────────────
    log.info("--- Step 2: Discovering events from Trends ---")
    event_terms = build_event_terms(
        sorted(shows,   key=lambda s: s.get("name","")),
        sorted(artists, key=lambda a: a.get("name",""))
    )
    for term in event_terms:
        for query in fetch_related_queries(pytrends, term):
            query_lower = query.lower()
            if query_lower in known_events: continue
            log.info(f"  New event candidate: '{query}'")
            rss   = enrich_event_from_rss(query, rss_entries)
            gid   = genre_map.get("others")
            links = [{"l":"More info","u":rss["link"]}] if rss.get("link") else []
            row   = {
                "title":           query,
                "genre_id":        gid,
                "type":            rss.get("type","Event"),
                "venue":           "",
                "event_date":      "",
                "description":     rss.get("description",""),
                "links":           json.dumps(links),
                "search_term":     query,
                "has_description": bool(rss.get("description","").strip()),
                "is_active":       True,
                "updated_at":      now_utc(),
            }
            try:
                sb.table("events_master").insert(row).execute()
                known_events.add(query_lower)
                log.info(f"  ✅ New event added: {query[:50]}")
            except Exception as e:
                log.warning(f"  ❌ Event insert failed '{query}': {e}")
        time.sleep(TRENDS_DELAY)

    # ── STEP 3: Discover events from RSS ─────────────────────────────────
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

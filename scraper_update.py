"""
Drama Watch SG — scraper_update.py
Runs daily at midnight SGT (16:00 UTC) via GitHub Actions.

Flow:
  0a. Auto-discover new SHOWS from FlixPatrol SG Top 10 TV shows only
  0b. Auto-discover new ARTISTS from TMDB cast of tracked shows
  0c. Auto-discover new EVENTS from Ticketmaster SG
  1.  Google Trends related queries — additional discovery
  2.  Score all active items via Google Trends
  3.  Normalise 0-100 and append history rows
  4.  has_description retry via Wikipedia
  5.  Deactivate past events
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
    "TH": "thai",   "SG": "local",  "JP": "others", "TR": "others", "IN": "others",
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
        "Korean drama Singapore", "Chinese drama Singapore", "Singapore drama",
        "Thai drama Singapore", "Western series Singapore",
    ],
    "artists": [
        "Korean drama artists Singapore", "Chinese drama artists Singapore",
        "Singapore drama artists", "Thai drama artists Singapore",
    ],
}

TERM_TO_GENRE = {
    "Korean drama": "kdrama", "Chinese drama": "cdrama", "Singapore drama": "local",
    "Thai drama": "thai", "Western series": "western", "drama": "others",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Shows: only allow these origin countries from TMDB ────────────────────
# Anything else (US/UK movies, animations, reality) gets rejected
ALLOWED_SHOW_ORIGINS = {"KR", "CN", "TW", "HK", "TH", "SG", "JP", "TR", "IN"}

# Shows: block these even if they pass origin check
BLOCKED_SHOW_KEYWORDS = [
    "movie", "film", "animation", "animated", "cartoon", "anime",
    "documentary", "docu", "reality", "game show", "talk show",
    "stand-up", "standup", "comedy special", "sport", "wrestling",
    "wwe", "formula", "football", "news", "kids", "children",
]

# Artists: only add cast from Asian dramas — skip Western shows
ALLOWED_ARTIST_ORIGINS = {"KR", "CN", "TW", "HK", "TH", "SG", "JP", "TR", "IN"}

# Events: only add if title mentions these keywords (K-pop/drama relevant)
EVENT_ALLOW_KEYWORDS = [
    "kpop", "k-pop", "kdrama", "k-drama", "korean", "cpop", "c-pop",
    "cdrama", "c-drama", "chinese", "thai", "jpop", "j-pop", "japanese",
    "hallyu", "fan meet", "fan meeting", "fansign", "fan sign",
    "showcase", "fan party", "world tour", "asia tour",
    # Kpop group/artist name fragments — checked dynamically
]

# Events: always block these even if they match allow keywords
EVENT_BLOCK_KEYWORDS = [
    "football", "soccer", "basketball", "tennis", "golf", "esport",
    "gaming", "comedy", "stand-up", "standup", "magic", "circus",
    "musical", "opera", "ballet", "orchestra", "classical",
    "conference", "summit", "seminar", "exhibition", "art fair",
]


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def now_sgt():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M SGT")

def term_to_genre_code(term):
    for key, code in TERM_TO_GENRE.items():
        if key.lower() in term.lower():
            return code
    return "others"


# ══════════════════════════════════════════════════════════════════════════
# TMDB + WIKI HELPERS
# ══════════════════════════════════════════════════════════════════════════

def tmdb_search_show(name):
    try:
        res = requests.get(f"{TMDB_BASE}/search/tv",
            params={"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"},
            timeout=10)
        if not res.ok: return {}
        results = res.json().get("results", [])
        if not results: return {}

        show    = results[0]
        tmdb_id = show["id"]

        detail = requests.get(f"{TMDB_BASE}/tv/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "en-SG",
                    "append_to_response": "watch/providers,alternative_titles"},
            timeout=10)
        if not detail.ok:
            return {"tmdb_id": tmdb_id, "description": show.get("overview", "")}

        d      = detail.json()
        origin = d.get("origin_country", [])

        # ── STRICT: reject if not from allowed countries ──────────────────
        if not any(c in ALLOWED_SHOW_ORIGINS for c in origin):
            log.info(f"  Rejected show '{name}' — origin {origin} not in allowed list")
            return {"rejected": True}

        # ── STRICT: reject if media type hints it's a movie/animation ─────
        show_type = d.get("type", "").lower()
        if show_type in ["miniseries"] and not any(c in {"KR","CN","TW","HK","TH","SG"} for c in origin):
            pass  # miniseries from Asia are fine
        genres_tmdb = [g.get("name","").lower() for g in d.get("genres",[])]
        if any(g in ["animation","reality","talk","news","documentary","game show"]
               for g in genres_tmdb):
            log.info(f"  Rejected show '{name}' — TMDB genre {genres_tmdb}")
            return {"rejected": True}

        sg = d.get("watch/providers", {}).get("results", {}).get("SG", {})
        platforms = []
        for p in sg.get("flatrate", []):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms:
                platforms.append(code)

        alt_titles    = d.get("alternative_titles", {}).get("results", [])
        chinese_title = next(
            (t["title"] for t in alt_titles if t.get("iso_3166_1") in ["CN","TW","HK","KR"]),
            None
        )
        genre_code = next(
            (ORIGIN_TO_GENRE[c] for c in origin if c in ORIGIN_TO_GENRE),
            "western"
        )

        return {
            "tmdb_id": tmdb_id, "description": d.get("overview", ""),
            "chinese_title": chinese_title, "platforms": platforms,
            "genre_code": genre_code, "origin": origin,
            "search_term": f"{name} drama",
        }
    except Exception as e:
        log.warning(f"TMDB show error '{name}': {e}")
        return {}

def tmdb_search_person(name):
    try:
        res = requests.get(f"{TMDB_BASE}/search/person",
            params={"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"},
            timeout=10)
        if not res.ok: return {}
        results = res.json().get("results", [])
        if not results: return {}

        person    = results[0]
        known_for = person.get("known_for", [])
        show_name = ""
        genre_code = "others"

        if known_for:
            show      = known_for[0]
            show_name = show.get("name") or show.get("title", "")
            origin    = show.get("origin_country", [])
            genre_code = next(
                (ORIGIN_TO_GENRE[c] for c in origin if c in ORIGIN_TO_GENRE),
                "others"
            )

        role = "Actress" if person.get("gender") == 1 else "Actor"
        return {
            "tmdb_id": person.get("id"), "role": role,
            "show_name": show_name, "genre_code": genre_code, "search_term": name,
        }
    except Exception as e:
        log.warning(f"TMDB person error '{name}': {e}")
        return {}

def wiki_lookup(name):
    try:
        for suffix in ["", "_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(name.replace(' ','_') + suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc      = res.json().get("extract", "")
                sentences = desc.split(". ")
                return {"description": ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")}
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════
# STEP 0a — SHOW DISCOVERY: FlixPatrol SG Top 10 TV only
# ══════════════════════════════════════════════════════════════════════════

FLIXPATROL_PAGES = [
    # TV shows only — use /tv/ path to skip movies
    ("https://flixpatrol.com/top10/netflix/singapore/", "netflix"),
    ("https://flixpatrol.com/top10/disney/singapore/",  "disney"),
]

def scrape_flixpatrol_tv_only(url):
    """
    Scrape FlixPatrol SG — return only TV show names, skip movies section.
    FlixPatrol page has two tables: Movies first, then TV Shows.
    We only want the TV Shows table.
    """
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Find the TV Shows section — it comes after a heading containing "TV"
        tv_section = None
        for heading in soup.find_all(["h2", "h3", "h4"]):
            if "tv" in heading.get_text().lower() or "show" in heading.get_text().lower():
                tv_section = heading
                break

        shows = []
        seen  = set()

        if tv_section:
            # Grab the next table after the TV heading
            table = tv_section.find_next("table")
            if table:
                for a in table.select("a[href^='/title/']"):
                    name = a.get_text(strip=True)
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        shows.append(name)
        else:
            # Fallback: grab all title links but skip obvious movies
            for a in soup.select("table a[href^='/title/']"):
                name = a.get_text(strip=True)
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    shows.append(name)

        return shows[:10]

    except Exception as e:
        log.warning(f"FlixPatrol scrape failed [{url}]: {e}")
        return []

def is_valid_drama(name, tmdb_meta):
    """
    Final validation — only add if:
    1. TMDB confirms it's from an allowed Asian country
    2. TMDB genre is not animation/reality/documentary
    3. Name doesn't contain blocked keywords
    """
    if tmdb_meta.get("rejected"):
        return False
    name_lower = name.lower()
    if any(kw in name_lower for kw in BLOCKED_SHOW_KEYWORDS):
        return False
    # Must have valid origin from TMDB
    origin = tmdb_meta.get("origin", [])
    if tmdb_meta.get("tmdb_id") and not any(c in ALLOWED_SHOW_ORIGINS for c in origin):
        return False
    return True

def discover_shows_from_flixpatrol(sb, genre_map, known_shows):
    log.info("--- Step 0a: FlixPatrol TV show discovery ---")
    candidates = {}
    for url, platform in FLIXPATROL_PAGES:
        names = scrape_flixpatrol_tv_only(url)
        log.info(f"  {platform} candidates: {names}")
        for name in names:
            if name.lower() not in candidates:
                candidates[name] = platform
        time.sleep(3)

    added = 0
    for name, platform in candidates.items():
        if name.lower() in known_shows:
            continue

        # Quick keyword block before hitting TMDB
        if any(kw in name.lower() for kw in BLOCKED_SHOW_KEYWORDS):
            log.info(f"  Blocked (keyword): {name}")
            continue

        log.info(f"  Checking: '{name}' via {platform}")
        meta = tmdb_search_show(name)
        time.sleep(2)

        # Strict validation
        if not is_valid_drama(name, meta):
            log.info(f"  Rejected: {name} (failed validation)")
            continue

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
            # Check if show exists but is inactive — reactivate it
            existing = sb.table("shows_master").select("id,is_active,tmdb_id") \
                .eq("name", name).execute().data or []
            if existing:
                show_rec = existing[0]
                if not show_rec.get("is_active"):
                    sb.table("shows_master").update({
                        "is_active": True, "updated_at": now_utc()
                    }).eq("id", show_rec["id"]).execute()
                    known_shows.add(name.lower())
                    log.info(f"  🔄 Show reactivated: {name}")
                else:
                    known_shows.add(name.lower())
            else:
                # Brand new show — insert and seed artists
                result = sb.table("shows_master").insert(row).execute()
                known_shows.add(name.lower())
                log.info(f"  ✅ Show added: {name} [{gc}]")
                added += 1
                seed_lead_artists(sb, genre_map, known_artists, name, meta.get("tmdb_id"), gid)
        except Exception as e:
            log.warning(f"  Show insert failed '{name}': {e}")

    log.info(f"--- Step 0a: {added} new shows added ---")




# ══════════════════════════════════════════════════════════════════════════
# STEP 0c — EVENT DISCOVERY: Ticketmaster SG (Asian acts only)
# ══════════════════════════════════════════════════════════════════════════

TICKETMASTER_URL = "https://ticketmaster.sg/activity"

def is_relevant_event(title, known_artist_names):
    """
    Only allow events that are clearly K-pop/K-drama/Asian entertainment.
    Block sports, musicals, comedy, conferences etc.
    """
    title_lower = title.lower()

    # Hard block first
    if any(kw in title_lower for kw in EVENT_BLOCK_KEYWORDS):
        return False

    # Must match at least one allow keyword OR a known artist name
    if any(kw in title_lower for kw in EVENT_ALLOW_KEYWORDS):
        return True

    # Check tracked artist names
    for artist_name in known_artist_names:
        if len(artist_name) >= 4 and artist_name in title_lower:
            return True

    return False

def scrape_ticketmaster_sg(known_artist_names):
    log.info("  Scraping Ticketmaster SG...")
    try:
        r = requests.get(TICKETMASTER_URL, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        soup   = BeautifulSoup(r.text, "html.parser")
        events = []
        seen   = set()

        for a in soup.select("a[href*='/activity/detail/']"):
            title = a.get_text(separator=" ", strip=True)
            link  = a.get("href", "")
            if not title or len(title) < 5 or title.lower() in seen:
                continue
            seen.add(title.lower())

            if not is_relevant_event(title, known_artist_names):
                log.info(f"  Skipped event: {title[:60]}")
                continue

            ev_type = next(
                (t for kw, t in EVENT_TYPE_MAP.items() if kw in title.lower()),
                "Concert"
            )

            # Try to find date in parent text
            date_str = ""
            parent   = a.parent
            if parent:
                date_match = re.search(r"\d{1,2}\s+\w+\s+\d{4}", parent.get_text(" ", strip=True))
                if date_match:
                    date_str = date_match.group(0)

            if link and not link.startswith("http"):
                link = "https://ticketmaster.sg" + link

            events.append({
                "title":       title[:120],
                "type":        ev_type,
                "date_str":    date_str,
                "link":        link,
                "description": f"Upcoming event in Singapore: {title}",
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

        title_lower = title.lower()
        gc = "kdrama"
        if any(w in title_lower for w in ["chinese","mandarin","cdrama","c-drama","cpop","c-pop"]):
            gc = "cdrama"
        elif any(w in title_lower for w in ["thai","thailand"]):
            gc = "thai"
        elif any(w in title_lower for w in ["singapore","sg","local","mediacorp"]):
            gc = "local"
        elif any(w in title_lower for w in ["japanese","japan","jpop","j-pop"]):
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
# RSS HELPERS
# ══════════════════════════════════════════════════════════════════════════

def fetch_rss_entries():
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
        title_text   = entry.get("title", "")
        summary_text = entry.get("summary", "")
        full_text    = (title_text + " " + summary_text).lower()
        link         = entry.get("link", "")
        if "singapore" not in full_text:
            continue
        ev_type = next((t for kw, t in EVENT_TYPE_MAP.items() if kw in full_text), None)
        if not ev_type:
            continue
        for name, genre_code in names:
            if len(name) < 4 or name.lower() not in full_text:
                continue
            event_title = title_text[:100].strip()
            search_term = f"{name} Singapore"
            if event_title.lower() in known_events or search_term.lower() in known_events:
                continue
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

def fetch_related_queries(pytrends, term):
    try:
        pytrends.build_payload([term], geo="SG", timeframe="now 7-d")
        related = pytrends.related_queries()
        if term not in related: return []
        top = related[term].get("top")
        if top is None or top.empty: return []
        return top["query"].tolist()[:10]
    except Exception as e:
        log.warning(f"Related queries error '{term}': {e}"); return []

def fetch_yesterday_score(pytrends, term):
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
    if score >= 40: return "Rising" if prev and score > prev + 3 else "Stable"
    return "Fading"

def score_to_trend(score, prev=None):
    if prev is None: return "→"
    if score > prev + 5: return "↑"
    if score < prev - 5: return "↓"
    return "→"

def build_sparkline(prev_sparkline, new_score):
    history = list(prev_sparkline or [])[-6:]
    history.append(round(new_score, 1))
    return history

def get_prev_scores(sb, view, id_field):
    try:
        res = sb.table(view).select(f"{id_field},score_today,sparkline").execute()
        return {
            r[id_field]: {"score": r.get("score_today", 0), "sparkline": r.get("sparkline", [])}
            for r in (res.data or [])
        }
    except Exception as e:
        log.warning(f"Could not fetch prev scores from {view}: {e}"); return {}


def auto_deactivate_zero_shows(sb):
    """
    Deactivate any show that has scored 0 for 30+ consecutive days.
    Keeps the active list lean and prevents timeout from scoring dead shows.
    """
    log.info("--- Auto-deactivating zero-score shows ---")
    try:
        # Get all active shows with their recent history
        shows = sb.table("shows_master").select("id,name").eq("is_active", True).execute().data or []
        deactivated = 0

        for show in shows:
            sid = show["id"]
            # Get last 14 days of scores
            history = sb.table("shows_history") \
                .select("score,recorded_at") \
                .eq("show_id", sid) \
                .order("recorded_at", desc=True) \
                .limit(30) \
                .execute().data or []

            # Only deactivate if we have 14+ days AND all are zero
            if len(history) >= 30 and all(float(h.get("score", 0)) == 0 for h in history):
                sb.table("shows_master").update({
                    "is_active": False, "updated_at": now_utc()
                }).eq("id", sid).execute()
                log.info(f"  Deactivated zero-score show: {show['name']}")
                deactivated += 1

        log.info(f"--- Auto-deactivated {deactivated} zero-score shows ---")
    except Exception as e:
        log.warning(f"Auto-deactivate error: {e}")


def auto_deactivate_zero_artists(sb):
    """
    Deactivate any artist that has scored 0 for 30+ consecutive days.
    """
    log.info("--- Auto-deactivating zero-score artists ---")
    try:
        artists = sb.table("artists_master").select("id,name").eq("is_active", True).execute().data or []
        deactivated = 0
        for artist in artists:
            aid = artist["id"]
            history = sb.table("artists_history") \
                .select("score,recorded_at") \
                .eq("artist_id", aid) \
                .order("recorded_at", desc=True) \
                .limit(30) \
                .execute().data or []
            if len(history) >= 30 and all(float(h.get("score", 0)) == 0 for h in history):
                sb.table("artists_master").update({
                    "is_active": False, "updated_at": now_utc()
                }).eq("id", aid).execute()
                log.info(f"  Deactivated zero-score artist: {artist['name']}")
                deactivated += 1
        log.info(f"--- Auto-deactivated {deactivated} zero-score artists ---")
    except Exception as e:
        log.warning(f"Auto-deactivate artist error: {e}")


def seed_lead_artists(sb, genre_map, known_artists, new_show_name, tmdb_id, genre_id):
    """
    When a new show is added, seed the top 2 lead actors from TMDB.
    Only runs for newly discovered shows — not existing ones.
    """
    if not tmdb_id:
        return
    try:
        r = requests.get(
            f"{TMDB_BASE}/tv/{tmdb_id}/credits",
            params={"api_key": TMDB_API_KEY, "language": "en-SG"},
            timeout=10,
        )
        if not r.ok:
            return
        cast  = r.json().get("cast", [])[:2]  # top 2 leads only
        added = 0
        for person in cast:
            name = person.get("name", "").strip()
            if not name or name.lower() in known_artists:
                continue
            role = "Actress" if person.get("gender") == 1 else "Actor"
            row  = {
                "name":            name,
                "role":            role,
                "show_name":       new_show_name,
                "genre_id":        genre_id,
                "search_term":     name,
                "tmdb_id":         person.get("id"),
                "has_description": True,
                "is_active":       True,
                "updated_at":      now_utc(),
            }
            try:
                sb.table("artists_master").insert(row).execute()
                known_artists.add(name.lower())
                log.info(f"  ✅ Lead artist seeded: {name} ({role} in {new_show_name})")
                added += 1
            except Exception as e:
                if "duplicate" in str(e).lower():
                    known_artists.add(name.lower())
                else:
                    log.warning(f"  Artist seed failed '{name}': {e}")
        time.sleep(1)
    except Exception as e:
        log.warning(f"  Seed artists error for '{new_show_name}': {e}")


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    log.info("=== Drama Watch SG — UPDATE scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}

    shows   = sb.table("shows_master").select("id,name,search_term,tmdb_id,genre_id").eq("is_active", True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,event_date").eq("is_active", True).execute().data or []
    log.info(f"Active: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    known_shows   = {s["name"].lower() for s in shows}
    known_artists = {a["name"].lower() for a in artists}
    known_events  = {e["title"].lower() for e in events}

    # ── STEP 0a: Show discovery ───────────────────────────────────────────
    discover_shows_from_flixpatrol(sb, genre_map, known_shows)

    # ── STEP 0c: Event discovery ──────────────────────────────────────────
    discover_events_from_ticketmaster(sb, genre_map, known_events, known_artists)

    # Reload after discoveries
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,event_date").eq("is_active", True).execute().data or []
    known_shows   = {s["name"].lower() for s in shows}
    known_artists = {a["name"].lower() for a in artists}
    known_events  = {e["title"].lower() for e in events}

    # ── STEP 1: Google Trends additional discovery ────────────────────────
    log.info("--- Step 1: Google Trends additional discovery ---")
    rss_entries = fetch_rss_entries()

    for category, terms in DISCOVERY_TERMS.items():
        for term in terms:
            genre_code = term_to_genre_code(term)
            queries    = fetch_related_queries(pytrends, term)
            for query in queries:
                query_lower = query.lower()

                if category == "shows" and query_lower not in known_shows:
                    meta = tmdb_search_show(query)
                    if not is_valid_drama(query, meta):
                        log.info(f"  Rejected Trends show: {query}")
                        continue
                    if not meta.get("description"):
                        meta["description"] = wiki_lookup(query).get("description", "")
                    gc  = meta.get("genre_code", genre_code)
                    gid = genre_map.get(gc) or genre_map.get("others")
                    row = {
                        "name": query, "chinese_title": meta.get("chinese_title"),
                        "genre_id": gid,
                        "platforms": meta.get("platforms") or GENRE_PLATFORM_FALLBACK.get(gc, ["netflix"]),
                        "description": meta.get("description", ""),
                        "search_term": meta.get("search_term", query),
                        "tmdb_id": meta.get("tmdb_id"),
                        "has_description": bool(meta.get("description", "").strip()),
                        "is_active": True, "updated_at": now_utc(),
                    }
                    try:
                        existing = sb.table("shows_master").select("id,is_active") \
                            .eq("name", query).execute().data or []
                        if existing:
                            if not existing[0].get("is_active"):
                                sb.table("shows_master").update({
                                    "is_active": True, "updated_at": now_utc()
                                }).eq("id", existing[0]["id"]).execute()
                                log.info(f"  🔄 Trends show reactivated: {query}")
                        else:
                            sb.table("shows_master").insert(row).execute()
                            log.info(f"  ✅ Trends show added: {query}")
                        known_shows.add(query_lower)
                    except Exception as e:
                        log.warning(f"  Trends show insert failed: {e}")

                elif category == "artists" and query_lower not in known_artists:
                    meta = tmdb_search_person(query)
                    # Only add if from Asian drama
                    if meta.get("genre_code", "others") == "western":
                        log.info(f"  Rejected Trends artist (Western): {query}")
                        continue
                    gc  = meta.get("genre_code", genre_code)
                    gid = genre_map.get(gc) or genre_map.get("others")
                    row = {
                        "name": query, "role": meta.get("role", "Actor"),
                        "show_name": meta.get("show_name", ""), "genre_id": gid,
                        "search_term": query, "tmdb_id": meta.get("tmdb_id"),
                        "has_description": bool(meta.get("role") and meta.get("show_name")),
                        "is_active": True, "updated_at": now_utc(),
                    }
                    try:
                        existing = sb.table("artists_master").select("id,is_active") \
                            .eq("name", query).execute().data or []
                        if existing:
                            if not existing[0].get("is_active"):
                                sb.table("artists_master").update({
                                    "is_active": True, "updated_at": now_utc()
                                }).eq("id", existing[0]["id"]).execute()
                                log.info(f"  🔄 Trends artist reactivated: {query}")
                        else:
                            sb.table("artists_master").insert(row).execute()
                            log.info(f"  ✅ Trends artist added: {query}")
                        known_artists.add(query_lower)
                    except Exception as e:
                        log.warning(f"  Trends artist insert failed: {e}")

            time.sleep(TRENDS_DELAY)

    # RSS event discovery
    log.info("--- Step 1b: RSS event discovery ---")
    rss_shows   = sb.table("shows_master").select("id,name,genre_id").eq("is_active", True).execute().data or []
    rss_artists = sb.table("artists_master").select("id,name,genre_id").eq("is_active", True).execute().data or []
    for ev in discover_events_from_rss(rss_entries, rss_artists, rss_shows, known_events, genre_map):
        gid   = genre_map.get(ev["genre_code"]) or genre_map.get("others")
        links = [{"l": "More info", "u": ev["link"]}] if ev.get("link") else []
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
    shows   = sb.table("shows_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active", True).execute().data or []

    # ── Auto-deactivate zero-score shows + artists ───────────────────────
    auto_deactivate_zero_shows(sb)
    auto_deactivate_zero_artists(sb)

    # Reload after deactivation
    shows = sb.table("shows_master").select("id,name,search_term").eq("is_active", True).execute().data or []
    log.info(f"  Active shows after cleanup: {len(shows)}")

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
        return {k: round((v / max_score) * 100, 1) for k, v in scores.items()}

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
    recorded_at   = datetime(
        yesterday_sgt.year, yesterday_sgt.month, yesterday_sgt.day,
        4, 0, 0, tzinfo=timezone.utc
    ).isoformat()

    show_rows = []
    for s in shows:
        score = show_norm.get(s["name"], 0)
        prev  = show_prev.get(s["id"], {})
        show_rows.append({
            "show_id": s["id"], "score": score,
            "trends_score": show_raw.get(s["name"], 0),
            "status": score_to_status(score, prev.get("score")),
            "trend":  score_to_trend(score, prev.get("score")),
            "sparkline": build_sparkline(prev.get("sparkline", []), score),
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
            "sparkline": build_sparkline(prev.get("sparkline", []), score),
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
            "sparkline": build_sparkline(prev.get("sparkline", []), score),
            "search_volume": 0, "recorded_at": recorded_at,
        })
    if event_rows:
        sb.table("events_history").insert(event_rows).execute()
        log.info(f"  {len(event_rows)} event history rows inserted")

    # ── STEP 4: Fill missing descriptions ────────────────────────────────
    log.info("--- Step 4: Filling missing descriptions ---")
    for table, name_field in [("shows_master", "name"), ("events_master", "title")]:
        try:
            pending = sb.table(table).select(f"id,{name_field}") \
                .eq("is_active", True).eq("has_description", False).execute().data or []
            for item in pending:
                desc = wiki_lookup(item[name_field]).get("description", "").strip()
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
    now_sgt_dt  = datetime.now(timezone(timedelta(hours=8)))
    past_months = [datetime(now_sgt_dt.year, m, 1).strftime("%b").lower()
                   for m in range(1, now_sgt_dt.month)]
    all_events  = sb.table("events_master").select("id,title,event_date").eq("is_active", True).execute().data or []
    for e in all_events:
        date_str = (e.get("event_date") or "").lower()
        if any(m in date_str for m in past_months):
            sb.table("events_master").update({"is_active": False, "updated_at": now_utc()}).eq("id", e["id"]).execute()
            log.info(f"  Deactivated: {e['title'][:50]}")

    log.info("=== Update complete ===")
    log.info(f"  Shows: {len(show_rows)} | Artists: {len(artist_rows)} | Events: {len(event_rows)}")


if __name__ == "__main__":
    main()

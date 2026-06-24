"""
discover_shows.py
=================
Drop-in auto-discovery module for DramaWatchSG scraper_update.py.

Scrapes FlixPatrol Netflix/Disney+/Viu SG Top 10 daily and
auto-inserts any new TV shows not already in shows_master.

Usage — add one call inside main() in scraper_update.py,
just before Step 2 (scoring):

    from discover_shows import discover_from_streaming
    discover_from_streaming(sb, genre_map, known_shows, log)

No other changes needed.
"""

import re
import time
import requests
import logging
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────

# FlixPatrol pages to scrape — free tier, no login needed
FLIXPATROL_PAGES = [
    ("https://flixpatrol.com/top10/netflix/singapore/",  "netflix"),
    ("https://flixpatrol.com/top10/disney/singapore/",   "disney"),
]

# Viu SG top dramas RSS / page
VIU_RSS = "https://www.viu.com/ott/sg/en-us/home"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Words that mean it's NOT a drama (movies, reality, docs, etc.)
SKIP_KEYWORDS = [
    "documentary", "standup", "stand-up", "comedy special",
    "reality", "game show", "news", "talk show", "variety",
    "animation", "anime", "cartoon",   # anime tracked separately if wanted
    "wwe", "sport", "formula", "football",
]

# Genre detection from title/origin keywords
GENRE_HINTS = [
    (["korean","k-drama","kdrama"],               "kdrama"),
    (["chinese","c-drama","cdrama","hong kong","taiwanese","mandarin"], "cdrama"),
    (["thai","thailand","gmmtv"],                 "thai"),
    (["singapore","sg","mediacorp","channel 8"],  "local"),
    (["japanese","j-drama","jdrama","japan"],     "others"),
    (["turkish","turkey"],                        "others"),
    (["indian","bollywood","hindi","tamil"],      "others"),
]

GENRE_PLATFORM_FALLBACK = {
    "kdrama":  ["netflix"],
    "cdrama":  ["netflix"],
    "thai":    ["netflix"],
    "local":   ["mewatch"],
    "western": ["netflix"],
    "others":  ["netflix"],
}

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_PROVIDER_MAP = {
    8: "netflix", 337: "disney", 96: "iqiyi",
    422: "wetv", 458: "viu", 2018: "mewatch",
    290: "youtube", 167: "gmmtv", 232: "zee5", 119: "amazon",
}

ORIGIN_TO_GENRE = {
    "KR": "kdrama", "CN": "cdrama", "TW": "cdrama",
    "HK": "cdrama", "TH": "thai",   "SG": "local",
    "JP": "others", "TR": "others", "IN": "others",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SCRAPING ──────────────────────────────────────────────────────────────

def scrape_flixpatrol(url: str, platform: str) -> list[str]:
    """
    Scrape FlixPatrol SG Top 10 TV shows page.
    Returns list of show name strings.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        shows = []
        # FlixPatrol renders show names as links inside table rows
        # Pattern: <a href="/title/...">Show Name</a>
        for a in soup.select("table a[href^='/title/']"):
            name = a.get_text(strip=True)
            if name and len(name) > 1:
                shows.append(name)

        # Deduplicate preserving order
        seen = set()
        unique = []
        for s in shows:
            if s.lower() not in seen:
                seen.add(s.lower())
                unique.append(s)

        return unique[:10]  # Top 10 only

    except Exception as e:
        logging.warning(f"FlixPatrol scrape failed [{url}]: {e}")
        return []


# ── TMDB ENRICHMENT ───────────────────────────────────────────────────────

def tmdb_enrich(name: str, tmdb_api_key: str, genre_map: dict) -> dict:
    """
    Look up show on TMDB to get genre, platforms, description, chinese title.
    Falls back to keyword-based genre detection.
    """
    if not tmdb_api_key:
        return _fallback_enrich(name, genre_map)

    try:
        # Search
        r = requests.get(
            f"{TMDB_BASE}/search/tv",
            params={"api_key": tmdb_api_key, "query": name, "language": "en-SG"},
            timeout=10,
        )
        if not r.ok or not r.json().get("results"):
            return _fallback_enrich(name, genre_map)

        result = r.json()["results"][0]
        tmdb_id = result["id"]

        # Detail with providers + alt titles
        d = requests.get(
            f"{TMDB_BASE}/tv/{tmdb_id}",
            params={
                "api_key": tmdb_api_key,
                "language": "en-SG",
                "append_to_response": "watch/providers,alternative_titles",
            },
            timeout=10,
        ).json()

        # Genre from origin country
        origin = d.get("origin_country", [])
        genre_code = "western"
        for country, code in ORIGIN_TO_GENRE.items():
            if country in origin:
                genre_code = code
                break

        genre_id = genre_map.get(genre_code) or genre_map.get("others")

        # SG platforms
        sg = d.get("watch/providers", {}).get("results", {}).get("SG", {})
        platforms = []
        for p in sg.get("flatrate", []):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms:
                platforms.append(code)
        if not platforms:
            platforms = GENRE_PLATFORM_FALLBACK.get(genre_code, ["netflix"])

        # Chinese/Korean title
        alt_titles = d.get("alternative_titles", {}).get("results", [])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN", "TW", "HK", "KR"]:
                chinese_title = t.get("title")
                break

        description = d.get("overview", "").strip()

        return {
            "tmdb_id":      tmdb_id,
            "genre_id":     genre_id,
            "genre_code":   genre_code,
            "platforms":    platforms,
            "description":  description,
            "chinese_title": chinese_title,
            "has_description": bool(description),
            "search_term":  f"{name} drama",
        }

    except Exception as e:
        logging.warning(f"TMDB enrich failed for '{name}': {e}")
        return _fallback_enrich(name, genre_map)


def _fallback_enrich(name: str, genre_map: dict) -> dict:
    """Keyword-based genre fallback when TMDB is unavailable."""
    name_lower = name.lower()
    genre_code = "western"
    for keywords, code in GENRE_HINTS:
        if any(kw in name_lower for kw in keywords):
            genre_code = code
            break
    genre_id = genre_map.get(genre_code) or genre_map.get("others")
    return {
        "tmdb_id":       None,
        "genre_id":      genre_id,
        "genre_code":    genre_code,
        "platforms":     GENRE_PLATFORM_FALLBACK.get(genre_code, ["netflix"]),
        "description":   "",
        "chinese_title": None,
        "has_description": False,
        "search_term":   f"{name} drama",
    }


def _looks_like_drama(name: str) -> bool:
    """Quick filter to skip obvious non-dramas."""
    name_lower = name.lower()
    return not any(kw in name_lower for kw in SKIP_KEYWORDS)


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────

def discover_from_streaming(sb, genre_map: dict, known_shows: set,
                            log, tmdb_api_key: str = None) -> list:
    """
    Scrape FlixPatrol SG Top 10, find shows not in shows_master,
    enrich via TMDB and insert.

    Returns list of newly added show names.
    """
    log.info("--- Auto-discovery: FlixPatrol SG Top 10 ---")

    candidates = {}  # name -> platform

    for url, platform in FLIXPATROL_PAGES:
        names = scrape_flixpatrol(url, platform)
        log.info(f"  {platform}: found {len(names)} shows — {names}")
        for name in names:
            if name.lower() not in candidates:
                candidates[name] = platform
        time.sleep(3)

    added = []

    for name, platform in candidates.items():
        name_lower = name.lower()

        # Skip if already tracked
        if name_lower in known_shows:
            log.info(f"  Already tracked: {name}")
            continue

        # Skip non-dramas
        if not _looks_like_drama(name):
            log.info(f"  Skipping non-drama: {name}")
            continue

        log.info(f"  New show discovered: '{name}' (via {platform})")

        # Enrich via TMDB
        meta = tmdb_enrich(name, tmdb_api_key, genre_map)
        time.sleep(2)

        row = {
            "name":          name,
            "chinese_title": meta.get("chinese_title"),
            "genre_id":      meta["genre_id"],
            "platforms":     meta["platforms"],
            "description":   meta.get("description", ""),
            "search_term":   meta["search_term"],
            "tmdb_id":       meta.get("tmdb_id"),
            "has_description": meta["has_description"],
            "is_active":     True,
            "updated_at":    now_utc(),
            # created_at left to DB default = NOW()
        }

        try:
            sb.table("shows_master").insert(row).execute()
            known_shows.add(name_lower)
            added.append(name)
            log.info(f"  ✅ Auto-added: {name} [{meta['genre_code']}] on {meta['platforms']}")
        except Exception as e:
            if "duplicate" in str(e).lower():
                known_shows.add(name_lower)
                log.info(f"  Already exists (race): {name}")
            else:
                log.warning(f"  Insert failed for '{name}': {e}")

    log.info(f"--- Auto-discovery complete: {len(added)} new shows added ---")
    return added

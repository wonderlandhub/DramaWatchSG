"""
Drama Watch SG — scraper_update.py

Runs daily at midnight SGT (16:00 UTC) via GitHub Actions.

Flow:
1. Score all active seeds via Google Trends SG
2. Normalise scores 0-100
3. Write history rows
4. Fill missing descriptions via Wikipedia
5. Deactivate past events
6. Auto-deactivate seeds with no SG interest for 30+ days
   (newly added seeds < 30 days old are protected from deactivation)
"""

import os, time, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pytrends.request import TrendReq
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WIKI_API     = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKI_HEADERS = {"User-Agent": "DramaWatchSG/1.0"}

TRENDS_DELAY  = 8   # seconds between each Trends call
NEW_SEED_DAYS = 30  # protect seeds added within this many days from auto-deactivation


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_sgt() -> str:
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%Y-%m-%d %H:%M SGT")

def days_since(created_at: str) -> int:
    """Return number of days since created_at timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).days
    except:
        return 999  # unknown — treat as old

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

def build_sparkline(prev_sparkline: list, new_score: float) -> list:
    history = list(prev_sparkline or [])[-6:]
    history.append(round(new_score, 1))
    return history

def get_prev_scores(sb, view: str, id_field: str) -> dict:
    try:
        res = sb.table(view).select(f"{id_field},score_today,sparkline").execute()
        return {
            r[id_field]: {
                "score":     r.get("score_today", 0),
                "sparkline": r.get("sparkline", []),
            }
            for r in (res.data or [])
        }
    except Exception as e:
        log.warning(f"Could not fetch prev scores from {view}: {e}"); return {}

def wiki_lookup(name: str) -> str:
    try:
        wiki_name = name.replace(" ", "_")
        for suffix in ["", "_TV_series"]:
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name+suffix)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc      = res.json().get("extract", "")
                sentences = desc.split(". ")
                return ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
        return ""
    except Exception as e:
        log.warning(f"Wikipedia error '{name}': {e}"); return ""


# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

def fetch_yesterday_score(pytrends, term: str) -> float:
    """
    Fetch yesterday's Google Trends SG score for a term.
    Uses 'today 1-m' (30 daily points). iloc[-2] = yesterday's complete day.
    Returns 0.0 on any error or 429.
    """
    try:
        pytrends.build_payload([term], geo="SG", timeframe="today 1-m")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns: return 0.0
        if len(df) < 2: return float(df[term].iloc[-1])
        return float(df[term].iloc[-2])
    except Exception as e:
        log.warning(f"Score error '{term}': {e}"); return 0.0


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — UPDATE scraper ===")
    log.info(f"  Run time: {now_sgt()}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # Load all active seeds
    shows   = sb.table("shows_master").select("id,name,search_term,created_at").eq("is_active", True).execute().data or []
    artists = sb.table("artists_master").select("id,name,search_term,created_at").eq("is_active", True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term,created_at").eq("is_active", True).execute().data or []
    log.info(f"Active seeds: {len(shows)} shows, {len(artists)} artists, {len(events)} events")

    # ── STEP 1: Score shows via Google Trends SG ──────────────────────────
    log.info("--- Step 1: Scoring shows ---")
    show_raw = {}
    for s in shows:
        term            = s.get("search_term") or s["name"]
        score           = fetch_yesterday_score(pytrends, term)
        show_raw[s["name"]] = score
        age             = days_since(s.get("created_at",""))
        log.info(f"    {s['name']}: {score:.1f}  (added {age}d ago)")
        time.sleep(TRENDS_DELAY)

    # ── STEP 2: Score artists via Google Trends SG ────────────────────────
    log.info("--- Step 2: Scoring artists ---")
    artist_raw = {}
    for a in artists:
        term              = a.get("search_term") or a["name"]
        score             = fetch_yesterday_score(pytrends, term)
        artist_raw[a["name"]] = score
        age               = days_since(a.get("created_at",""))
        log.info(f"    {a['name']}: {score:.1f}  (added {age}d ago)")
        time.sleep(TRENDS_DELAY)

    # ── STEP 3: Score events via Google Trends SG ─────────────────────────
    log.info("--- Step 3: Scoring events ---")
    event_raw = {}
    for e in events:
        term               = e.get("search_term") or e["title"]
        score              = fetch_yesterday_score(pytrends, term)
        event_raw[e["title"]] = score
        log.info(f"    {e['title'][:40]}: {score:.1f}")
        time.sleep(TRENDS_DELAY)

    # ── Normalise 0-100 across all active seeds ───────────────────────────
    all_raw   = {**show_raw, **artist_raw, **event_raw}
    max_score = max(all_raw.values()) if all_raw else 1
    if max_score == 0: max_score = 1

    def normalise(scores: dict) -> dict:
        return {k: round((v / max_score) * 100, 1) for k, v in scores.items()}

    show_norm   = normalise(show_raw)
    artist_norm = normalise(artist_raw)
    event_norm  = normalise(event_raw)

    show_prev   = get_prev_scores(sb, "shows_scores",   "id")
    artist_prev = get_prev_scores(sb, "artists_scores", "id")
    event_prev  = get_prev_scores(sb, "events_scores",  "id")

    # ── STEP 4: Append history rows ───────────────────────────────────────
    log.info("--- Step 4: Appending history rows ---")
    sgt           = timezone(timedelta(hours=8))
    yesterday_sgt = (datetime.now(sgt) - timedelta(days=1)).date()
    recorded_at   = datetime(
        yesterday_sgt.year, yesterday_sgt.month, yesterday_sgt.day,
        4, 0, 0, tzinfo=timezone.utc
    ).isoformat()

    show_rows = []
    for s in shows:
        name  = s["name"]; sid = s["id"]
        score = show_norm.get(name, 0); prev = show_prev.get(sid, {})
        show_rows.append({
            "show_id":       sid,   "score": score,
            "trends_score":  show_raw.get(name, 0),
            "status":        score_to_status(score, prev.get("score")),
            "trend":         score_to_trend(score, prev.get("score")),
            "sparkline":     build_sparkline(prev.get("sparkline", []), score),
            "search_volume": 0,     "recorded_at": recorded_at,
        })
    if show_rows:
        sb.table("shows_history").insert(show_rows).execute()
        log.info(f"  Inserted {len(show_rows)} show history rows")

    artist_rows = []
    for a in artists:
        name  = a["name"]; aid = a["id"]
        score = artist_norm.get(name, 0); prev = artist_prev.get(aid, {})
        artist_rows.append({
            "artist_id":     aid,   "score": score,
            "trends_score":  artist_raw.get(name, 0),
            "status":        score_to_status(score, prev.get("score")),
            "trend":         score_to_trend(score, prev.get("score")),
            "sparkline":     build_sparkline(prev.get("sparkline", []), score),
            "search_volume": 0,     "recorded_at": recorded_at,
        })
    if artist_rows:
        sb.table("artists_history").insert(artist_rows).execute()
        log.info(f"  Inserted {len(artist_rows)} artist history rows")

    event_rows = []
    for e in events:
        title = e["title"]; eid = e["id"]
        score = event_norm.get(title, 0); prev = event_prev.get(eid, {})
        event_rows.append({
            "event_id":      eid,   "score": score,
            "trends_score":  event_raw.get(title, 0),
            "status":        "Upcoming",
            "trend":         score_to_trend(score, prev.get("score")),
            "sparkline":     build_sparkline(prev.get("sparkline", []), score),
            "search_volume": 0,     "recorded_at": recorded_at,
        })
    if event_rows:
        sb.table("events_history").insert(event_rows).execute()
        log.info(f"  Inserted {len(event_rows)} event history rows")

    # ── STEP 5: Fill missing descriptions via Wikipedia ───────────────────
    log.info("--- Step 5: Filling missing descriptions ---")
    for table, name_field in [("shows_master", "name"), ("events_master", "title")]:
        try:
            pending = sb.table(table).select(f"id,{name_field}")\
                       .eq("is_active", True).eq("has_description", False)\
                       .execute().data or []
            log.info(f"  {table}: {len(pending)} missing description")
            for item in pending:
                name = item[name_field]
                desc = wiki_lookup(name)
                if desc:
                    sb.table(table).update({
                        "description":     desc,
                        "has_description": True,
                        "updated_at":      now_utc(),
                    }).eq("id", item["id"]).execute()
                    log.info(f"  ✅ Description filled: {name}")
                else:
                    log.info(f"  ⏳ Still pending: {name}")
                time.sleep(0.5)
        except Exception as e:
            log.warning(f"  has_description retry error for {table}: {e}")

    # ── STEP 6: Deactivate past events ───────────────────────────────────
    log.info("--- Step 6: Deactivating past events ---")
    now_sgt_dt  = datetime.now(timezone(timedelta(hours=8)))
    past_months = [
        datetime(now_sgt_dt.year, m, 1).strftime("%b").lower()
        for m in range(1, now_sgt_dt.month)
    ]
    all_events = sb.table("events_master").select("id,title,event_date")\
                  .eq("is_active", True).execute().data or []
    for e in all_events:
        date_str = (e.get("event_date") or "").lower()
        if any(m in date_str for m in past_months):
            sb.table("events_master").update({"is_active": False, "updated_at": now_utc()})\
              .eq("id", e["id"]).execute()
            log.info(f"  Deactivated past event: {e['title'][:50]}")

    # ── STEP 7: Auto-deactivate zero-interest seeds ───────────────────────
    # Rules:
    #   - score_7d = 0 AND score_30d = 0 AND sparkline has 5+ consecutive zeros
    #   - BUT protect seeds added within last 30 days — they haven't had time to trend yet
    log.info("--- Step 7: Auto-deactivating zero-interest seeds ---")

    try:
        all_show_scores = sb.table("shows_scores")\
            .select("id,score_today,score_7d,score_30d,sparkline,created_at")\
            .execute().data or []

        deactivate_shows = []
        for s in all_show_scores:
            age = days_since(s.get("created_at", ""))
            if age < NEW_SEED_DAYS:
                log.info(f"  ⏭  Show id={s['id']} — protected (added {age}d ago)")
                continue
            if (s.get("score_7d") or 0) == 0 \
            and (s.get("score_30d") or 0) == 0 \
            and len(s.get("sparkline") or []) >= 5 \
            and all(v == 0 for v in (s.get("sparkline") or [])):
                deactivate_shows.append(s["id"])

        if deactivate_shows:
            sb.table("shows_master").update({"is_active": False, "updated_at": now_utc()})\
              .in_("id", deactivate_shows).execute()
            log.info(f"  Deactivated {len(deactivate_shows)} zero-interest shows")
        else:
            log.info("  No shows to deactivate")
    except Exception as e:
        log.warning(f"  Show auto-deactivate error: {e}")

    try:
        all_artist_scores = sb.table("artists_scores")\
            .select("id,score_today,score_7d,score_30d,sparkline,created_at")\
            .execute().data or []

        deactivate_artists = []
        for a in all_artist_scores:
            age = days_since(a.get("created_at", ""))
            if age < NEW_SEED_DAYS:
                log.info(f"  ⏭  Artist id={a['id']} — protected (added {age}d ago)")
                continue
            if (a.get("score_7d") or 0) == 0 \
            and (a.get("score_30d") or 0) == 0 \
            and len(a.get("sparkline") or []) >= 5 \
            and all(v == 0 for v in (a.get("sparkline") or [])):
                deactivate_artists.append(a["id"])

        if deactivate_artists:
            sb.table("artists_master").update({"is_active": False, "updated_at": now_utc()})\
              .in_("id", deactivate_artists).execute()
            log.info(f"  Deactivated {len(deactivate_artists)} zero-interest artists")
        else:
            log.info("  No artists to deactivate")
    except Exception as e:
        log.warning(f"  Artist auto-deactivate error: {e}")

    log.info("=== Update complete ===")
    log.info(f"  Shows scored:   {len(show_rows)}")
    log.info(f"  Artists scored: {len(artist_rows)}")
    log.info(f"  Events scored:  {len(event_rows)}")


if __name__ == "__main__":
    main()

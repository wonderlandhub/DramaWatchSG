"""
One-off: discover new events via dynamic event-terms (same logic as
scraper_update.py Step 1b), then backfill their historical Google Trends
scores (Jan 1 -> today) into events_history, normalised the same way as
scraper_init.py.

Run once after deploying the updated scraper_update.py to seed history
for any newly-discovered events so they show up on charts immediately
instead of waiting weeks to accumulate daily rows.
"""
import os, json, time
from datetime import datetime, timezone
from supabase import create_client
from pytrends.request import TrendReq

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TRENDS_DELAY = 1.5

EVENT_SUFFIXES = [
    "Singapore concert","Singapore fan meet","Singapore tour",
    "Singapore fan meeting","Singapore showcase",
]

def build_event_terms(shows, artists, limit=8):
    terms = []
    names = [a["name"] for a in artists[:6]] + [s["name"] for s in shows[:4]]
    for i, name in enumerate(names):
        suffix = EVENT_SUFFIXES[i % len(EVENT_SUFFIXES)]
        terms.append(f"{name} {suffix}")
    return terms[:limit]

def fetch_related_queries(pytrends, term):
    try:
        pytrends.build_payload([term], geo="SG", timeframe="now 7-d")
        related = pytrends.related_queries()
        top = related.get(term, {}).get("top")
        if top is None or top.empty: return []
        return top["query"].tolist()[:10]
    except Exception as e:
        print(f"  related_queries error '{term}': {e}")
        return []

def fetch_daily_scores(pytrends, term):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pytrends.build_payload([term], geo="SG", timeframe=f"2026-01-01 {today}")
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns: return []
        return [{"date": ts.date(), "score": float(df.loc[ts, term])} for ts in df.index]
    except Exception as e:
        print(f"  Trends error '{term}': {e}")
        return []

def build_sparkline(scores):
    return list(scores)[-7:]

def score_to_trend(score, prev):
    if prev is None: return "→"
    if score > prev+5: return "↑"
    if score < prev-5: return "↓"
    return "→"

def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10,25))

    genres = {g["code"]: g["id"] for g in sb.table("genres").select("id,code").execute().data}
    shows   = sb.table("shows_master").select("id,name").eq("is_active",True).execute().data or []
    artists = sb.table("artists_master").select("id,name").eq("is_active",True).execute().data or []
    events  = sb.table("events_master").select("id,title,search_term").eq("is_active",True).execute().data or []
    known_events = {e["title"].lower() for e in events}

    print(f"Existing events: {len(events)}")

    # ── Discover new events ──────────────────────────────────────────
    sorted_artists = sorted(artists, key=lambda a: a["name"])
    sorted_shows   = sorted(shows, key=lambda s: s["name"])
    terms = build_event_terms(sorted_shows, sorted_artists)
    new_titles = []

    for term in terms:
        queries = fetch_related_queries(pytrends, term)
        for query in [term] + queries:
            ql = query.lower()
            if ql in known_events:
                continue
            gid = genres.get("others")
            row = {
                "title": query, "genre_id": gid, "type": "Event",
                "venue": "", "event_date": "", "description": "",
                "links": json.dumps([]), "search_term": query,
                "has_description": False, "is_active": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                res = sb.table("events_master").insert(row).execute()
                eid = res.data[0]["id"]
                known_events.add(ql)
                new_titles.append((eid, query))
                print(f"  ✅ New event: {query}")
            except Exception as e:
                print(f"  insert failed '{query}': {e}")
        time.sleep(TRENDS_DELAY)

    if not new_titles:
        print("No new events discovered — nothing to backfill.")
        return

    # ── Backfill history for new events ──────────────────────────────
    print(f"\nBackfilling history for {len(new_titles)} new events...")
    for eid, title in new_titles:
        term = title
        daily = fetch_daily_scores(pytrends, term)
        if not daily:
            print(f"  {title}: no Trends data, skipping history")
            time.sleep(TRENDS_DELAY)
            continue
        mx = max(d["score"] for d in daily) or 1
        scores_so_far, rows, prev = [], [], None
        for d in daily:
            norm = round((d["score"]/mx)*100, 1)
            scores_so_far.append(norm)
            rows.append({
                "event_id": eid, "score": norm, "trends_score": round(d["score"],1),
                "status": "Upcoming", "trend": score_to_trend(norm, prev),
                "sparkline": build_sparkline(scores_so_far), "search_volume": 0,
                "recorded_at": datetime.combine(d["date"], datetime.min.time()).replace(tzinfo=timezone.utc).isoformat(),
            })
            prev = norm
        if rows:
            sb.table("events_history").insert(rows).execute()
            print(f"  {title}: {len(rows)} history rows inserted")
        time.sleep(TRENDS_DELAY)

if __name__ == "__main__":
    main()

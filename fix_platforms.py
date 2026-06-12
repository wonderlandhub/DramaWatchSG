"""
Fix missing platforms for shows_master using JustWatch (real SG availability).

JustWatch covers Viu, WeTV, iQIYI, meWatch, Netflix, Disney+ etc for Singapore —
far better than TMDB for Asian dramas.

Run: pip install simplejustwatchapi
     python3 fix_platforms.py
Requires: SUPABASE_URL, SUPABASE_SERVICE_KEY env vars
"""
import os, time, requests
from simplejustwatchapi.justwatch import search

SB_URL  = os.environ.get("SUPABASE_URL")
SB_KEY  = os.environ.get("SUPABASE_SERVICE_KEY")
HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}

# JustWatch provider name -> our PP keys (see index.html PP object)
JW_TO_PP = {
    "netflix": "netflix",
    "disney plus": "disney",
    "viu": "viu",
    "wetv": "wetv",
    "iq.": "iqiyi", "iqiyi": "iqiyi",
    "amazon prime video": "amazon",
    "youtube": "youtube",
    "mewatch": "mewatch",
    "zee5": "zee5",
}

# Last-resort fallback ONLY if JustWatch also returns nothing
GENRE_FALLBACK = {
    "kdrama": ["viu"], "cdrama": ["wetv","iqiyi"], "thai": ["viu","gmmtv"],
    "local": ["mewatch"], "western": ["netflix"], "others": ["viu"],
}

def justwatch_sg_platforms(title):
    try:
        results = search(title, "SG", "en", count=1, best_only=True)
        if not results:
            return set()
        offers = results[0].offers or []
        names = set()
        for o in offers:
            pname = (o.package.name or "").lower()
            for k, v in JW_TO_PP.items():
                if k in pname:
                    names.add(v)
        return names
    except Exception as e:
        print(f"  JustWatch error for {title}: {e}")
        return set()

def main():
    shows = requests.get(f"{SB_URL}/rest/v1/shows_master?select=id,name,search_term,genre_id,platforms",
                          headers=HEADERS).json()
    genres = {g["id"]: g["code"] for g in
              requests.get(f"{SB_URL}/rest/v1/genres?select=id,code", headers=HEADERS).json()}

    for s in shows:
        if s.get("platforms"):
            continue
        title = s["search_term"] or s["name"]
        platforms = justwatch_sg_platforms(title)
        source = "JustWatch"
        if not platforms:
            gc = genres.get(s["genre_id"], "others")
            platforms = set(GENRE_FALLBACK.get(gc, ["viu"]))
            source = "fallback"
        plats = sorted(platforms)
        resp = requests.patch(f"{SB_URL}/rest/v1/shows_master?id=eq.{s['id']}",
                               headers=HEADERS, json={"platforms": plats})
        status = "OK" if resp.status_code in (200,204) else f"FAIL {resp.status_code}"
        print(f"{s['name']:40s} -> {plats}  ({source}) [{status}]")
        time.sleep(0.5)

if __name__ == "__main__":
    main()

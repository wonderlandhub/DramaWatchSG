"""
Fix missing platforms for shows_master using JustWatch's public GraphQL API
(direct request — no extra package needed).
"""
import os, time, requests

SB_URL  = os.environ.get("SUPABASE_URL")
SB_KEY  = os.environ.get("SUPABASE_SERVICE_KEY")
HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}

JW_URL = "https://apis.justwatch.com/graphql"
JW_TO_PP = {
    "netflix": "netflix", "disney plus": "disney", "viu": "viu",
    "wetv": "wetv", "iq.": "iqiyi", "iqiyi": "iqiyi",
    "amazon prime video": "amazon", "youtube": "youtube",
    "mewatch": "mewatch", "zee5": "zee5",
}
GENRE_FALLBACK = {
    "kdrama": ["viu"], "cdrama": ["wetv","iqiyi"], "thai": ["viu","gmmtv"],
    "local": ["mewatch"], "western": ["netflix"], "others": ["viu"],
}

SEARCH_QUERY = """
query GetSearchTitles($searchTitlesFilter: TitleFilter!, $country: Country!, $language: Language!) {
  popularTitles(country: $country, filter: $searchTitlesFilter, first: 1) {
    edges {
      node {
        offers(country: $country, platform: WEB) {
          package { clearName }
        }
      }
    }
  }
}
"""

def justwatch_sg_platforms(title):
    try:
        payload = {
            "query": SEARCH_QUERY,
            "variables": {
                "searchTitlesFilter": {"searchQuery": title},
                "country": "SG",
                "language": "en"
            }
        }
        r = requests.post(JW_URL, json=payload, timeout=10)
        r.raise_for_status()
        edges = r.json()["data"]["popularTitles"]["edges"]
        if not edges:
            return set()
        offers = edges[0]["node"].get("offers", [])
        names = set()
        for o in offers:
            pname = (o["package"]["clearName"] or "").lower()
            for k, v in JW_TO_PP.items():
                if k in pname:
                    names.add(v)
        return names
    except Exception as e:
        print(f"  JustWatch error for {title}: {e}")
        return set()

def main():
    if not SB_URL or not SB_KEY:
        print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY env var is missing/empty")
        return

    shows_resp = requests.get(f"{SB_URL}/rest/v1/shows_master?select=id,name,search_term,genre_id,platforms",
                               headers=HEADERS)
    genres_resp = requests.get(f"{SB_URL}/rest/v1/genres?select=id,code", headers=HEADERS)

    if shows_resp.status_code != 200:
        print(f"ERROR fetching shows_master: {shows_resp.status_code} {shows_resp.text[:300]}")
        return
    if genres_resp.status_code != 200:
        print(f"ERROR fetching genres: {genres_resp.status_code} {genres_resp.text[:300]}")
        return

    shows = shows_resp.json()
    genres = {g["id"]: g["code"] for g in genres_resp.json()}

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

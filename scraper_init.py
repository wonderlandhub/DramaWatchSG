"""
Drama Watch SG — scraper_init.py
Run ONCE via GitHub Actions to bootstrap the database.

Revised approach:
- Seed list of known 2026 SG drama shows/artists/events
- Google Trends direct scoring (reliable) — not related_queries
- TMDB fills all content — description, Chinese title, platforms
- RSS discovers any additional shows from news articles
- Fetches daily scores from 1st Jan to today (~160+ days)
- Inserts ~30 history rows per item — real sparklines from day one
- Genre ranking derived from actual search scores

Environment variables (GitHub Actions secrets):
  SUPABASE_URL
  SUPABASE_KEY   — service_role key
  TMDB_API_KEY   — free from themoviedb.org
"""

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import feedparser
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
TMDB_BASE    = "https://api.themoviedb.org/3"
WIKI_API     = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKI_HEADERS = {"User-Agent": "DramaWatchSG/1.0"}
TRENDS_DELAY = 8  # seconds between Google Trends calls — be polite

# SG streaming provider mapping from TMDB provider IDs
TMDB_PROVIDER_MAP = {
    8:    "netflix",
    337:  "disney",
    96:   "iqiyi",
    422:  "wetv",
    458:  "viu",
    2018: "mewatch",
    290:  "youtube",
    167:  "gmmtv",
    232:  "zee5",
    119:  "amazon",
}

# Genre colours — assigned by rank
GENRE_COLORS = [
    {"dot_color":"#7F77DD","bg_color":"#EEEDFE","text_color":"#3C3489"},  # rank 1
    {"dot_color":"#D85A30","bg_color":"#FAECE7","text_color":"#993C1D"},  # rank 2
    {"dot_color":"#1D9E75","bg_color":"#E1F5EE","text_color":"#085041"},  # rank 3
    {"dot_color":"#BA7517","bg_color":"#FAEEDA","text_color":"#633806"},  # rank 4
    {"dot_color":"#378ADD","bg_color":"#E6F1FB","text_color":"#0C447C"},  # rank 5
    {"dot_color":"#9C27B0","bg_color":"#F3E5F5","text_color":"#4A148C"},  # others
]

GENRE_LABELS = {
    "kdrama":   "K-Drama",
    "cdrama":   "C-Drama",
    "local":    "Local",
    "thai":     "Thai",
    "western":  "Western",
    "japanese": "J-Drama",
    "turkish":  "Turkish",
    "indian":   "Indian",
    "others":   "Others",
}

# RSS feeds for additional show discovery
RSS_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
    "https://mothership.sg/feed/",
    "https://8world.com/rss",
]

# ── 2026 SEED DATA ────────────────────────────────────────────────────────
# Sources: Her World SG, Harper's Bazaar SG, Singapore Women's Weekly,
#          Star Awards 2026, Disney+ SG, Netflix SG
# search_term = what SG people search on Google for this show
# tmdb_id = TMDB TV show ID for accurate content lookup
# context = SG-specific one-liner (only we can write this)

SEED_SHOWS = [

    # ── K-DRAMA 2026 ──────────────────────────────────────────────────────
    # Sources: Her World SG, Singapore Women's Weekly, Harper's Bazaar SG

    # Currently airing / recently finished
    {
        "name":        "Perfect Crown",
        "genre":       "kdrama",
        "context":     "IU and Byeon Woo-seok — Disney+ biggest K-Drama premiere 2026",
        "search_term": "Perfect Crown",
        "tmdb_id":     None,
    },
    {
        "name":        "Tempest",
        "genre":       "kdrama",
        "context":     "Gianna Jun and Gang Dong-won espionage thriller on Disney+",
        "search_term": "Tempest",
        "tmdb_id":     None,
    },
    {
        "name":        "My Royal Nemesis",
        "genre":       "kdrama",
        "context":     "Lim Ji-yeon time-slip enemies-to-lovers on Netflix SG",
        "search_term": "My Royal Nemesis",
        "tmdb_id":     None,
    },
    {
        "name":        "The Remarried Empress",
        "genre":       "kdrama",
        "context":     "Shin Min-A and Hyun Bin fantasy romance — coming to Disney+",
        "search_term": "The Remarried Empress",
        "tmdb_id":     None,
    },
    {
        "name":        "Bloodhounds Season 2",
        "genre":       "kdrama",
        "context":     "Action sequel on Disney+ — SG fans of Season 1 waiting",
        "search_term": "Bloodhounds Season 2",
        "tmdb_id":     None,
    },
    {
        "name":        "Bad Guys Reign of Chaos",
        "genre":       "kdrama",
        "context":     "Ma Dong-Seok crime action on Viu SG",
        "search_term": "Bad Guys Reign of Chaos",
        "tmdb_id":     None,
    },
    {
        "name":        "Karma",
        "genre":       "kdrama",
        "context":     "Park Hae-soo slow-burn thriller — gaining SG traction",
        "search_term": "Karma",
        "tmdb_id":     None,
    },
    {
        "name":        "When Life Gives You Tangerines",
        "genre":       "kdrama",
        "context":     "IU heartwarming drama — actively discussed in SG on Netflix",
        "search_term": "When Life Gives You Tangerines",
        "tmdb_id":     None,
    },
    {
        "name":        "The Wonderfools",
        "genre":       "kdrama",
        "context":     "Cha Eun-woo Netflix drama — premiered May 15 2026",
        "search_term": "The Wonderfools",
        "tmdb_id":     None,
    },
    {
        "name":        "Slowly Intensely",
        "genre":       "kdrama",
        "context":     "Song Hye-kyo and Gong Yoo Netflix reunion — most anticipated 2026",
        "search_term": "Slowly Intensely",
        "tmdb_id":     None,
    },
    {
        "name":        "Study Group",
        "genre":       "kdrama",
        "context":     "Hwang Min-hyun action webtoon drama on Viu",
        "search_term": "Study Group",
        "tmdb_id":     None,
    },
    {
        "name":        "Bogota City of the Lost",
        "genre":       "kdrama",
        "context":     "Song Joong-ki Netflix drama — crime thriller set in Colombia",
        "search_term": "Bogota City of the Lost",
        "tmdb_id":     None,
    },
    {
        "name":        "The Koreans",
        "genre":       "kdrama",
        "context":     "Lee Byung-hun and Han Ji-min spy drama on Disney+",
        "search_term": "The Koreans drama",
        "tmdb_id":     None,
    },
    {
        "name":        "Squid Game Season 2",
        "genre":       "kdrama",
        "context":     "Global phenomenon — SG searches still strong on Netflix",
        "search_term": "Squid Game Season 2",
        "tmdb_id":     None,
    },
    # Timeless classics still being searched in SG
    {
        "name":        "Queen of Tears",
        "genre":       "kdrama",
        "context":     "2024 classic — still heavily rewatched by SG fans on Netflix",
        "search_term": "Queen of Tears",
        "tmdb_id":     202431,
    },
    {
        "name":        "Lovely Runner",
        "genre":       "kdrama",
        "context":     "Byeon Woo-seok time-travel romance — rewatched in SG",
        "search_term": "Lovely Runner",
        "tmdb_id":     None,
    },
    {
        "name":        "Crash Landing on You",
        "genre":       "kdrama",
        "context":     "Timeless classic — consistent SG rewatch community on Netflix",
        "search_term": "Crash Landing on You",
        "tmdb_id":     130392,
    },
    {
        "name":        "Vincenzo",
        "genre":       "kdrama",
        "context":     "Song Joong-ki crime thriller — still popular in SG",
        "search_term": "Vincenzo",
        "tmdb_id":     None,
    },

    # ── C-DRAMA 2026 ──────────────────────────────────────────────────────
    # Sources: Her World SG, Singapore Women's Weekly, Tonboriday

    {
        "name":        "How Dare You",
        "genre":       "cdrama",
        "context":     "Transmigration comedy — dominating SG C-Drama discussions on iQIYI",
        "search_term": "How Dare You",
        "tmdb_id":     None,
    },
    {
        "name":        "My Page in the 90s",
        "genre":       "cdrama",
        "context":     "Chen Xingxu time-travel romance — SG fans loving 90s nostalgia",
        "search_term": "My Page in the 90s",
        "tmdb_id":     None,
    },
    {
        "name":        "Shine on Me",
        "genre":       "cdrama",
        "context":     "Song Weilong and Zhao Jinmai youth romance on Netflix SG",
        "search_term": "Shine on Me",
        "tmdb_id":     None,
    },
    {
        "name":        "Pursuit of Jade",
        "genre":       "cdrama",
        "context":     "Zhang Linghe historical romance — trending on Netflix and iQIYI SG",
        "search_term": "Pursuit of Jade",
        "tmdb_id":     None,
    },
    {
        "name":        "The Story of Rose",
        "genre":       "cdrama",
        "context":     "Liu Yifei modern drama — still actively discussed in SG on WeTV",
        "search_term": "The Story of Rose",
        "tmdb_id":     None,
    },
    {
        "name":        "Blossoms Shanghai",
        "genre":       "cdrama",
        "context":     "Wong Kar-wai masterpiece — SG fans still recommending on iQIYI",
        "search_term": "Blossoms Shanghai",
        "tmdb_id":     None,
    },
    {
        "name":        "The Double",
        "genre":       "cdrama",
        "context":     "Revenge period drama — strong SG word-of-mouth",
        "search_term": "The Double",
        "tmdb_id":     None,
    },
    {
        "name":        "The Untamed",
        "genre":       "cdrama",
        "context":     "Xiao Zhan and Wang Yibo — timeless BL classic rewatched in SG",
        "search_term": "The Untamed",
        "tmdb_id":     None,
    },
    {
        "name":        "Nirvana in Fire",
        "genre":       "cdrama",
        "context":     "Timeless C-Drama classic — steady SG rewatch community on iQIYI",
        "search_term": "Nirvana in Fire",
        "tmdb_id":     None,
    },
    {
        "name":        "Love Like the Galaxy",
        "genre":       "cdrama",
        "context":     "Historical romance — loyal SG xianxia fan community on WeTV",
        "search_term": "Love Like the Galaxy",
        "tmdb_id":     None,
    },
    {
        "name":        "Go Ahead",
        "genre":       "cdrama",
        "context":     "Family drama — still recommended in SG groups on iQIYI",
        "search_term": "Go Ahead",
        "tmdb_id":     None,
    },

    # ── LOCAL SG 2026 ─────────────────────────────────────────────────────
    # Sources: Star Awards 2026, Mediacorp, meWatch

    {
        "name":        "Emerald Hill Little Nyonya Story",
        "genre":       "local",
        "context":     "Star Awards 2026 biggest winner — 6 awards including Best Drama",
        "search_term": "Emerald Hill Little Nyonya Story",
        "tmdb_id":     None,
    },
    {
        "name":        "Pure Vanilla",
        "genre":       "local",
        "context":     "New 2026 SG drama — gaining local viewership on meWatch",
        "search_term": "Pure Vanilla",
        "tmdb_id":     None,
    },
    {
        "name":        "People Like Us",
        "genre":       "local",
        "context":     "SG community drama — relatable heartland stories on Channel 8",
        "search_term": "People Like Us",
        "tmdb_id":     None,
    },
    {
        "name":        "128 Circle",
        "genre":       "local",
        "context":     "Singapore first multilingual drama — hawker centre stories",
        "search_term": "128 Circle",
        "tmdb_id":     None,
    },
    {
        "name":        "The Little Nyonya",
        "genre":       "local",
        "context":     "Iconic 2008 Peranakan drama — rewatched ahead of sequel",
        "search_term": "The Little Nyonya",
        "tmdb_id":     None,
    },
    {
        "name":        "Unforgivable",
        "genre":       "local",
        "context":     "Star Awards 2025 Best Drama — still being discussed in SG",
        "search_term": "Unforgivable",
        "tmdb_id":     None,
    },
    {
        "name":        "Code of Law",
        "genre":       "local",
        "context":     "Long-running SG legal drama — loyal local following on meWatch",
        "search_term": "Code of Law",
        "tmdb_id":     None,
    },

    # ── THAI 2026 ─────────────────────────────────────────────────────────
    # Sources: Harper's Bazaar SG, GMMTV 2026 lineup, Channel 3

    {
        "name":        "Beneath The Lies",
        "genre":       "thai",
        "context":     "Gulf Kanawut and Yaya Urassaya — massive Channel 3 production 2026",
        "search_term": "Beneath The Lies",
        "tmdb_id":     None,
    },
    {
        "name":        "Girl From Nowhere The Reset",
        "genre":       "thai",
        "context":     "Cult Thai thriller reboot — Becky Armstrong on Netflix",
        "search_term": "Girl From Nowhere The Reset",
        "tmdb_id":     None,
    },
    {
        "name":        "Moonshadow",
        "genre":       "thai",
        "context":     "Emi and Bonnie GL sequel — highly anticipated in SG",
        "search_term": "Moonshadow",
        "tmdb_id":     None,
    },
    {
        "name":        "Only Friends",
        "genre":       "thai",
        "context":     "GMMTV BL — still active SG fan community on WeTV",
        "search_term": "Only Friends",
        "tmdb_id":     None,
    },
    {
        "name":        "Hidden Agenda",
        "genre":       "thai",
        "context":     "Thai BL — word-of-mouth growing in SG on WeTV",
        "search_term": "Hidden Agenda",
        "tmdb_id":     None,
    },
    {
        "name":        "Bad Buddy",
        "genre":       "thai",
        "context":     "GMMTV BL classic — timeless SG rewatch favourite",
        "search_term": "Bad Buddy",
        "tmdb_id":     None,
    },
    {
        "name":        "2gether The Series",
        "genre":       "thai",
        "context":     "Thai BL pioneer — long-tail rewatches in SG",
        "search_term": "2gether The Series",
        "tmdb_id":     None,
    },
    {
        "name":        "A Tale of Thousand Stars",
        "genre":       "thai",
        "context":     "Scenic Thai countryside — travel curiosity among SG viewers",
        "search_term": "A Tale of Thousand Stars",
        "tmdb_id":     None,
    },
    {
        "name":        "Hometown Romance",
        "genre":       "thai",
        "context":     "Thai countryside rom-com — SG fans following on WeTV",
        "search_term": "Hometown Romance",
        "tmdb_id":     None,
    },

    # ── WESTERN 2026 ──────────────────────────────────────────────────────
    # Sources: Netflix SG, Disney+ SG, Harper's Bazaar SG

    {
        "name":        "Bridgerton Season 4",
        "genre":       "western",
        "context":     "Benedict Bridgerton arc — SG fans eagerly waiting on Netflix",
        "search_term": "Bridgerton Season 4",
        "tmdb_id":     92783,
    },
    {
        "name":        "Suits LA",
        "genre":       "western",
        "context":     "Suits spinoff — SG legal drama fans on Netflix",
        "search_term": "Suits LA",
        "tmdb_id":     None,
    },
    {
        "name":        "The White Lotus Season 3",
        "genre":       "western",
        "context":     "Thailand setting — SG viewers very engaged on HBO/Max",
        "search_term": "The White Lotus Season 3",
        "tmdb_id":     None,
    },
    {
        "name":        "Emily in Paris",
        "genre":       "western",
        "context":     "Guilty pleasure — love-hate relationship with SG viewers on Netflix",
        "search_term": "Emily in Paris",
        "tmdb_id":     None,
    },
    {
        "name":        "Virgin River",
        "genre":       "western",
        "context":     "Cosy small-town romance — loyal SG binge-watching audience on Netflix",
        "search_term": "Virgin River",
        "tmdb_id":     None,
    },
    {
        "name":        "Outlander",
        "genre":       "western",
        "context":     "Epic time-travel romance — loyal SG fandom on Netflix",
        "search_term": "Outlander",
        "tmdb_id":     None,
    },

    # ── OTHERS 2026 ───────────────────────────────────────────────────────
    # Turkish, Japanese, Indian — active SG community searches

    {
        "name":        "Dirilis Ertugrul",
        "genre":       "others",
        "context":     "Turkish epic — loyal SG Malay community still active on Netflix",
        "search_term": "Dirilis Ertugrul",
        "tmdb_id":     None,
    },
    {
        "name":        "Kurulus Osman",
        "genre":       "others",
        "context":     "Ertugrul sequel — SG Malay fans following on Netflix",
        "search_term": "Kurulus Osman",
        "tmdb_id":     None,
    },
    {
        "name":        "Scam 1992",
        "genre":       "others",
        "context":     "Indian drama — SG viewers calling it essential viewing on ZEE5",
        "search_term": "Scam 1992",
        "tmdb_id":     None,
    },
    {
        "name":        "Panchayat",
        "genre":       "others",
        "context":     "Indian drama on Amazon Prime — SG Indian community recommending",
        "search_term": "Panchayat",
        "tmdb_id":     None,
    },
    {
        "name":        "Mirzapur",
        "genre":       "others",
        "context":     "Indian crime drama on Amazon Prime — SG Indian community watching",
        "search_term": "Mirzapur",
        "tmdb_id":     None,
    },
    {
        "name":        "Oshin",
        "genre":       "others",
        "context":     "J-Drama classic — nostalgia wave among older SG viewers on YouTube",
        "search_term": "Oshin",
        "tmdb_id":     None,
    },
    {
        "name":        "Hana Yori Dango",
        "genre":       "others",
        "context":     "J-Drama classic — younger SG viewers discovering it on Netflix",
        "search_term": "Hana Yori Dango",
        "tmdb_id":     None,
    },
]

# ── 2026 SEED ARTISTS ─────────────────────────────────────────────────────
# Top searched drama artists in SG 2026
# search_term = what SG people actually search
SEED_ARTISTS = [

    # ── K-DRAMA — Perfect Crown (Disney+, Apr 2026) ───────────────────────
    # Cast: IU, Byeon Woo-seok, Gong Seung-yeon, Yu Su-bin, Lee Yeon
    {"name":"Byeon Woo-seok",    "show":"Perfect Crown",              "genre":"kdrama",  "tmdb_id":1067226},
    {"name":"IU",                "show":"Perfect Crown",              "genre":"kdrama",  "tmdb_id":976264},
    {"name":"Gong Seung-yeon",   "show":"Perfect Crown",              "genre":"kdrama",  "tmdb_id":None},
    {"name":"Lee Yeon",          "show":"Perfect Crown",              "genre":"kdrama",  "tmdb_id":None},

    # ── K-DRAMA — Tempest (Disney+, Sep 2025) ────────────────────────────
    # Cast: Gianna Jun, Gang Dong-won, Park Hae-joon, Lee Mi-sook
    {"name":"Gianna Jun",        "show":"Tempest",                    "genre":"kdrama",  "tmdb_id":19543},
    {"name":"Gang Dong-won",     "show":"Tempest",                    "genre":"kdrama",  "tmdb_id":None},
    {"name":"Park Hae-joon",     "show":"Tempest",                    "genre":"kdrama",  "tmdb_id":None},

    # ── K-DRAMA — When Life Gives You Tangerines (Netflix, 2025) ─────────
    # Cast: IU, Park Bo-gum
    {"name":"Park Bo-gum",       "show":"When Life Gives You Tangerines","genre":"kdrama","tmdb_id":None},

    # ── K-DRAMA — Other top K-Drama actors searched in SG ────────────────
    {"name":"Shin Min-A",        "show":"The Remarried Empress",      "genre":"kdrama",  "tmdb_id":19429},
    {"name":"Song Hye-kyo",      "show":"The Glory",                  "genre":"kdrama",  "tmdb_id":19492},
    {"name":"Gong Yoo",          "show":"Various",                    "genre":"kdrama",  "tmdb_id":19217},
    {"name":"Kim Soo-hyun",      "show":"Queen of Tears",             "genre":"kdrama",  "tmdb_id":55168},
    {"name":"Kim Ji-won",        "show":"Queen of Tears",             "genre":"kdrama",  "tmdb_id":1726172},
    {"name":"Park Hae-soo",      "show":"Karma",                      "genre":"kdrama",  "tmdb_id":2974862},
    {"name":"Lee Min-ho",        "show":"Various",                    "genre":"kdrama",  "tmdb_id":1271717},
    {"name":"Park Seo-joon",     "show":"Various",                    "genre":"kdrama",  "tmdb_id":1271757},
    {"name":"Son Ye-jin",        "show":"Crash Landing on You",       "genre":"kdrama",  "tmdb_id":19430},
    {"name":"Han So-hee",        "show":"Various",                    "genre":"kdrama",  "tmdb_id":2613816},
    {"name":"Go Youn-jung",      "show":"Various",                    "genre":"kdrama",  "tmdb_id":3490526},
    {"name":"Cha Eun-woo",       "show":"Various",                    "genre":"kdrama",  "tmdb_id":2254676},
    {"name":"Park Bo-young",     "show":"Various",                    "genre":"kdrama",  "tmdb_id":975376},
    {"name":"Lim Yoona",         "show":"Various",                    "genre":"kdrama",  "tmdb_id":19458},
    {"name":"Song Joong-ki",     "show":"Vincenzo",                   "genre":"kdrama",  "tmdb_id":None},
    {"name":"Lee Jong-suk",      "show":"Various",                    "genre":"kdrama",  "tmdb_id":None},
    {"name":"Hyun Bin",          "show":"Crash Landing on You",       "genre":"kdrama",  "tmdb_id":None},

    # ── C-DRAMA — How Dare You (iQIYI/Netflix, 2026) ─────────────────────
    # Cast: Cheng Lei, Wang Churan, Tian Xiwei, Zhang Linghe
    {"name":"Cheng Lei",         "show":"How Dare You",               "genre":"cdrama",  "tmdb_id":None},
    {"name":"Wang Churan",       "show":"How Dare You",               "genre":"cdrama",  "tmdb_id":None},

    # ── C-DRAMA — Pursuit of Jade (Netflix/iQIYI, 2026) ──────────────────
    # Cast: Tian Xiwei, Zhang Linghe
    {"name":"Zhang Linghe",      "show":"Pursuit of Jade",            "genre":"cdrama",  "tmdb_id":None},
    {"name":"Tian Xiwei",        "show":"Pursuit of Jade",            "genre":"cdrama",  "tmdb_id":None},

    # ── C-DRAMA — Shine on Me (Netflix, 2026) ────────────────────────────
    # Cast: Song Weilong, Zhao Jinmai
    {"name":"Song Weilong",      "show":"Shine on Me",                "genre":"cdrama",  "tmdb_id":None},
    {"name":"Zhao Jinmai",       "show":"Shine on Me",                "genre":"cdrama",  "tmdb_id":None},

    # ── C-DRAMA — Other top C-Drama actors searched in SG ────────────────
    {"name":"Chen Xingxu",       "show":"My Page in the 90s",         "genre":"cdrama",  "tmdb_id":None},
    {"name":"Liu Yifei",         "show":"The Story of Rose",          "genre":"cdrama",  "tmdb_id":16000},
    {"name":"Hu Ge",             "show":"Nirvana in Fire",            "genre":"cdrama",  "tmdb_id":None},
    {"name":"Xiao Zhan",         "show":"The Untamed",                "genre":"cdrama",  "tmdb_id":None},
    {"name":"Wang Yibo",         "show":"The Untamed",                "genre":"cdrama",  "tmdb_id":None},
    {"name":"Yang Mi",           "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},
    {"name":"Zhao Liying",       "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},
    {"name":"Dilireba",          "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},
    {"name":"Yang Zi",           "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},
    {"name":"Zhao Lusi",         "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},
    {"name":"Bai Lu",            "show":"Various",                    "genre":"cdrama",  "tmdb_id":None},

    # ── LOCAL SG — Emerald Hill Little Nyonya Story (Channel 8, 2026) ────
    # Star Awards 2026 — 6 awards including Best Drama, Best Actress (Jesseca Liu)
    {"name":"Carrie Wong",       "show":"Emerald Hill Little Nyonya Story","genre":"local","tmdb_id":None},
    {"name":"Jesseca Liu",       "show":"Emerald Hill Little Nyonya Story","genre":"local","tmdb_id":None},
    {"name":"Zoe Tay",           "show":"Emerald Hill Little Nyonya Story","genre":"local","tmdb_id":None},

    # ── LOCAL SG — Other top local SG actors searched ────────────────────
    {"name":"Desmond Tan",       "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Tay Ping Hui",      "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Rebecca Lim",       "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Joanne Peh",        "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Pierre Png",        "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Fann Wong",         "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Chen Hanwei",       "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Romeo Tan",         "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Felicia Chin",      "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Rui En",            "show":"Various",                    "genre":"local",   "tmdb_id":None},
    {"name":"Thomas Ong",        "show":"Various",                    "genre":"local",   "tmdb_id":None},

    # ── THAI — Beneath The Lies (Channel 3, 2026) ────────────────────────
    # Cast: Gulf Kanawut, Yaya Urassaya, Kao Noppakao
    {"name":"Gulf Kanawut",      "show":"Beneath The Lies",           "genre":"thai",    "tmdb_id":None},
    {"name":"Yaya Urassaya",     "show":"Beneath The Lies",           "genre":"thai",    "tmdb_id":None},

    # ── THAI — GMMTV 2026 top actors ─────────────────────────────────────
    {"name":"Joss Wachirawit",   "show":"Only Friends",               "genre":"thai",    "tmdb_id":None},
    {"name":"Bright Vachirawit", "show":"2gether The Series",         "genre":"thai",    "tmdb_id":None},
    {"name":"Mew Suppasit",      "show":"Various",                    "genre":"thai",    "tmdb_id":None},
    {"name":"Win Metawin",       "show":"2gether The Series",         "genre":"thai",    "tmdb_id":None},
    {"name":"Apo Nattawin",      "show":"Various",                    "genre":"thai",    "tmdb_id":None},
    {"name":"Mile Phakphum",     "show":"Various",                    "genre":"thai",    "tmdb_id":None},
    {"name":"New Thitipoom",     "show":"Hidden Agenda",              "genre":"thai",    "tmdb_id":None},
    {"name":"Baifern Pimchanok", "show":"Various",                    "genre":"thai",    "tmdb_id":None},
    {"name":"Nanon Korapat",     "show":"Various",                    "genre":"thai",    "tmdb_id":None},
    {"name":"Ohm Thitiwat",      "show":"Various",                    "genre":"thai",    "tmdb_id":None},

    # ── WESTERN — Bridgerton Season 4 (Netflix, 2026) ────────────────────
    {"name":"Nicola Coughlan",   "show":"Bridgerton Season 4",        "genre":"western", "tmdb_id":1892381},
    {"name":"Jonathan Bailey",   "show":"Bridgerton Season 4",        "genre":"western", "tmdb_id":1185649},
    {"name":"Luke Thompson",     "show":"Bridgerton Season 4",        "genre":"western", "tmdb_id":None},

    # ── WESTERN — Other top Western actors searched in SG ─────────────────
    {"name":"Pedro Pascal",      "show":"Various",                    "genre":"western", "tmdb_id":1253360},
    {"name":"Sydney Sweeney",    "show":"Various",                    "genre":"western", "tmdb_id":2195545},
    {"name":"Millie Bobby Brown","show":"Various",                    "genre":"western", "tmdb_id":None},

    # ── OTHERS — Turkish, Japanese, Indian ───────────────────────────────
    {"name":"Engin Altan",       "show":"Dirilis Ertugrul",           "genre":"others",  "tmdb_id":None},
    {"name":"Burak Ozcivit",     "show":"Kurulus Osman",              "genre":"others",  "tmdb_id":None},
    {"name":"Can Yaman",         "show":"Various Turkish",            "genre":"others",  "tmdb_id":None},
    {"name":"Pratik Gandhi",     "show":"Scam 1992",                  "genre":"others",  "tmdb_id":None},
    {"name":"Takuya Kimura",     "show":"Various J-Drama",            "genre":"others",  "tmdb_id":None},
    {"name":"Pankaj Tripathi",   "show":"Mirzapur",                   "genre":"others",  "tmdb_id":None},
]

# ── 2026 SEED EVENTS ──────────────────────────────────────────────────────
# Real confirmed SG drama events in 2026
# Sources: Wikipedia, Soompi, KBeats SG, Singapore Expo, Ticketmaster SG
SEED_EVENTS = [
    {
        "title":       "Star Awards 2026",
        "genre":       "local",
        "type":        "Awards",
        "venue":       "MES Theatre @ Mediacorp",
        "event_date":  "Apr 19 2026",
        "description": "The 31st Star Awards ceremony celebrating excellence in Singapore TV drama. Themed 'Born to Glow'. Emerald Hill - The Little Nyonya Story won 6 awards including Best Drama. Jesseca Liu won Best Actress.",
        "search_term": "Star Awards 2026",
        "links":       [{"l":"Mediacorp","u":"https://www.mediacorp.sg"},{"l":"meWatch","u":"https://www.mewatch.sg"}],
    },
    {
        "title":       "LingOrm Only Love Fan Meeting Singapore",
        "genre":       "thai",
        "type":        "Fan Meet",
        "venue":       "ARENA@EXPO, Singapore Expo",
        "event_date":  "Feb 21 2026",
        "description": "Thai actress LingOrm's first solo fan meeting in Singapore. Tickets from $208–$388. Organised by SimpleLightCulture.",
        "search_term": "LingOrm fan meeting Singapore",
        "links":       [{"l":"Singapore Expo","u":"https://www.singaporeexpo.com.sg"}],
    },
    {
        "title":       "SEVENTEEN World Tour Singapore",
        "genre":       "kdrama",
        "type":        "Concert",
        "venue":       "National Stadium",
        "event_date":  "Mar 7 2026",
        "description": "SEVENTEEN NEW_ World Tour concert at the National Stadium Singapore. One of the biggest K-pop events of 2026 in SG.",
        "search_term": "SEVENTEEN Singapore 2026",
        "links":       [{"l":"SISTIC","u":"https://www.sistic.com.sg"}],
    },
    {
        "title":       "ATEEZ World Tour Singapore",
        "genre":       "kdrama",
        "type":        "Concert",
        "venue":       "Singapore Indoor Stadium",
        "event_date":  "Feb 22 2026",
        "description": "ATEEZ 2026 World Tour 'IN YOUR FANTASY' at Singapore Indoor Stadium. High-energy performance from the globally acclaimed K-pop group.",
        "search_term": "ATEEZ Singapore 2026",
        "links":       [{"l":"SISTIC","u":"https://www.sistic.com.sg"}],
    },
    {
        "title":       "NMIXX World Tour Singapore",
        "genre":       "kdrama",
        "type":        "Concert",
        "venue":       "Singapore Indoor Stadium",
        "event_date":  "Jun 20 2026",
        "description": "NMIXX 1st World Tour EPISODE 1: ZERO FRONTIER in Singapore. JYP Entertainment's rising girl group performing live in SG.",
        "search_term": "NMIXX Singapore 2026",
        "links":       [{"l":"Ticketmaster","u":"https://ticketmaster.sg"}],
    },
    {
        "title":       "i-dle World Tour Singapore",
        "genre":       "kdrama",
        "type":        "Concert",
        "venue":       "Singapore Indoor Stadium",
        "event_date":  "Jun 13 2026",
        "description": "(G)I-DLE WORLD TOUR SYNCOPATION in Singapore. The acclaimed K-pop group known for self-produced music performs at the Singapore Indoor Stadium.",
        "search_term": "G-IDLE Singapore 2026",
        "links":       [{"l":"Ticketmaster","u":"https://ticketmaster.sg"}],
    },
    {
        "title":       "TXT World Tour Singapore",
        "genre":       "kdrama",
        "type":        "Concert",
        "venue":       "Singapore Indoor Stadium",
        "event_date":  "Jan 17–18 2026",
        "description": "TOMORROW X TOGETHER World Tour ACT: TOMORROW at Singapore Indoor Stadium. Two-night concert by BTS labelmates TXT.",
        "search_term": "TXT Tomorrow X Together Singapore 2026",
        "links":       [{"l":"SISTIC","u":"https://www.sistic.com.sg"}],
    },
    {
        "title":       "Emerald Hill Little Nyonya Story",
        "genre":       "local",
        "type":        "Drama Series",
        "venue":       "Channel 8 / meWatch",
        "event_date":  "2026",
        "description": "The most awarded drama at Star Awards 2026 — 6 awards including Best Drama. A sequel to the iconic Little Nyonya, exploring Peranakan heritage in modern Singapore.",
        "search_term": "Emerald Hill Little Nyonya Story",
        "links":       [{"l":"meWatch","u":"https://www.mewatch.sg"}],
    },
]

# ── TMDB HELPERS ──────────────────────────────────────────────────────────

def tmdb_search_show(name: str, tmdb_id: int = None) -> dict:
    """Fetch show details from TMDB. Use tmdb_id if provided, else search by name."""
    try:
        if tmdb_id:
            url = f"{TMDB_BASE}/tv/{tmdb_id}"
            params = {
                "api_key": TMDB_API_KEY,
                "language": "en-SG",
                "append_to_response": "watch/providers,alternative_titles",
            }
            res = requests.get(url, params=params, timeout=10)
            if not res.ok:
                return {}
            d = res.json()
        else:
            # Search by name
            url = f"{TMDB_BASE}/search/tv"
            params = {"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"}
            res = requests.get(url, params=params, timeout=10)
            if not res.ok:
                return {}
            results = res.json().get("results", [])
            if not results:
                return {}
            found_id = results[0]["id"]
            # Get full details
            detail_url = f"{TMDB_BASE}/tv/{found_id}"
            detail_params = {
                "api_key": TMDB_API_KEY,
                "language": "en-SG",
                "append_to_response": "watch/providers,alternative_titles",
            }
            detail_res = requests.get(detail_url, params=detail_params, timeout=10)
            if not detail_res.ok:
                return {"tmdb_id": found_id, "description": results[0].get("overview", "")}
            d = detail_res.json()

        # SG streaming platforms
        sg = d.get("watch/providers", {}).get("results", {}).get("SG", {})
        platforms = []
        for p in sg.get("flatrate", []):
            code = TMDB_PROVIDER_MAP.get(p.get("provider_id"))
            if code and code not in platforms:
                platforms.append(code)

        # Chinese/Korean original title
        alt_titles = d.get("alternative_titles", {}).get("results", [])
        chinese_title = None
        for t in alt_titles:
            if t.get("iso_3166_1") in ["CN", "TW", "HK", "KR"]:
                chinese_title = t.get("title")
                break

        # is_new from last air date
        last_air = d.get("last_air_date", "")
        status   = d.get("status", "")
        is_new   = status in ["Returning Series", "In Production"]
        if last_air:
            try:
                days_ago = (datetime.now() - datetime.strptime(last_air, "%Y-%m-%d")).days
                is_new = days_ago <= 14
            except Exception:
                pass

        return {
            "tmdb_id":       d.get("id"),
            "description":   d.get("overview", ""),
            "chinese_title": chinese_title,
            "platforms":     platforms,
            "is_new":        is_new,
        }
    except Exception as e:
        log.warning(f"TMDB show error for '{name}': {e}")
        return {}


def tmdb_search_person(name: str, tmdb_id: int = None) -> dict:
    """Search TMDB for an artist. Use tmdb_id for direct lookup if available."""
    try:
        if tmdb_id:
            # Direct lookup by TMDB person ID — more accurate
            url = f"{TMDB_BASE}/person/{tmdb_id}"
            params = {"api_key": TMDB_API_KEY, "language": "en-SG",
                      "append_to_response": "combined_credits"}
            res = requests.get(url, params=params, timeout=10)
            if res.ok:
                person = res.json()
                # Get most known drama role
                cast = person.get("combined_credits", {}).get("cast", [])
                cast_sorted = sorted(
                    [c for c in cast if c.get("media_type") in ["tv","movie"]],
                    key=lambda x: x.get("popularity", 0), reverse=True
                )
                show_name = cast_sorted[0].get("name") or cast_sorted[0].get("title","") if cast_sorted else ""
                role = "Actress" if person.get("gender") == 1 else "Actor"
                return {"tmdb_id": tmdb_id, "role": role, "show_name": show_name}

        # Search by name
        url = f"{TMDB_BASE}/search/person"
        params = {"api_key": TMDB_API_KEY, "query": name, "language": "en-SG"}
        res = requests.get(url, params=params, timeout=10)
        if not res.ok:
            return {}
        results = res.json().get("results", [])
        if not results:
            return {}
        person = results[0]
        known_for = person.get("known_for", [])
        show_name = ""
        if known_for:
            show_name = known_for[0].get("name") or known_for[0].get("title", "")
        role = "Actress" if person.get("gender") == 1 else "Actor"
        return {
            "tmdb_id":  person.get("id"),
            "role":     role,
            "show_name": show_name,
        }
    except Exception as e:
        log.warning(f"TMDB person error for '{name}': {e}")
        return {}


def wiki_lookup(name: str) -> dict:
    """Wikipedia fallback for shows not found on TMDB."""
    try:
        for suffix in ["", "_TV_series", "_drama"]:
            wiki_name = name.replace(" ", "_") + suffix
            url = f"{WIKI_API}/{requests.utils.quote(wiki_name)}"
            res = requests.get(url, timeout=10, headers=WIKI_HEADERS)
            if res.ok and res.json().get("type") != "disambiguation":
                desc = res.json().get("extract", "")
                sentences = desc.split(". ")
                return {
                    "description": ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
                }
        return {}
    except Exception as e:
        log.warning(f"Wikipedia error for '{name}': {e}")
        return {}

# ── RSS HELPERS ───────────────────────────────────────────────────────────

def fetch_rss_entries() -> list:
    """Fetch all RSS entries from SG news sources."""
    entries = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
            log.info(f"  RSS: {len(feed.entries)} entries from {url}")
        except Exception as e:
            log.warning(f"  RSS error [{url}]: {e}")
    return entries


def discover_shows_from_rss(entries: list, known_names: set, genre_map: dict) -> list:
    """
    Scan RSS articles for drama show mentions not yet in seed list.
    Returns list of new show dicts to insert.
    """
    drama_keywords = ["drama","series","kdrama","k-drama","c-drama","cdrama",
                      "netflix","disney+","viu","wetv","iqiyi","mediacorp",
                      "channel 8","channel 5","mewatch"]
    sg_keywords    = ["singapore","sg","singaporean","local"]
    new_shows = []

    for entry in entries:
        title   = entry.get("title", "").strip()
        summary = entry.get("summary", "").lower()
        text    = (title + " " + summary).lower()

        # Must mention drama AND Singapore
        if not any(kw in text for kw in drama_keywords): continue
        if not any(kw in text for kw in sg_keywords):    continue
        if title.lower() in known_names:                 continue
        if len(title) > 100:                             continue  # too long — generic

        # Determine genre from content
        if any(w in text for w in ["korean drama","k-drama","kdrama"]):
            genre = "kdrama"
        elif any(w in text for w in ["chinese drama","c-drama","cdrama"]):
            genre = "cdrama"
        elif any(w in text for w in ["thai drama","thailand drama"]):
            genre = "thai"
        elif any(w in text for w in ["singapore drama","local drama","channel 8","channel 5","mediacorp"]):
            genre = "local"
        else:
            genre = "others"

        genre_id = genre_map.get(genre) or genre_map.get("others")

        new_shows.append({
            "name":          title,
            "genre":         genre,
            "genre_id":      genre_id,
            "context":       "Discovered from SG news",
            "search_term":   title,
            "description":   "",
            "chinese_title": None,
            "platforms":     [],
            "is_new":        False,
            "has_description": False,
        })
        known_names.add(title.lower())
        log.info(f"  RSS discovery: '{title}' ({genre})")

    return new_shows

# ── GOOGLE TRENDS ─────────────────────────────────────────────────────────

def fetch_daily_scores(pytrends, term: str, name: str) -> list:
    """
    Fetch daily Google Trends scores from 1st Jan to today.
    Returns list of {date, score} dicts — raw scores, not normalised.
    Normalisation happens later across all shows per day.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        timeframe = f"2026-01-01 {today}"
        pytrends.build_payload([term], geo="SG", timeframe=timeframe)
        df = pytrends.interest_over_time()
        if df.empty or term not in df.columns:
            log.warning(f"  No Trends data for '{name}'")
            return []
        results = [
            {
                "date":  ts.to_pydatetime().replace(tzinfo=timezone.utc),
                "score": float(df.loc[ts, term]),
            }
            for ts in df.index
        ]
        log.info(f"  Trends [{name}]: {len(results)} days, max={max(r['score'] for r in results):.1f}")
        return results
    except Exception as e:
        log.warning(f"  Trends error for '{name}': {e}")
        return []

# ── SCORING HELPERS ───────────────────────────────────────────────────────

def normalise_daily(daily_scores: list) -> list:
    """Legacy — kept for compatibility. Per-day cross-show normalisation happens in Phase 5."""
    return daily_scores


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


def build_sparkline(scores: list) -> list:
    """Last 7 scores as sparkline array."""
    return [round(s, 1) for s in scores[-7:]]

# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Drama Watch SG — INIT scraper v2 ===")
    log.info(f"    {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M SGT')}")

    sb       = create_client(SUPABASE_URL, SUPABASE_KEY)
    pytrends = TrendReq(hl="en-SG", tz=480, timeout=(10, 25))

    # ── PHASE 1: Fetch Google Trends scores for seed shows ────────────────
    log.info("--- Phase 1: Fetching Google Trends scores for seed shows ---")
    show_scores    = {}  # name → [{date, score, normalised}]
    artist_scores  = {}
    event_scores   = {}
    genre_totals   = {}  # genre_code → total score

    for s in SEED_SHOWS:
        daily = fetch_daily_scores(pytrends, s["search_term"], s["name"])
        if daily:
            show_scores[s["name"]] = daily
            total = sum(d["score"] for d in daily)
            genre_totals[s["genre"]] = genre_totals.get(s["genre"], 0) + total
        time.sleep(TRENDS_DELAY)

    log.info(f"  Shows with Trends data: {len(show_scores)}/{len(SEED_SHOWS)}")

    # ── PHASE 2: Fetch scores for seed artists ────────────────────────────
    log.info("--- Phase 2: Fetching Google Trends scores for seed artists ---")
    for a in SEED_ARTISTS:
        daily = fetch_daily_scores(pytrends, a["search_term"], a["name"])
        if daily:
            artist_scores[a["name"]] = daily
        time.sleep(TRENDS_DELAY)

    log.info(f"  Artists with Trends data: {len(artist_scores)}/{len(SEED_ARTISTS)}")

    # ── PHASE 3: Fetch scores for seed events ────────────────────────────
    log.info("--- Phase 3: Fetching Google Trends scores for seed events ---")
    for e in SEED_EVENTS:
        daily = fetch_daily_scores(pytrends, e["search_term"], e["title"])
        if daily:
            event_scores[e["title"]] = daily
        time.sleep(TRENDS_DELAY)

    log.info(f"  Events with Trends data: {len(event_scores)}/{len(SEED_EVENTS)}")

    # ── PHASE 4: Genre ranking from real scores ───────────────────────────
    log.info("--- Phase 4: Ranking genres from real search scores ---")

    # Ensure all seed genres are represented even if no Trends data
    all_seed_genres = list(dict.fromkeys(s["genre"] for s in SEED_SHOWS))
    for g in all_seed_genres:
        if g not in genre_totals:
            genre_totals[g] = 0  # zero score — will build over time

    sorted_genres = sorted(genre_totals.items(), key=lambda x: x[1], reverse=True)
    top5 = [g[0] for g in sorted_genres[:5]]
    log.info(f"  Top 5 genres by SG search volume: {top5}")

    # Insert genres — always ensure "others" exists
    for i, (code, total) in enumerate(sorted_genres):
        is_top5 = i < 5
        rank    = i + 1 if is_top5 else 99
        colors  = GENRE_COLORS[i] if i < 5 else GENRE_COLORS[5]
        sb.table("genres").upsert({
            "code":       code,
            "label":      GENRE_LABELS.get(code, code.title()),
            "dot_color":  colors["dot_color"],
            "bg_color":   colors["bg_color"],
            "text_color": colors["text_color"],
            "is_top5":    is_top5,
            "rank":       rank,
            "is_active":  True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="code").execute()
        log.info(f"  Genre {rank}: {code} (total score {total:.0f}) is_top5={is_top5}")

    # Always ensure Others exists
    others_colors = GENRE_COLORS[5]
    sb.table("genres").upsert({
        "code": "others", "label": "Others",
        "dot_color": others_colors["dot_color"],
        "bg_color":  others_colors["bg_color"],
        "text_color":others_colors["text_color"],
        "is_top5": False, "rank": 99,
        "is_active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="code").execute()

    # Build genre_id lookup
    genre_rows = sb.table("genres").select("id,code").execute().data
    genre_map  = {r["code"]: r["id"] for r in genre_rows}
    log.info(f"  Genre map: {genre_map}")

    # ── PHASE 5: TMDB lookup + insert shows_master ────────────────────────
    log.info("--- Phase 5: TMDB lookup and inserting shows_master ---")
    show_id_map = {}  # name → id

    for s in SEED_SHOWS:
        # Insert all seed shows regardless of Trends data
        # Shows with no Trends data get score=0 until daily update picks them up
        has_trends = s["name"] in show_scores
        if not has_trends:
            log.warning(f"  No Trends data for {s['name']} — inserting with score=0")

        # TMDB lookup
        tmdb = tmdb_search_show(s["name"], s.get("tmdb_id"))

        # Wikipedia fallback if no description
        if not tmdb.get("description"):
            log.info(f"  No TMDB description for {s['name']} — trying Wikipedia")
            wiki = wiki_lookup(s["name"])
            if wiki.get("description"):
                tmdb["description"] = wiki["description"]

        genre_id = genre_map.get(s["genre"]) or genre_map.get("others")
        has_desc = bool(tmdb.get("description", "").strip())

        row = {
            "name":          s["name"],
            "chinese_title": tmdb.get("chinese_title"),
            "genre_id":      genre_id,
            "platforms":     tmdb.get("platforms", []),
            "description":   tmdb.get("description", ""),
            "search_term":   s["search_term"],
            "tmdb_id":       tmdb.get("tmdb_id") or s.get("tmdb_id"),
            "has_description": has_desc,
            "is_active":     True,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("shows_master").upsert(row, on_conflict="name").execute()
        if res.data:
            show_id_map[s["name"]] = res.data[0]["id"]
            log.info(f"  ✅ Show: {s['name']} (genre={s['genre']}, has_desc={has_desc}, platforms={tmdb.get('platforms',[])})")
        time.sleep(0.3)

    # ── PHASE 6: RSS discovery — additional shows ─────────────────────────
    log.info("--- Phase 6: RSS discovery for additional shows ---")
    rss_entries = fetch_rss_entries()
    known_names = {s["name"].lower() for s in SEED_SHOWS}
    rss_shows   = discover_shows_from_rss(rss_entries, known_names, genre_map)

    for s in rss_shows:
        # TMDB lookup for RSS-discovered shows
        tmdb = tmdb_search_show(s["name"])
        if tmdb:
            s["description"]   = tmdb.get("description", "")
            s["chinese_title"] = tmdb.get("chinese_title")
            s["platforms"]     = tmdb.get("platforms", [])
            s["has_description"] = bool(s["description"].strip())

        row = {
            "name":          s["name"],
            "chinese_title": s.get("chinese_title"),
            "genre_id":      s["genre_id"],
            "platforms":     s.get("platforms", []),
            "description":   s.get("description", ""),
            "search_term":   s["search_term"],
            "has_description": s.get("has_description", False),
            "is_active":     True,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        try:
            res = sb.table("shows_master").upsert(row, on_conflict="name").execute()
            if res.data:
                # Also fetch 30-day Trends for RSS-discovered shows
                daily = fetch_daily_scores(pytrends, s["search_term"], s["name"])
                if daily:
                    daily = normalise_daily(daily)
                    show_scores[s["name"]] = daily
                    show_id_map[s["name"]] = res.data[0]["id"]
                time.sleep(TRENDS_DELAY)
        except Exception as e:
            log.warning(f"  RSS show insert error: {e}")

    # ── PHASE 7: TMDB lookup + insert artists_master ──────────────────────
    log.info("--- Phase 7: Inserting artists_master ---")
    artist_id_map = {}

    for a in SEED_ARTISTS:
        # Insert all seed artists regardless of Trends data
        has_trends = a["name"] in artist_scores
        if not has_trends:
            log.warning(f"  No Trends data for {a['name']} — inserting with score=0")

        tmdb = tmdb_search_person(a["name"], a.get("tmdb_id"))
        genre_id = genre_map.get(a["genre"]) or genre_map.get("others")

        # Find linked show_id
        show_id = None
        for sname, sid in show_id_map.items():
            if a["show"].lower() in sname.lower():
                show_id = sid
                break

        row = {
            "name":           a["name"],
            "role":           tmdb.get("role", "Actor"),
            "show_name":      a["show"],
            "genre_id":       genre_id,
            "search_term":    a["search_term"],
            "tmdb_id":        tmdb.get("tmdb_id"),
            "has_description": bool(tmdb.get("role") and a["show"]),
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("artists_master").upsert(row, on_conflict="name").execute()
        if res.data:
            artist_id_map[a["name"]] = res.data[0]["id"]
            log.info(f"  ✅ Artist: {a['name']} (role={tmdb.get('role','Actor')}, show={a['show']})")
        time.sleep(0.3)

    # ── PHASE 8: Insert events_master ─────────────────────────────────────
    log.info("--- Phase 8: Inserting events_master ---")
    event_id_map = {}

    for e in SEED_EVENTS:
        genre_id = genre_map.get(e["genre"]) or genre_map.get("others")
        has_desc = bool(e.get("description", "").strip())

        row = {
            "title":          e["title"],
            "genre_id":       genre_id,
            "type":           e["type"],
            "venue":          e.get("venue", ""),
            "event_date":     e.get("event_date", ""),
            "description":    e.get("description", ""),
            "links":          json.dumps(e.get("links", [])),
            "search_term":    e["search_term"],
            "has_description": has_desc,
            "is_active":      True,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        res = sb.table("events_master").upsert(row, on_conflict="title").execute()
        if res.data:
            event_id_map[e["title"]] = res.data[0]["id"]
            log.info(f"  ✅ Event: {e['title']}")
        time.sleep(0.2)

    # ── PHASE 9: Per-day cross-show normalisation + insert history rows ──────
    log.info("--- Phase 9: Per-day cross-show normalisation + inserting history rows ---")

    # Collect all dates across all items
    all_dates = set()
    for scores in list(show_scores.values()) + list(artist_scores.values()) + list(event_scores.values()):
        for d in scores:
            all_dates.add(d["date"].date())
    all_dates = sorted(all_dates)
    log.info(f"  Total days to process: {len(all_dates)}")

    # Build per-date score lookup — {date: {name: raw_score}}
    show_date_scores   = {}  # {name: {date: raw_score}}
    artist_date_scores = {}
    event_date_scores  = {}

    for name, daily in show_scores.items():
        show_date_scores[name] = {d["date"].date(): d["score"] for d in daily}

    for name, daily in artist_scores.items():
        artist_date_scores[name] = {d["date"].date(): d["score"] for d in daily}

    for title, daily in event_scores.items():
        event_date_scores[title] = {d["date"].date(): d["score"] for d in daily}

    # Per-day normalisation across all items combined
    # Find max score across ALL shows + artists + events for each day
    # Then normalise everything relative to that day's maximum
    show_norm_scores   = {name: {} for name in show_date_scores}
    artist_norm_scores = {name: {} for name in artist_date_scores}
    event_norm_scores  = {title: {} for title in event_date_scores}

    for date in all_dates:
        # Collect all raw scores for this day
        day_scores = {}
        for name in show_date_scores:
            day_scores[("show", name)] = show_date_scores[name].get(date, 0)
        for name in artist_date_scores:
            day_scores[("artist", name)] = artist_date_scores[name].get(date, 0)
        for title in event_date_scores:
            day_scores[("event", title)] = event_date_scores[title].get(date, 0)

        # Find max across all items this day
        max_score = max(day_scores.values()) or 1

        # Normalise each item for this day
        for (kind, name), raw in day_scores.items():
            norm = round((raw / max_score) * 100, 1)
            if kind == "show":
                show_norm_scores[name][date] = (norm, raw)
            elif kind == "artist":
                artist_norm_scores[name][date] = (norm, raw)
            else:
                event_norm_scores[name][date] = (norm, raw)

    # Insert shows_history
    log.info("  Inserting shows_history...")
    for name, show_id in show_id_map.items():
        if name not in show_norm_scores:
            continue
        scores_so_far, rows, prev = [], [], None
        for date in all_dates:
            if date not in show_norm_scores[name]:
                continue
            norm, raw = show_norm_scores[name][date]
            scores_so_far.append(norm)
            rows.append({
                "show_id":      show_id,
                "score":        norm,
                "trends_score": round(raw, 1),
                "status":       score_to_status(norm, prev),
                "trend":        score_to_trend(norm, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat(),
            })
            prev = norm
        if rows:
            sb.table("shows_history").insert(rows).execute()
            log.info(f"    {name}: {len(rows)} history rows")

    # Insert artists_history
    log.info("  Inserting artists_history...")
    for name, artist_id in artist_id_map.items():
        if name not in artist_norm_scores:
            continue
        scores_so_far, rows, prev = [], [], None
        for date in all_dates:
            if date not in artist_norm_scores[name]:
                continue
            norm, raw = artist_norm_scores[name][date]
            scores_so_far.append(norm)
            rows.append({
                "artist_id":    artist_id,
                "score":        norm,
                "trends_score": round(raw, 1),
                "status":       score_to_status(norm, prev),
                "trend":        score_to_trend(norm, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat(),
            })
            prev = norm
        if rows:
            sb.table("artists_history").insert(rows).execute()
            log.info(f"    {name}: {len(rows)} history rows")

    # Insert events_history
    log.info("  Inserting events_history...")
    for title, event_id in event_id_map.items():
        if title not in event_norm_scores:
            continue
        scores_so_far, rows, prev = [], [], None
        for date in all_dates:
            if date not in event_norm_scores[title]:
                continue
            norm, raw = event_norm_scores[title][date]
            scores_so_far.append(norm)
            rows.append({
                "event_id":     event_id,
                "score":        norm,
                "trends_score": round(raw, 1),
                "status":       "Upcoming",
                "trend":        score_to_trend(norm, prev),
                "sparkline":    build_sparkline(scores_so_far),
                "search_volume":0,
                "recorded_at":  datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat(),
            })
            prev = norm
        if rows:
            sb.table("events_history").insert(rows).execute()
            log.info(f"    {title[:40]}: {len(rows)} history rows")

    # ── DONE ──────────────────────────────────────────────────────────────
    log.info("=== INIT complete ===")
    log.info(f"  Shows:   {len(show_id_map)}")
    log.info(f"  Artists: {len(artist_id_map)}")
    log.info(f"  Events:  {len(event_id_map)}")
    log.info(f"  Top 5 genres: {top5}")
    log.info("  Database bootstrapped with real Google Trends data from 1st January")


if __name__ == "__main__":
    main()

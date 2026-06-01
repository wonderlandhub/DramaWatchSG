# Drama Watch SG

Tracking what Singapore searches in drama — shows, events, and artists ranked by real search momentum.

## Repository structure

```
drama-watch-sg/
├── index.html                        ← main website (GitHub Pages)
├── scraper.py                        ← data pipeline (runs via GitHub Actions)
├── supabase-schema.sql               ← run once in Supabase SQL editor
├── .github/
│   └── workflows/
│       └── scraper.yml               ← GitHub Actions workflow
└── README.md
```

## Setup guide

### 1. Supabase (database)
1. Create a free account at https://supabase.com
2. Create a new project
3. Go to SQL Editor → paste contents of `supabase-schema.sql` → Run
4. Go to Settings → API → copy your Project URL and anon key

### 2. Reddit API (not added)
1. Go to https://www.reddit.com/prefs/apps
2. Click "create another app" → select "script"
3. Copy your client ID and secret

### 3. GitHub repository
1. Create a new public repository on GitHub
2. Upload all files from this folder
3. Rename `index.html` — it will be your homepage
4. Go to Settings → Secrets and variables → Actions → add:
   - `SUPABASE_URL` — your Supabase project URL
   - `SUPABASE_KEY` — your Supabase anon key
   - `REDDIT_CLIENT_ID` — your Reddit app client ID
   - `REDDIT_SECRET` — your Reddit app secret

### 4. GitHub Pages (website hosting)
1. Go to Settings → Pages
2. Source: Deploy from a branch → main → / (root)
3. Your site will be live at: `https://yourusername.github.io/drama-watch-sg`

### 5. Connect website to live data
In `index.html`, find the comment that says:
```
// NOTE: In production, replace this section with a Supabase fetch:
```
Replace the mock data section with:
```javascript
const SUPABASE_URL = 'https://YOUR_PROJECT.supabase.co';
const SUPABASE_KEY = 'YOUR_ANON_KEY';

async function loadLiveData() {
  const [showsRes, artistsRes, eventsRes] = await Promise.all([
    fetch(`${SUPABASE_URL}/rest/v1/shows?select=*&order=score.desc`, {
      headers: { 'apikey': SUPABASE_KEY, 'Authorization': `Bearer ${SUPABASE_KEY}` }
    }),
    fetch(`${SUPABASE_URL}/rest/v1/artists?select=*&order=score.desc`, {
      headers: { 'apikey': SUPABASE_KEY, 'Authorization': `Bearer ${SUPABASE_KEY}` }
    }),
    fetch(`${SUPABASE_URL}/rest/v1/events?select=*&order=hot_score.desc`, {
      headers: { 'apikey': SUPABASE_KEY, 'Authorization': `Bearer ${SUPABASE_KEY}` }
    }),
  ]);
  const shows   = await showsRes.json();
  const artists = await artistsRes.json();
  const events  = await eventsRes.json();
  // Replace DRAMA, ARTISTS, EVENTS objects with live data
  // then call updateAll()
}

loadLiveData();
```

## Monthly cost: $0
- GitHub (repo + Actions + Pages): free for public repos
- Supabase: free tier (500MB, 2GB transfer)
- Google Trends via pytrends: free (unofficial API)
- Reddit API: free tier (100 req/min)
- RSS feeds: free

## Pipeline schedule
Runs automatically every 6 hours via GitHub Actions cron.
You can also trigger it manually from the Actions tab in GitHub.

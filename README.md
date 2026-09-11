# Fantasy Football Auto-Analyst

Pulls your ESPN league automatically every week, simulates the rest of
the season 10,000 times, and publishes an HTML report with standings,
playoff odds, start/sit guidance, and waiver targets. No manual data
entry, no server to run — it lives entirely in this free GitHub repo.

## One-time setup (~10 minutes)

### 1. Create the repo
Create a new GitHub repo and upload these files, keeping the folder
structure (`analyze.py`, `requirements.txt`, and
`.github/workflows/weekly_report.yml` all at the paths shown).

### 2. Get your ESPN credentials
These identify you to ESPN's API so it can read your league (this
works for private leagues too):

1. Log into your league at fantasy.espn.com in a desktop browser.
2. Open Developer Tools (F12 or right-click → Inspect) → **Application**
   tab (Chrome) or **Storage** tab (Firefox) → **Cookies** →
   `fantasy.espn.com`.
3. Copy the values of the `espn_s2` and `SWID` cookies (SWID includes
   the curly braces, e.g. `{ABC123...}`).
4. Your **League ID** is the number in your league's URL, e.g.
   `.../leagueId=123456`.
5. Your **Team ID** is the number in your team's URL, e.g.
   `.../teamId=4`. (Find it by clicking your team.)

### 3. Add secrets to the repo
In your repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret name | Value |
|---|---|
| `LEAGUE_ID` | your league ID |
| `SEASON_YEAR` | e.g. `2026` |
| `ESPN_S2` | the espn_s2 cookie value |
| `SWID` | the SWID cookie value (with braces) |
| `MY_TEAM_ID` | your team ID |

### 4. Turn on GitHub Pages
**Settings → Pages** → Source: "Deploy from a branch" → Branch:
`main`, folder: `/docs`. Save. Your live report will appear at
`https://<your-username>.github.io/<repo-name>/` a minute or two after
the first run.

### 5. Run it
Go to the **Actions** tab → "Weekly Fantasy Football Report" →
**Run workflow** to generate your first report immediately. After
that, it runs automatically every Tuesday morning all season — no
action needed from you.

## Using the Trade Analyzer
No need to edit any files or run anything locally:

1. In your repo, go to the **Actions** tab.
2. Click **Trade Analyzer** in the left sidebar.
3. Click **Run workflow**. Fill in the form:
   - Team A - Team ID / what Team A sends (comma-separated player names)
   - Team B - Team ID / what Team B sends (comma-separated player names)
4. Click **Run workflow** to confirm. It finishes in under a minute.
5. View the result at `https://<your-username>.github.io/<repo-name>/trade_result.html`
   — shows both teams' projected wins and playoff odds before vs. after
   the trade.

This only simulates the trade; it never touches your actual ESPN
roster.

## Maintenance
Essentially none. The one thing that can happen: ESPN's `espn_s2` /
`SWID` cookies occasionally expire (roughly once or twice a season).
If a run fails with an auth error, just repeat step 2 and update the
two secrets — takes under a minute.

## Notes on the model
Player scoring is modeled as a normal distribution using each
player's actual game-log mean and standard deviation so far this
season (falling back to ESPN's own projection early in the year or
after a bye). The simulation plays out every remaining matchup 10,000
times to estimate final win totals and playoff odds — it's a solid
directional read, not a guarantee (injuries and matchup-specific
factors aren't modeled).

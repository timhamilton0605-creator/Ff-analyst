#!/usr/bin/env python3
"""
ESPN Fantasy Football Auto-Analyst
-----------------------------------
Pulls your league directly from ESPN (no manual data entry), builds a
scoring distribution for every rostered player from their actual game
log this season, runs a Monte Carlo simulation of the rest of the
schedule, and writes out an HTML report with:
  - projected final standings + playoff odds
  - your roster ranked by projected value & volatility (start/sit help)
  - top available waiver-wire targets

All configuration comes from environment variables so this can run
unattended in GitHub Actions with zero weekly upkeep.
"""

import os
import json
import random
import statistics
from pathlib import Path
from datetime import datetime

from espn_api.football import League

# ---------- Config (set these as GitHub Actions secrets) ----------
LEAGUE_ID = int(os.environ["LEAGUE_ID"])
SEASON_YEAR = int(os.environ.get("SEASON_YEAR", datetime.now().year))
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]
MY_TEAM_ID = os.environ.get("MY_TEAM_ID")
MY_TEAM_ID = int(MY_TEAM_ID) if MY_TEAM_ID else None
N_SIMULATIONS = int(os.environ.get("N_SIMULATIONS", "10000"))

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_league():
    return League(league_id=LEAGUE_ID, year=SEASON_YEAR, espn_s2=ESPN_S2, swid=SWID)


def build_player_models(league, current_week):
    """
    {player_name: (mean, std)} for every rostered player. Uses this
    season's game log where we have enough data, and falls back to
    ESPN's own season projection otherwise (e.g. week 1, or byes).

    Fetches each past week's box score exactly once (not once per
    player) — this is the data that feeds every player's model.
    """
    player_scores = {}
    for wk in range(1, current_week):
        try:
            box_scores = league.box_scores(wk)
        except Exception:
            continue
        for bs in box_scores:
            for lineup in (bs.home_lineup, bs.away_lineup):
                for p in lineup:
                    if p.points:
                        player_scores.setdefault(p.name, []).append(p.points)

    models = {}
    for team in league.teams:
        for player in team.roster:
            hist = player_scores.get(player.name, [])
            if len(hist) >= 2:
                mean = statistics.mean(hist)
                std = statistics.stdev(hist)
            elif hist:
                mean = hist[0]
                std = max(mean * 0.35, 2.0)
            else:
                proj = getattr(player, "projected_total_points", 0) or 0
                mean = proj / max(current_week, 1)
                std = max(mean * 0.4, 2.0)
            models[player.name] = (mean, std)
    return models


def get_starting_lineup(team):
    return [p for p in team.roster if p.lineupSlot not in ("BE", "IR")]


def team_starter_names(team):
    return [p.name for p in get_starting_lineup(team)]


def simulate_remaining_season(league, models, current_week, n_sims=10000, lineup_override=None):
    """
    Monte Carlo sim of every remaining regular-season matchup.

    lineup_override: optional {team_id: [player_name, ...]} to substitute
    in place of a team's actual current roster — this is how the trade
    analyzer evaluates a hypothetical trade without touching your real
    ESPN roster.

    Each remaining week's matchups/rosters are fetched from ESPN exactly
    once, up front — the actual n_sims loop below runs purely on data
    already in memory, so it stays fast regardless of how many
    simulations you run.
    """
    lineup_override = lineup_override or {}
    teams = league.teams
    reg_season_weeks = league.settings.reg_season_count
    remaining_weeks = list(range(current_week, reg_season_weeks + 1))
    playoff_spots = league.settings.playoff_team_count

    weekly_matchups = {}
    for wk in remaining_weeks:
        try:
            matchups = league.box_scores(wk)
        except Exception:
            matchups = []
        pairs = []
        for m in matchups:
            if not m.home_team or not m.away_team:
                continue
            home_names = lineup_override.get(m.home_team.team_id) or team_starter_names(m.home_team)
            away_names = lineup_override.get(m.away_team.team_id) or team_starter_names(m.away_team)
            pairs.append((m.home_team.team_id, home_names, m.away_team.team_id, away_names))
        weekly_matchups[wk] = pairs

    win_totals = {team.team_id: [] for team in teams}

    for _ in range(n_sims):
        wins = {team.team_id: team.wins for team in teams}
        for wk in remaining_weeks:
            for home_id, home_names, away_id, away_names in weekly_matchups[wk]:
                home_score = sum(random.gauss(*models.get(n, (0, 4))) for n in home_names)
                away_score = sum(random.gauss(*models.get(n, (0, 4))) for n in away_names)
                if home_score >= away_score:
                    wins[home_id] += 1
                else:
                    wins[away_id] += 1
        ranked = sorted(wins.keys(), key=lambda t: -wins[t])
        made_playoffs = set(ranked[:playoff_spots])
        for tid in wins:
            win_totals[tid].append((wins[tid], tid in made_playoffs))

    avg_wins, playoff_odds = {}, {}
    for tid, results in win_totals.items():
        avg_wins[tid] = round(statistics.mean(r[0] for r in results), 2)
        playoff_odds[tid] = round(100 * sum(r[1] for r in results) / n_sims, 1)
    return avg_wins, playoff_odds


def simulate_this_week(league, models, my_team_id, current_week, n_sims=10000):
    """Win probability for MY_TEAM_ID's specific matchup this week only."""
    if not my_team_id:
        return None
    try:
        matchups = league.box_scores(current_week)
    except Exception:
        return None

    my_matchup = None
    for m in matchups:
        if m.home_team and m.home_team.team_id == my_team_id:
            my_matchup = (m.home_team, m.away_team, True)
            break
        if m.away_team and m.away_team.team_id == my_team_id:
            my_matchup = (m.home_team, m.away_team, False)
            break
    if not my_matchup or not my_matchup[0] or not my_matchup[1]:
        return None

    home_team, away_team, i_am_home = my_matchup
    wins = 0
    for _ in range(n_sims):
        home_score = sum(
            random.gauss(*models.get(p.name, (p.points or 0, 4))) for p in get_starting_lineup(home_team)
        )
        away_score = sum(
            random.gauss(*models.get(p.name, (p.points or 0, 4))) for p in get_starting_lineup(away_team)
        )
        if (i_am_home and home_score >= away_score) or (not i_am_home and away_score > home_score):
            wins += 1

    opponent = away_team if i_am_home else home_team
    return {"opponent": opponent.team_name, "win_prob": round(100 * wins / n_sims, 1)}


def waiver_suggestions(free_agents, models, top_n=15):
    """Best available free agents by season-long modeled value."""
    ranked = sorted(
        free_agents,
        key=lambda p: models.get(p.name, (getattr(p, "projected_total_points", 0) or 0, 0))[0],
        reverse=True,
    )
    return ranked[:top_n]


def current_week_projection(player, current_week, season_mean):
    """Pull this specific week's projection if ESPN exposes it, else fall back."""
    try:
        stat = player.stats.get(current_week, {})
        proj = stat.get("projected_points")
        if proj:
            return proj
    except Exception:
        pass
    return season_mean


def one_week_add_candidates(free_agents, models, current_week, top_n=8, min_season_mean=4.0):
    """
    Free agents whose projection THIS week spikes well above their season
    average — bye-week fill-ins, plus-matchup streamers, etc. Filtered to
    players with some baseline floor so it's not just noise.
    """
    candidates = []
    for p in free_agents:
        season_mean, _ = models.get(p.name, (getattr(p, "projected_total_points", 0) or 0, 0))
        if season_mean < min_season_mean:
            continue
        week_proj = current_week_projection(p, current_week, season_mean)
        spike = week_proj - season_mean
        if spike > 0:
            candidates.append((p, round(week_proj, 1), round(spike, 1)))
    candidates.sort(key=lambda c: -c[2])
    return candidates[:top_n]


def start_sit_for_my_team(league, models):
    my_team = next((t for t in league.teams if t.team_id == MY_TEAM_ID), None)
    if not my_team:
        return None, []
    recs = []
    for p in my_team.roster:
        mean, std = models.get(p.name, (0, 0))
        recs.append((p.name, p.position, p.lineupSlot, round(mean, 1), round(std, 1)))
    recs.sort(key=lambda r: -r[3])
    return my_team, recs


def render_html(league, current_week, avg_wins, playoff_odds, waivers, streamers, my_team, my_recs, this_week=None):
    team_lookup = {t.team_id: t.team_name for t in league.teams}
    rows = sorted(avg_wins.keys(), key=lambda tid: -avg_wins[tid])

    # ---- top summary cards ----
    my_odds = playoff_odds.get(MY_TEAM_ID, None) if my_team else None
    my_rank = (rows.index(MY_TEAM_ID) + 1) if (my_team and MY_TEAM_ID in rows) else None
    top_waiver = waivers[0] if waivers else None
    top_streamer = streamers[0] if streamers else None

    cards = []
    if this_week:
        cards.append(("This Week vs " + this_week["opponent"], f"{this_week['win_prob']}% to win"))
    if my_team:
        cards.append(("Your Rank", f"#{my_rank} of {len(rows)}" if my_rank else "—"))
        cards.append(("Playoff Odds", f"{my_odds}%" if my_odds is not None else "—"))
    if top_waiver:
        cards.append(("Top Free Agent", f"{top_waiver.name} ({top_waiver.position})"))
    if top_streamer:
        p, week_proj, spike = top_streamer
        cards.append(("Top One-Week Add", f"{p.name} (+{spike} pts this wk)"))

    card_html = "".join(
        f'<div class="stat-card"><div class="stat-label">{label}</div>'
        f'<div class="stat-value">{value}</div></div>'
        for label, value in cards
    )

    html = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fantasy Football Dashboard — Week {current_week}</title>
<style>
:root{{--blue:#0b5cff;--bg:#f4f6fb;--card:#fff;--border:#e3e7ef;}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:1000px;margin:0 auto;padding:1.25rem;
background:var(--bg);color:#1c2333}}
h1{{color:var(--blue);margin-bottom:0.15rem;font-size:1.5rem}}
.meta{{color:#667;font-size:0.85rem;margin-bottom:1.25rem}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0.75rem;margin-bottom:1.75rem}}
.stat-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:0.9rem 1rem}}
.stat-label{{font-size:0.75rem;color:#667;text-transform:uppercase;letter-spacing:.03em}}
.stat-value{{font-size:1.15rem;font-weight:600;margin-top:0.2rem;color:var(--blue)}}
.section{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:1rem 1.1rem;margin-bottom:1.25rem}}
.section h2{{margin-top:0;font-size:1.05rem;color:var(--blue)}}
.section p.note{{color:#667;font-size:0.8rem;margin-top:-0.4rem}}
table{{border-collapse:collapse;width:100%;font-size:0.9rem}}
th,td{{border-bottom:1px solid var(--border);padding:7px 8px;text-align:left}}
th{{color:#556;font-size:0.75rem;text-transform:uppercase;letter-spacing:.02em}}
tr:hover{{background:#fafbff}}
.badge{{display:inline-block;background:#e8f0ff;color:var(--blue);border-radius:6px;padding:1px 7px;font-size:0.78rem;font-weight:600}}
</style></head><body>
<h1>Fantasy Football Dashboard</h1>
<p class="meta">{league.settings.name} &bull; Week {current_week} &bull; Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="stats-grid">{card_html}</div>

<div class="section">
<h2>Projected Final Standings &amp; Playoff Odds</h2>
<table><tr><th>Team</th><th>Proj. Wins</th><th>Playoff Odds</th></tr>"""]

    for tid in rows:
        highlight = ' style="font-weight:700"' if tid == MY_TEAM_ID else ""
        html.append(
            f"<tr{highlight}><td>{team_lookup.get(tid, tid)}</td><td>{avg_wins[tid]}</td>"
            f"<td><span class=\"badge\">{playoff_odds.get(tid, 0)}%</span></td></tr>"
        )
    html.append("</table></div>")

    if my_team:
        html.append(f'<div class="section"><h2>Your Roster — {my_team.team_name}</h2>')
        html.append(
            "<table><tr><th>Player</th><th>Pos</th><th>Slot</th>"
            "<th>Proj. Mean</th><th>Volatility</th></tr>"
        )
        for name, pos, slot, mean, std in my_recs:
            html.append(f"<tr><td>{name}</td><td>{pos}</td><td>{slot}</td><td>{mean}</td><td>{std}</td></tr>")
        html.append("</table></div>")

    html.append(
        '<div class="section"><h2>Best Available Free Agents</h2>'
        '<p class="note">Ranked by season-long projected value.</p>'
        "<table><tr><th>Player</th><th>Position</th><th>NFL Team</th></tr>"
    )
    for p in waivers:
        html.append(f"<tr><td>{p.name}</td><td>{p.position}</td><td>{p.proTeam}</td></tr>")
    html.append("</table></div>")

    html.append(
        '<div class="section"><h2>One-Week Add Candidates</h2>'
        '<p class="note">Free agents projected well above their own season average '
        "this week specifically — bye-week fill-ins and favorable-matchup streamers.</p>"
        "<table><tr><th>Player</th><th>Position</th><th>This Week Proj.</th><th>Spike vs. Avg</th></tr>"
    )
    for p, week_proj, spike in streamers:
        html.append(f"<tr><td>{p.name}</td><td>{p.position}</td><td>{week_proj}</td><td>+{spike}</td></tr>")
    html.append("</table></div>")

    html.append("</body></html>")
    return "\n".join(html)


def main():
    league = load_league()
    current_week = league.current_week or 1

    models = build_player_models(league, current_week)
    avg_wins, playoff_odds = simulate_remaining_season(league, models, current_week, N_SIMULATIONS)

    free_agents = league.free_agents(size=200)
    waivers = waiver_suggestions(free_agents, models)
    streamers = one_week_add_candidates(free_agents, models, current_week)
    my_team, my_recs = start_sit_for_my_team(league, models)
    this_week = simulate_this_week(league, models, MY_TEAM_ID, current_week, N_SIMULATIONS)

    html = render_html(league, current_week, avg_wins, playoff_odds, waivers, streamers, my_team, my_recs, this_week)
    (OUTPUT_DIR / "index.html").write_text(html)

    summary = {
        "week": current_week,
        "avg_wins": avg_wins,
        "playoff_odds": playoff_odds,
        "waivers": [p.name for p in waivers],
        "one_week_adds": [{"name": p.name, "week_proj": wp, "spike": sp} for p, wp, sp in streamers],
        "this_week": this_week,
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Report written to {OUTPUT_DIR}/index.html")


if __name__ == "__main__":
    main()

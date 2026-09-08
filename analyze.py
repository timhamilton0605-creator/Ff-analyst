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


def player_history(league, player_name, current_week):
    """Actual per-week scores so far this season, for variance estimation."""
    scores = []
    for wk in range(1, current_week):
        try:
            box_scores = league.box_scores(wk)
        except Exception:
            continue
        for bs in box_scores:
            for lineup in (bs.home_lineup, bs.away_lineup):
                for p in lineup:
                    if p.name == player_name and p.points:
                        scores.append(p.points)
    return scores


def build_player_models(league, current_week):
    """
    {player_name: (mean, std)} for every rostered player. Uses this
    season's game log where we have enough data, and falls back to
    ESPN's own season projection otherwise (e.g. week 1, or byes).
    """
    models = {}
    for team in league.teams:
        for player in team.roster:
            hist = player_history(league, player.name, current_week)
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


def simulate_remaining_season(league, models, current_week, n_sims=10000):
    """Monte Carlo sim of every remaining regular-season matchup."""
    teams = league.teams
    reg_season_weeks = league.settings.reg_season_count
    remaining_weeks = range(current_week, reg_season_weeks + 1)
    playoff_spots = league.settings.playoff_team_count

    win_totals = {team.team_id: [] for team in teams}

    for _ in range(n_sims):
        wins = {team.team_id: team.wins for team in teams}
        for wk in remaining_weeks:
            try:
                matchups = league.box_scores(wk)
            except Exception:
                continue
            for m in matchups:
                if not m.home_team or not m.away_team:
                    continue
                home_score = sum(
                    random.gauss(*models.get(p.name, (p.points or 0, 4)))
                    for p in get_starting_lineup(m.home_team)
                )
                away_score = sum(
                    random.gauss(*models.get(p.name, (p.points or 0, 4)))
                    for p in get_starting_lineup(m.away_team)
                )
                if home_score >= away_score:
                    wins[m.home_team.team_id] += 1
                else:
                    wins[m.away_team.team_id] += 1
        ranked = sorted(wins.keys(), key=lambda t: -wins[t])
        made_playoffs = set(ranked[:playoff_spots])
        for tid in wins:
            win_totals[tid].append((wins[tid], tid in made_playoffs))

    avg_wins, playoff_odds = {}, {}
    for tid, results in win_totals.items():
        avg_wins[tid] = round(statistics.mean(r[0] for r in results), 2)
        playoff_odds[tid] = round(100 * sum(r[1] for r in results) / n_sims, 1)
    return avg_wins, playoff_odds


def waiver_suggestions(league, models, top_n=10):
    free_agents = league.free_agents(size=200)
    ranked = sorted(
        free_agents,
        key=lambda p: models.get(p.name, (getattr(p, "projected_total_points", 0) or 0, 0))[0],
        reverse=True,
    )
    return ranked[:top_n]


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


def render_html(league, current_week, avg_wins, playoff_odds, waivers, my_team, my_recs):
    team_lookup = {t.team_id: t.team_name for t in league.teams}
    rows = sorted(avg_wins.keys(), key=lambda tid: -avg_wins[tid])

    html = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Fantasy Football Weekly Report — Week {current_week}</title>
<style>
body{{font-family:-apple-system,Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#0b5cff;color:#fff}}
tr:nth-child(even){{background:#f7f7f7}}
h1,h2{{color:#0b5cff}}
.meta{{color:#666;font-size:0.9rem}}
</style></head><body>
<h1>Fantasy Football Weekly Report</h1>
<p class="meta">League: {league.settings.name} &bull; Week {current_week} &bull; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<h2>Projected Final Standings &amp; Playoff Odds</h2>
<table><tr><th>Team</th><th>Projected Wins</th><th>Playoff Odds</th></tr>"""]

    for tid in rows:
        html.append(
            f"<tr><td>{team_lookup.get(tid, tid)}</td><td>{avg_wins[tid]}</td>"
            f"<td>{playoff_odds.get(tid, 0)}%</td></tr>"
        )
    html.append("</table>")

    if my_team:
        html.append(f"<h2>Your Roster — {my_team.team_name}</h2>")
        html.append(
            "<table><tr><th>Player</th><th>Pos</th><th>Slot</th>"
            "<th>Proj. Mean</th><th>Volatility (std)</th></tr>"
        )
        for name, pos, slot, mean, std in my_recs:
            html.append(f"<tr><td>{name}</td><td>{pos}</td><td>{slot}</td><td>{mean}</td><td>{std}</td></tr>")
        html.append("</table>")

    html.append("<h2>Top Waiver Wire Targets</h2><table><tr><th>Player</th><th>Position</th><th>Team</th></tr>")
    for p in waivers:
        html.append(f"<tr><td>{p.name}</td><td>{p.position}</td><td>{p.proTeam}</td></tr>")
    html.append("</table></body></html>")

    return "\n".join(html)


def main():
    league = load_league()
    current_week = league.current_week or 1

    models = build_player_models(league, current_week)
    avg_wins, playoff_odds = simulate_remaining_season(league, models, current_week, N_SIMULATIONS)
    waivers = waiver_suggestions(league, models)
    my_team, my_recs = start_sit_for_my_team(league, models)

    html = render_html(league, current_week, avg_wins, playoff_odds, waivers, my_team, my_recs)
    (OUTPUT_DIR / "index.html").write_text(html)

    summary = {
        "week": current_week,
        "avg_wins": avg_wins,
        "playoff_odds": playoff_odds,
        "waivers": [p.name for p in waivers],
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Report written to {OUTPUT_DIR}/index.html")


if __name__ == "__main__":
    main()

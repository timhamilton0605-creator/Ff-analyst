#!/usr/bin/env python3
"""
Trade Analyzer
--------------
Evaluates a hypothetical trade by re-running the season simulation
with the two rosters swapped, and reports the before/after change in
projected wins and playoff odds for both teams. Nothing is actually
transacted on ESPN — this is purely a "should I make this trade?"
what-if tool.

Run from the command line (or via the trade_analyzer GitHub Actions
workflow, which exposes these as form inputs):

    python trade_analyzer.py \\
        --team-a-id 3 --team-a-gives "Player One,Player Two" \\
        --team-b-id 7 --team-b-gives "Player Three"

Uses the same LEAGUE_ID / SEASON_YEAR / ESPN_S2 / SWID environment
variables as analyze.py.
"""

import os
import argparse
import json
from pathlib import Path
from datetime import datetime

from analyze import (
    load_league,
    build_player_models,
    compute_league_avg_allowed,
    simulate_remaining_season,
    team_starter_names,
    N_SIMULATIONS,
)

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_names(raw):
    if not raw:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


def find_team(league, team_id):
    return next((t for t in league.teams if t.team_id == team_id), None)


def apply_trade(starters_a, starters_b, a_gives, b_gives):
    """Return (new_starters_a, new_starters_b) after swapping named players."""
    new_a = [n for n in starters_a if n not in a_gives] + b_gives
    new_b = [n for n in starters_b if n not in b_gives] + a_gives
    return new_a, new_b


def main():
    parser = argparse.ArgumentParser(description="Evaluate a hypothetical trade.")
    parser.add_argument("--team-a-id", type=int, required=True)
    parser.add_argument("--team-a-gives", type=str, default="", help="Comma-separated player names Team A sends")
    parser.add_argument("--team-b-id", type=int, required=True)
    parser.add_argument("--team-b-gives", type=str, default="", help="Comma-separated player names Team B sends")
    parser.add_argument("--n-sims", type=int, default=N_SIMULATIONS)
    args = parser.parse_args()

    league = load_league()
    current_week = league.current_week or 1
    models, _, defense_vs_position = build_player_models(league, current_week)
    league_avg_allowed = compute_league_avg_allowed(defense_vs_position)

    team_a = find_team(league, args.team_a_id)
    team_b = find_team(league, args.team_b_id)
    if not team_a or not team_b:
        raise SystemExit("Could not find one or both team IDs in this league.")

    a_gives = parse_names(args.team_a_gives)
    b_gives = parse_names(args.team_b_gives)

    # ---- baseline (no trade) ----
    baseline_wins, baseline_odds = simulate_remaining_season(
        league, models, current_week, args.n_sims,
        defense_vs_position=defense_vs_position, league_avg_allowed=league_avg_allowed,
    )

    # ---- post-trade ----
    starters_a = team_starter_names(team_a)
    starters_b = team_starter_names(team_b)
    new_a, new_b = apply_trade(starters_a, starters_b, a_gives, b_gives)
    override = {team_a.team_id: new_a, team_b.team_id: new_b}
    trade_wins, trade_odds = simulate_remaining_season(
        league, models, current_week, args.n_sims, override,
        defense_vs_position=defense_vs_position, league_avg_allowed=league_avg_allowed,
    )

    result = {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "trade": {
            f"{team_a.team_name} sends": a_gives,
            f"{team_b.team_name} sends": b_gives,
        },
        "team_a": {
            "name": team_a.team_name,
            "wins_before": baseline_wins.get(team_a.team_id),
            "wins_after": trade_wins.get(team_a.team_id),
            "playoff_odds_before": baseline_odds.get(team_a.team_id),
            "playoff_odds_after": trade_odds.get(team_a.team_id),
        },
        "team_b": {
            "name": team_b.team_name,
            "wins_before": baseline_wins.get(team_b.team_id),
            "wins_after": trade_wins.get(team_b.team_id),
            "playoff_odds_before": baseline_odds.get(team_b.team_id),
            "playoff_odds_after": trade_odds.get(team_b.team_id),
        },
    }

    print(json.dumps(result, indent=2))

    for key in ("team_a", "team_b"):
        t = result[key]
        win_delta = round(t["wins_after"] - t["wins_before"], 2)
        odds_delta = round(t["playoff_odds_after"] - t["playoff_odds_before"], 1)
        sign_w = "+" if win_delta >= 0 else ""
        sign_o = "+" if odds_delta >= 0 else ""
        print(
            f"\n{t['name']}: proj wins {t['wins_before']} -> {t['wins_after']} "
            f"({sign_w}{win_delta}), playoff odds {t['playoff_odds_before']}% -> "
            f"{t['playoff_odds_after']}% ({sign_o}{odds_delta} pts)"
        )

    (OUTPUT_DIR / "trade_result.json").write_text(json.dumps(result, indent=2))
    write_html(result)


def write_html(result):
    def team_block(t):
        win_delta = round(t["wins_after"] - t["wins_before"], 2)
        odds_delta = round(t["playoff_odds_after"] - t["playoff_odds_before"], 1)
        return f"""
        <div class="section">
          <h2>{t['name']}</h2>
          <table>
            <tr><th></th><th>Before</th><th>After</th><th>Change</th></tr>
            <tr><td>Projected Wins</td><td>{t['wins_before']}</td><td>{t['wins_after']}</td>
                <td>{'+' if win_delta>=0 else ''}{win_delta}</td></tr>
            <tr><td>Playoff Odds</td><td>{t['playoff_odds_before']}%</td><td>{t['playoff_odds_after']}%</td>
                <td>{'+' if odds_delta>=0 else ''}{odds_delta} pts</td></tr>
          </table>
        </div>"""

    trade_lines = "".join(
        f"<li>{side}: {', '.join(players) if players else '(nothing)'}</li>"
        for side, players in result["trade"].items()
    )

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Analysis</title>
<style>
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:700px;margin:0 auto;padding:1.25rem;
background:#f4f6fb;color:#1c2333}}
h1{{color:#0b5cff;font-size:1.4rem}}
.section{{background:#fff;border:1px solid #e3e7ef;border-radius:10px;padding:1rem 1.1rem;margin-bottom:1.25rem}}
table{{border-collapse:collapse;width:100%;font-size:0.9rem}}
th,td{{border-bottom:1px solid #e3e7ef;padding:7px 8px;text-align:left}}
.meta{{color:#667;font-size:0.85rem}}
</style></head><body>
<h1>Trade Analysis</h1>
<p class="meta">Generated {result['generated']}</p>
<div class="section"><h2>Proposed Trade</h2><ul>{trade_lines}</ul></div>
{team_block(result['team_a'])}
{team_block(result['team_b'])}
</body></html>"""
    (OUTPUT_DIR / "trade_result.html").write_text(html)


if __name__ == "__main__":
    main()

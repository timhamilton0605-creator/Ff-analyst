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
N_SIMULATIONS = int(os.environ.get("N_SIMULATIONS", "2000"))

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_league():
    return League(league_id=LEAGUE_ID, year=SEASON_YEAR, espn_s2=ESPN_S2, swid=SWID)


def build_player_models(league, current_week, extra_players=None):
    """
    {player_name: (mean, std)} for every rostered player, plus any
    extra_players supplied (used to model free agents too, so waiver/
    streaming suggestions get a real per-game estimate instead of
    misreading a season-long total as a per-game number).

    Fetches each past week's box score exactly once (not once per
    player) — this is the data that feeds every player's model.

    Returns (models, player_scores, defense_vs_position):
      - player_scores: each player's raw per-week actual points, reused
        by the one-week-add logic to spot recent usage spikes.
      - defense_vs_position: {(opponent_team_abbr, position): avg points
        allowed} built from this league's own historical box scores —
        a real matchup-difficulty figure with no extra data source.
    """
    player_scores = {}
    defense_vs_position_raw = {}
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
                        opp = getattr(p, "pro_opponent", None)
                        pos = getattr(p, "position", None)
                        if opp and pos:
                            defense_vs_position_raw.setdefault((opp, pos), []).append(p.points)

    reg_season_weeks = league.settings.reg_season_count or 17

    def model_for(player):
        hist = player_scores.get(player.name, [])
        if len(hist) >= 2:
            mean = statistics.mean(hist)
            std = statistics.stdev(hist)
        elif hist:
            mean = hist[0]
            std = max(mean * 0.35, 2.0)
        else:
            proj = getattr(player, "projected_total_points", 0) or 0
            mean = proj / reg_season_weeks
            std = max(mean * 0.4, 2.0)
        return (mean, std)

    models = {}
    for team in league.teams:
        for player in team.roster:
            models[player.name] = model_for(player)
    for player in extra_players or []:
        if player.name not in models:
            models[player.name] = model_for(player)

    defense_vs_position = {
        key: round(statistics.mean(vals), 1) for key, vals in defense_vs_position_raw.items()
    }
    return models, player_scores, defense_vs_position


def compute_league_avg_allowed(defense_vs_position):
    """Average points allowed per position, across all NFL defenses — the baseline
    a specific opponent's number gets compared against to form a matchup factor."""
    by_position = {}
    for (team, pos), avg_allowed in defense_vs_position.items():
        by_position.setdefault(pos, []).append(avg_allowed)
    return {pos: round(statistics.mean(vals), 2) for pos, vals in by_position.items()}


def matchup_adjusted_dist(models, defense_vs_position, league_avg_allowed, name, position, opponent):
    """
    A player's (mean, std) nudged by their actual upcoming opponent's
    defense-vs-position number. E.g. Rashee Rice facing a Broncos defense
    that's allowed well below the league-average points to WRs this
    season gets his mean scaled down accordingly (and up against a weak
    defense). Clamped to +/-30% so one small-sample week can't swing it
    to an unrealistic extreme.
    """
    mean, std = models.get(name, (0, 4))
    league_avg = league_avg_allowed.get(position)
    allowed = defense_vs_position.get((opponent, position)) if opponent else None
    if league_avg and allowed:
        factor = max(0.7, min(1.3, allowed / league_avg))
        mean = mean * factor
    return (mean, std)


def get_starting_lineup(team):
    return [p for p in team.roster if p.lineupSlot not in ("BE", "IR")]


def team_starter_names(team):
    return [p.name for p in get_starting_lineup(team)]


def simulate_remaining_season(league, models, current_week, n_sims=10000, lineup_override=None,
                               defense_vs_position=None, league_avg_allowed=None):
    """
    Monte Carlo sim of every remaining regular-season matchup. Each
    player's weekly distribution is adjusted for their actual opponent
    that week (see matchup_adjusted_dist) — not just a flat season
    average repeated every week.

    lineup_override: optional {team_id: [player_name, ...]} to substitute
    in place of a team's actual current roster — this is how the trade
    analyzer evaluates a hypothetical trade without touching your real
    ESPN roster. Overridden teams fall back to season-average (no
    matchup adjustment), since we don't know the added player's
    real per-week opponent history on that roster.

    Each remaining week's matchups/rosters are fetched from ESPN exactly
    once, up front — the actual n_sims loop below runs purely on data
    already in memory, so it stays fast regardless of how many
    simulations you run.
    """
    lineup_override = lineup_override or {}
    defense_vs_position = defense_vs_position or {}
    league_avg_allowed = league_avg_allowed or {}
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

            if m.home_team.team_id in lineup_override:
                home_dists = [models.get(n, (0, 4)) for n in lineup_override[m.home_team.team_id]]
            else:
                home_dists = [
                    matchup_adjusted_dist(
                        models, defense_vs_position, league_avg_allowed,
                        bp.name, bp.position, getattr(bp, "pro_opponent", None),
                    )
                    for bp in m.home_lineup if bp.lineupSlot not in ("BE", "IR")
                ]

            if m.away_team.team_id in lineup_override:
                away_dists = [models.get(n, (0, 4)) for n in lineup_override[m.away_team.team_id]]
            else:
                away_dists = [
                    matchup_adjusted_dist(
                        models, defense_vs_position, league_avg_allowed,
                        bp.name, bp.position, getattr(bp, "pro_opponent", None),
                    )
                    for bp in m.away_lineup if bp.lineupSlot not in ("BE", "IR")
                ]

            pairs.append((m.home_team.team_id, home_dists, m.away_team.team_id, away_dists))
        weekly_matchups[wk] = pairs

    win_totals = {team.team_id: [] for team in teams}

    for _ in range(n_sims):
        wins = {team.team_id: team.wins for team in teams}
        for wk in remaining_weeks:
            for home_id, home_dists, away_id, away_dists in weekly_matchups[wk]:
                home_score = sum(random.gauss(*d) for d in home_dists)
                away_score = sum(random.gauss(*d) for d in away_dists)
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


def simulate_this_week(league, models, my_team_id, current_week, n_sims=10000,
                        defense_vs_position=None, league_avg_allowed=None):
    """Win probability for MY_TEAM_ID's specific matchup this week only, with each
    player's distribution adjusted for their actual opponent that week."""
    defense_vs_position = defense_vs_position or {}
    league_avg_allowed = league_avg_allowed or {}
    if not my_team_id:
        return None
    try:
        matchups = league.box_scores(current_week)
    except Exception:
        return None

    my_matchup = None
    for m in matchups:
        if m.home_team and m.home_team.team_id == my_team_id:
            my_matchup = (m.home_team, m.away_team, m.home_lineup, m.away_lineup, True)
            break
        if m.away_team and m.away_team.team_id == my_team_id:
            my_matchup = (m.home_team, m.away_team, m.home_lineup, m.away_lineup, False)
            break
    if not my_matchup or not my_matchup[0] or not my_matchup[1]:
        return None

    home_team, away_team, home_lineup, away_lineup, i_am_home = my_matchup

    def dists_for(lineup):
        return [
            matchup_adjusted_dist(
                models, defense_vs_position, league_avg_allowed,
                bp.name, bp.position, getattr(bp, "pro_opponent", None),
            )
            for bp in lineup if bp.lineupSlot not in ("BE", "IR")
        ]

    home_dists = dists_for(home_lineup)
    away_dists = dists_for(away_lineup)

    wins = 0
    for _ in range(n_sims):
        home_score = sum(random.gauss(*d) for d in home_dists)
        away_score = sum(random.gauss(*d) for d in away_dists)
        if (i_am_home and home_score >= away_score) or (not i_am_home and away_score > home_score):
            wins += 1

    opponent = away_team if i_am_home else home_team
    return {"opponent": opponent.team_name, "win_prob": round(100 * wins / n_sims, 1)}


def season_total_projection(player, mean, current_week, reg_season_weeks):
    """Points scored so far this season + modeled mean for each remaining week."""
    played_total = getattr(player, "total_points", 0) or 0
    remaining_weeks = max(reg_season_weeks - current_week + 1, 0)
    return round(played_total + mean * remaining_weeks, 1)


FA_POSITION_LIMITS = {"QB": 5, "RB": 8, "WR": 10, "TE": 5}


def tiered_free_agents(free_agents, models, league, current_week):
    """Top available free agents grouped by position, each with a season total projection."""
    reg_season_weeks = league.settings.reg_season_count or 17
    tiers = {}
    for pos, limit in FA_POSITION_LIMITS.items():
        pos_players = [p for p in free_agents if p.position == pos]
        pos_players.sort(key=lambda p: models.get(p.name, (0, 0))[0], reverse=True)
        rows = []
        for p in pos_players[:limit]:
            mean, _ = models.get(p.name, (0, 0))
            season_total = season_total_projection(p, mean, current_week, reg_season_weeks)
            rows.append((p, round(mean, 1), season_total))
        tiers[pos] = rows
    return tiers


def one_week_add_candidates(free_agents, models, player_scores, league, current_week, top_n=8, recent_window=2, min_recent_avg=4.0):
    """
    Free agents whose recent actual output (last `recent_window` games)
    is running well above their season average — this is what catches
    a usage bump from another player's injury, a role change, etc.,
    using real results instead of a forward projection.
    """
    reg_season_weeks = league.settings.reg_season_count or 17
    candidates = []
    for p in free_agents:
        season_mean, _ = models.get(p.name, (0, 0))
        hist = player_scores.get(p.name, [])
        if not hist:
            continue
        recent = hist[-recent_window:]
        recent_avg = statistics.mean(recent)
        spike = recent_avg - season_mean
        if spike > 0 and recent_avg >= min_recent_avg:
            season_total = season_total_projection(p, season_mean, current_week, reg_season_weeks)
            candidates.append((p, round(recent_avg, 1), round(spike, 1), season_total))
    candidates.sort(key=lambda c: -c[2])
    return candidates[:top_n]


def get_team_week_lineup(league, team_id, week):
    """{player_name: BoxPlayer} for a team's specific week, or {} if not found."""
    try:
        matchups = league.box_scores(week)
    except Exception:
        return {}
    for m in matchups:
        if m.home_team and m.home_team.team_id == team_id:
            return {bp.name: bp for bp in m.home_lineup}
        if m.away_team and m.away_team.team_id == team_id:
            return {bp.name: bp for bp in m.away_lineup}
    return {}


def start_sit_for_my_team(league, models, defense_vs_position, league_avg_allowed, current_week):
    my_team = next((t for t in league.teams if t.team_id == MY_TEAM_ID), None)
    if not my_team:
        return None, []
    reg_season_weeks = league.settings.reg_season_count or 17
    week_lineup = get_team_week_lineup(league, MY_TEAM_ID, current_week)

    recs = []
    for p in my_team.roster:
        mean, std = models.get(p.name, (0, 0))
        season_total = season_total_projection(p, mean, current_week, reg_season_weeks)
        bp = week_lineup.get(p.name)
        opponent = getattr(bp, "pro_opponent", None) if bp else None
        on_bye = bool(getattr(bp, "on_bye_week", False)) if bp else False
        adj_mean, _ = matchup_adjusted_dist(models, defense_vs_position, league_avg_allowed, p.name, p.position, opponent)
        this_week_proj = round(adj_mean, 1)
        matchup_val = defense_vs_position.get((opponent, p.position)) if opponent else None
        recs.append(
            (p.name, p.position, p.lineupSlot, round(mean, 1), round(std, 1),
             this_week_proj, opponent, on_bye, matchup_val, season_total)
        )
    recs.sort(key=lambda r: -r[3])
    return my_team, recs


def render_html(league, current_week, avg_wins, playoff_odds, tiers, streamers, my_team, my_recs, this_week=None):
    team_lookup = {t.team_id: t.team_name for t in league.teams}
    rows = sorted(avg_wins.keys(), key=lambda tid: -avg_wins[tid])

    # ---- top summary cards ----
    my_odds = playoff_odds.get(MY_TEAM_ID, None) if my_team else None
    my_rank = (rows.index(MY_TEAM_ID) + 1) if (my_team and MY_TEAM_ID in rows) else None
    all_tier_rows = [row for rows_ in tiers.values() for row in rows_]
    top_waiver = max(all_tier_rows, key=lambda r: r[1]) if all_tier_rows else None
    top_streamer = streamers[0] if streamers else None

    cards = []
    if this_week:
        cards.append(("This Week vs " + this_week["opponent"], f"{this_week['win_prob']}% to win"))
    if my_team:
        cards.append(("Your Rank", f"#{my_rank} of {len(rows)}" if my_rank else "—"))
        cards.append(("Playoff Odds", f"{my_odds}%" if my_odds is not None else "—"))
    if top_waiver:
        p, mean, _ = top_waiver
        cards.append(("Top Free Agent", f"{p.name} ({p.position})"))
    if top_streamer:
        p, recent_avg, spike, _ = top_streamer
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
            '<p class="note">Matchup = avg fantasy points that opponent\'s defense has '
            "allowed to that position this season (higher = more favorable matchup).</p>"
        )
        html.append(
            "<table><tr><th>Player</th><th>Pos</th><th>Slot</th><th>Season Avg</th>"
            "<th>Volatility</th><th>This Week Proj.</th><th>Opponent</th>"
            "<th>Matchup</th><th>Proj. Season Total</th></tr>"
        )
        for name, pos, slot, mean, std, week_proj, opponent, on_bye, matchup_val, season_total in my_recs:
            opp_display = "BYE" if on_bye else (opponent or "—")
            matchup_display = matchup_val if matchup_val is not None else "—"
            html.append(
                f"<tr><td>{name}</td><td>{pos}</td><td>{slot}</td><td>{mean}</td><td>{std}</td>"
                f"<td>{week_proj}</td><td>{opp_display}</td><td>{matchup_display}</td><td>{season_total}</td></tr>"
            )
        html.append("</table></div>")

    html.append(
        '<div class="section"><h2>Best Available Free Agents</h2>'
        '<p class="note">Top QBs, RBs, WRs &amp; TEs on the wire, ranked by projected value.</p>'
    )
    for pos, rows_ in tiers.items():
        html.append(f"<h3 style='margin-bottom:0.3rem'>{pos}</h3>")
        html.append(
            "<table style='margin-bottom:1rem'><tr><th>Player</th><th>NFL Team</th>"
            "<th>Proj. Mean</th><th>Proj. Season Total</th></tr>"
        )
        for p, mean, season_total in rows_:
            html.append(f"<tr><td>{p.name}</td><td>{p.proTeam}</td><td>{mean}</td><td>{season_total}</td></tr>")
        html.append("</table>")
    html.append("</div>")

    html.append(
        '<div class="section"><h2>One-Week Add Candidates</h2>'
        '<p class="note">Free agents whose last couple of games are running well above '
        "their season average — often a sign of a usage bump (e.g. an injury opening up "
        "targets/touches elsewhere).</p>"
        "<table><tr><th>Player</th><th>Position</th><th>Recent Avg</th><th>Spike vs. Season Avg</th><th>Proj. Season Total</th></tr>"
    )
    for p, recent_avg, spike, season_total in streamers:
        html.append(
            f"<tr><td>{p.name}</td><td>{p.position}</td><td>{recent_avg}</td>"
            f"<td>+{spike}</td><td>{season_total}</td></tr>"
        )
    html.append("</table></div>")

    html.append("</body></html>")
    return "\n".join(html)


def main():
    league = load_league()
    current_week = league.current_week or 1

    free_agents = league.free_agents(size=200)
    models, player_scores, defense_vs_position = build_player_models(
        league, current_week, extra_players=free_agents
    )
    league_avg_allowed = compute_league_avg_allowed(defense_vs_position)
    avg_wins, playoff_odds = simulate_remaining_season(
        league, models, current_week, N_SIMULATIONS,
        defense_vs_position=defense_vs_position, league_avg_allowed=league_avg_allowed,
    )

    tiers = tiered_free_agents(free_agents, models, league, current_week)
    streamers = one_week_add_candidates(free_agents, models, player_scores, league, current_week)
    my_team, my_recs = start_sit_for_my_team(league, models, defense_vs_position, league_avg_allowed, current_week)
    this_week = simulate_this_week(
        league, models, MY_TEAM_ID, current_week, N_SIMULATIONS,
        defense_vs_position=defense_vs_position, league_avg_allowed=league_avg_allowed,
    )

    html = render_html(league, current_week, avg_wins, playoff_odds, tiers, streamers, my_team, my_recs, this_week)
    (OUTPUT_DIR / "index.html").write_text(html)

    summary = {
        "week": current_week,
        "avg_wins": avg_wins,
        "playoff_odds": playoff_odds,
        "my_roster": [
            {
                "name": name, "position": pos, "season_avg": mean, "this_week_proj": wp,
                "opponent": ("BYE" if bye else opp), "matchup": mv, "season_total": st,
            }
            for name, pos, slot, mean, std, wp, opp, bye, mv, st in my_recs
        ],
        "free_agents": {
            pos: [{"name": p.name, "mean": mean, "season_total": st} for p, mean, st in rows_]
            for pos, rows_ in tiers.items()
        },
        "one_week_adds": [
            {"name": p.name, "recent_avg": ra, "spike": sp, "season_total": st}
            for p, ra, sp, st in streamers
        ],
        "this_week": this_week,
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Report written to {OUTPUT_DIR}/index.html")


if __name__ == "__main__":
    main()

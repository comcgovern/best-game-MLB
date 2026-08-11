"""Command line entry point: ``python -m mlbdelta <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import analytics, db, demo, ingest, server
from .analytics import DEFAULT_ROLE
from .config import DEFAULT_DB_PATH, DEFAULT_GAME_TYPES, Thresholds, current_season
from .statsapi import StatsApiClient


def _thresholds(args) -> Thresholds:
    base = Thresholds()
    return Thresholds(
        min_season_pa=getattr(args, "min_season_pa", base.min_season_pa),
        min_season_outs=base.min_season_outs,
        min_games_vs_opponent=getattr(args, "min_games", base.min_games_vs_opponent),
        min_games_vs_opponent_pitching=base.min_games_vs_opponent_pitching,
        min_pa_vs_opponent=base.min_pa_vs_opponent,
        min_outs_vs_opponent=base.min_outs_vs_opponent,
        min_pa_for_best_game=base.min_pa_for_best_game,
        min_outs_for_best_game=base.min_outs_for_best_game,
    )


def _progress(done: int, total: int) -> None:
    print(f"\r  {done}/{total} games", end="", flush=True)
    if done == total:
        print()


# ------------------------------------------------------------------ commands


def cmd_ingest(args) -> int:
    conn = db.connect(args.db)
    client = StatsApiClient(min_interval=args.sleep)
    print(f"ingesting {args.season} into {args.db} …")
    result = ingest.ingest_season(
        conn,
        client,
        args.season,
        game_types=tuple(args.game_types.split(",")),
        refresh=args.refresh,
        workers=args.workers,
        limit=args.limit,
        progress=_progress,
    )
    print(
        f"{result.games_ingested} new games "
        f"({result.games_skipped} already stored of {result.games_scheduled} final), "
        f"{result.batting_lines} batting lines, {result.pitching_lines} pitching lines"
    )
    for error in result.errors[:5]:
        print(f"  ! {error}", file=sys.stderr)
    if len(result.errors) > 5:
        print(f"  ! … and {len(result.errors) - 5} more", file=sys.stderr)
    conn.close()
    return 1 if result.errors and not result.games_ingested else 0


def cmd_demo(args) -> int:
    conn = db.connect(args.db)
    print(f"generating a fictional {args.season} season into {args.db} …")
    summary = demo.generate(conn, args.season, days=args.days, seed=args.seed,
                            progress=_progress)
    db.set_meta(conn, "demo", "1")
    db.set_meta(conn, "latest_season", str(args.season))
    conn.commit()
    print(
        f"{summary['games']} games, {summary['batting_lines']} batting lines, "
        f"{summary['pitching_lines']} pitching lines"
    )
    print("planted nemesis pairings:")
    for entry in summary["planted"]:
        print(f"  {entry['player']:<24} {entry['side']:<9} vs team {entry['opp_team_id']}")
    conn.close()
    return 0


def cmd_report(args) -> int:
    conn = db.connect(args.db)
    data = analytics.load_season(conn, args.season)
    conn.close()

    if not data.batting and not data.pitching:
        print("no data — run `ingest` or `demo` first", file=sys.stderr)
        return 1

    thresholds = _thresholds(args)

    if args.team:
        team_id = _resolve_team(data, args.team)
        if team_id is None:
            print(f"no team matching {args.team!r}", file=sys.stderr)
            return 1
        report = analytics.team_report(
            data, team_id, args.metric, args.top, thresholds, args.pitchers
        )
        print(f"\n{report['team']} — {data.season}  ({report['role_label'].lower()})")
        print(f"  season-best games allowed: {report['best_game_counts']['batters']} batters, "
              f"{report['best_game_counts']['pitchers']} pitchers")
        for label, rows in (("BATTERS", report["batters"]), ("PITCHERS", report["pitchers"])):
            print(f"\n  {label} (by {report['metric_label'].lower()})")
            _print_rows(rows)
    else:
        print(f"\nLeague leaderboard — {data.season}")
        for side in ("batting", "pitching"):
            print(f"\n  {side.upper()}")
            _print_rows(analytics.leaderboard(
                data, side, args.metric, args.top, thresholds, args.pitchers
            ))

        print("\n  SEASON-BEST GAMES ALLOWED, BY TEAM")
        print(f"  {'Team':<26}{'Bat':>5}{'Pit':>5}{'Tot':>6}")
        for row in analytics.best_game_counts(data, thresholds, args.pitchers)[: args.top]:
            print(f"  {row['team']:<26}{row['batters']:>5}{row['pitchers']:>5}{row['total']:>6}")
    print()
    return 0


def _print_rows(rows) -> None:
    if not rows:
        print("    (nobody clears the thresholds)")
        return
    print(f"    {'#':>2}  {'Player':<22}{'Opponent':<24}{'G':>3}{'Δ':>9}{'/g vs':>8}{'base':>8}   best game")
    for row in rows:
        print(
            f"    {row['rank']:>2}  {row['player'][:21]:<22}{row['opponent'][:23]:<24}"
            f"{row['games_vs']:>3}{row['metric_value']:>9.2f}{row['per_game_vs']:>8.2f}"
            f"{row['baseline_per_game']:>8.2f}   {row['best_game']['summary']}"
        )


def _resolve_team(data, needle: str):
    needle = needle.strip().lower()
    if needle.isdigit():
        # Only accept an id that is actually in the data, so a typo reports
        # "no team matching 1470" instead of an empty report.
        return int(needle) if int(needle) in data.teams else None
    for team_id, team in data.teams.items():
        if needle in (str(team.get("abbrev", "")).lower(), team["name"].lower()):
            return team_id
    for team_id, team in data.teams.items():
        if needle in team["name"].lower():
            return team_id
    return None


def cmd_serve(args) -> int:
    server.serve(Path(args.db), args.host, args.port, _thresholds(args))
    return 0


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlbdelta",
        description="Who had the best game of their season against a given MLB team?",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="pull a season from the MLB Stats API")
    p_ingest.add_argument("--season", type=int, default=current_season())
    p_ingest.add_argument("--game-types", default=",".join(DEFAULT_GAME_TYPES),
                          help="comma separated MLB game type codes (R,F,D,L,W)")
    p_ingest.add_argument("--refresh", action="store_true",
                          help="re-read games already in the database")
    p_ingest.add_argument("--workers", type=int, default=4)
    p_ingest.add_argument("--sleep", type=float, default=0.10,
                          help="minimum seconds between API requests")
    p_ingest.add_argument("--limit", type=int, default=None,
                          help="stop after this many new games (for a quick trial)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_demo = sub.add_parser("demo", help="generate a fictional season for offline use")
    p_demo.add_argument("--season", type=int, default=current_season())
    p_demo.add_argument("--days", type=int, default=162)
    p_demo.add_argument("--seed", type=int, default=20260810)
    p_demo.set_defaults(func=cmd_demo)

    p_report = sub.add_parser("report", help="print deltas and leaderboards")
    p_report.add_argument("--season", type=int, default=None)
    p_report.add_argument("--team", default=None, help="team name, abbreviation or id")
    p_report.add_argument("--metric", default="total",
                          choices=["total", "per_game", "rate"])
    p_report.add_argument("--top", type=int, default=10)
    p_report.add_argument("--min-games", type=int, default=Thresholds().min_games_vs_opponent)
    p_report.add_argument("--min-season-pa", type=int, default=Thresholds().min_season_pa)
    p_report.add_argument("--pitchers", default=DEFAULT_ROLE,
                          choices=["starters", "all", "relievers"],
                          help="which pitching appearances to count (default: starters)")
    p_report.set_defaults(func=cmd_report)

    p_serve = sub.add_parser("serve", help="run the dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--min-games", type=int, default=Thresholds().min_games_vs_opponent)
    p_serve.add_argument("--min-season-pa", type=int, default=Thresholds().min_season_pa)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)

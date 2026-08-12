"""Turn scored game logs into the numbers the dashboard shows.

The central quantity is a **delta**: how a player performed against one
particular opponent, compared with how that same player performed against
*everybody else* in the same season.

    delta = (average score vs opponent) - (average score vs the rest of the league)

Using the rest of the league as the baseline — rather than the player's full
season, which contains the games in question — keeps the comparison honest. A
hitter who faces Baltimore nineteen times would otherwise be measured partly
against himself.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping, Sequence

from .config import Thresholds
from .scoring import (
    LeagueContext,
    league_context,
    score_batting_lines,
    score_pitching_lines,
)

Side = Literal["batting", "pitching"]
Metric = Literal["total", "per_game", "rate"]
PitcherRole = Literal["starters", "all", "relievers"]

METRICS: dict[Metric, str] = {
    "total": "Total runs above own norm",
    "per_game": "Runs above own norm per game",
    "rate": "Rate delta (per PA / per 9 IP)",
}

ROLES: dict[PitcherRole, str] = {
    "starters": "Starting pitchers only",
    "all": "All pitchers",
    "relievers": "Relievers only",
}

# Relief appearances are short and wildly variable, so mixing them with starts
# lets a handful of clean innings outrank a season of real work — and worse,
# they land in the baseline too, comparing a start against a club with relief
# outings against everyone else. Starts-versus-starts is the honest comparison.
DEFAULT_ROLE: PitcherRole = "starters"


@dataclass
class SeasonData:
    """Everything one season needs, held in memory (a season is a few MB)."""

    season: int
    context: LeagueContext
    teams: dict[int, dict]
    players: dict[int, str]
    batting: list[dict] = field(default_factory=list)
    pitching: list[dict] = field(default_factory=list)
    _by_role: dict[PitcherRole, list[dict]] = field(default_factory=dict, repr=False)

    def lines(self, side: Side, role: PitcherRole = "all") -> list[dict]:
        """Game lines for one side, optionally narrowed to a pitching role."""
        if side == "batting":
            return self.batting
        if role == "all":
            return self.pitching
        if role not in self._by_role:
            want_start = role == "starters"
            self._by_role[role] = [
                line for line in self.pitching if bool(line["is_start"]) is want_start
            ]
        return self._by_role[role]

    def team_name(self, team_id: int) -> str:
        team = self.teams.get(team_id)
        return team["name"] if team else f"Team {team_id}"

    def team_abbrev(self, team_id: int) -> str:
        team = self.teams.get(team_id)
        return (team.get("abbrev") if team else None) or self.team_name(team_id)[:3].upper()

    def player_name(self, player_id: int) -> str:
        return self.players.get(player_id, f"Player {player_id}")


def load_season(conn: sqlite3.Connection, season: int | None = None) -> SeasonData:
    """Read a season out of SQLite and score every game line."""
    if season is None:
        row = conn.execute("SELECT MAX(season) AS s FROM games").fetchone()
        season = row["s"] if row and row["s"] else 0

    batting_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM batting_lines WHERE season = ?", (season,)
        )
    ]
    pitching_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM pitching_lines WHERE season = ?", (season,)
        )
    ]
    context = league_context(season, batting_rows, pitching_rows)

    teams = {
        r["team_id"]: dict(r) for r in conn.execute("SELECT * FROM teams")
    }
    players = {
        r["player_id"]: r["name"] for r in conn.execute("SELECT * FROM players")
    }

    return SeasonData(
        season=season,
        context=context,
        teams=teams,
        players=players,
        batting=score_batting_lines(batting_rows, context),
        pitching=score_pitching_lines(pitching_rows, context),
    )


# ------------------------------------------------------------------ helpers


def _group_by_player(lines: Iterable[Mapping]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for line in lines:
        grouped[line["player_id"]].append(dict(line))
    return grouped


def _totals(lines: Sequence[Mapping]) -> tuple[int, float, float]:
    return (
        len(lines),
        sum(line["score"] for line in lines),
        sum(line["workload"] for line in lines),
    )


def _season_qualified(side: Side, workload_units: float, thresholds: Thresholds) -> bool:
    """``workload_units`` is plate appearances for batters, nine-inning units for pitchers."""
    if side == "batting":
        return workload_units >= thresholds.min_season_pa
    return workload_units * 27 >= thresholds.min_season_outs


def _opponent_qualified(side: Side, games: int, workload_units: float, thresholds: Thresholds) -> bool:
    if side == "batting":
        return (
            games >= thresholds.min_games_vs_opponent
            and workload_units >= thresholds.min_pa_vs_opponent
        )
    return (
        games >= thresholds.min_games_vs_opponent_pitching
        and workload_units * 27 >= thresholds.min_outs_vs_opponent
    )


def _best_game_eligible(side: Side, line: Mapping, thresholds: Thresholds) -> bool:
    if side == "batting":
        return line["pa"] >= thresholds.min_pa_for_best_game
    return line["outs"] >= thresholds.min_outs_for_best_game


def _game_view(data: SeasonData, side: Side, line: Mapping) -> dict:
    view = {
        "game_pk": line["game_pk"],
        "date": line["date"],
        "opp_team_id": line["opp_team_id"],
        "opponent": data.team_name(line["opp_team_id"]),
        "opponent_abbrev": data.team_abbrev(line["opp_team_id"]),
        "team_id": line["team_id"],
        "team": data.team_name(line["team_id"]),
        "is_home": bool(line["is_home"]),
        "score": round(line["score"], 3),
        "summary": line["summary"],
    }
    if side == "pitching":
        view["game_score"] = round(line["game_score"], 1)
        view["is_start"] = bool(line["is_start"])
    return view


def _rate(score: float, workload: float) -> float:
    return score / workload if workload else 0.0


def _best_line(lines: Iterable[Mapping]) -> dict | None:
    """The highest-scoring line, with ties settled the same way everywhere.

    Ties are common — every clean one-inning relief appearance scores exactly
    the same — so the tie-break has to be deterministic or a player ends up
    with several "best games of the season". The earlier game wins, and the
    date decides it rather than the game id: MLB assigns a game_pk when the
    game is *scheduled*, so a rained-out game replayed in September still
    carries a low id.
    """
    return min(
        lines,
        key=lambda l: (-l["score"], l["date"], l["game_pk"]),
        default=None,
    )


def _latest_line(lines: Sequence[Mapping]) -> Mapping:
    """The most recent line — which club a player belongs to, after a trade."""
    return max(lines, key=lambda l: (l["date"], l["game_pk"]))


# ------------------------------------------------------------------- deltas


def player_opponent_deltas(
    data: SeasonData,
    side: Side,
    opp_team_id: int | None = None,
    thresholds: Thresholds = Thresholds(),
    role: PitcherRole = DEFAULT_ROLE,
) -> list[dict]:
    """One row per (player, opponent) pair that clears the sample thresholds.

    ``role`` narrows the pitching side. It filters the lines *before* anything
    is aggregated, so both the performance against the club and the baseline it
    is measured against are built from the same kind of appearance.
    """
    rows: list[dict] = []

    for player_id, lines in _group_by_player(data.lines(side, role)).items():
        season_games, season_score, season_workload = _totals(lines)
        if not _season_qualified(side, season_workload, thresholds):
            continue

        by_opponent: dict[int, list[dict]] = defaultdict(list)
        for line in lines:
            by_opponent[line["opp_team_id"]].append(line)

        eligible = [l for l in lines if _best_game_eligible(side, l, thresholds)]
        best = _best_line(eligible)

        opponents = [opp_team_id] if opp_team_id is not None else list(by_opponent)
        for opponent in opponents:
            vs_lines = by_opponent.get(opponent)
            if not vs_lines:
                continue
            games_vs, score_vs, workload_vs = _totals(vs_lines)
            if not _opponent_qualified(side, games_vs, workload_vs, thresholds):
                continue

            base_games = season_games - games_vs
            base_score = season_score - score_vs
            base_workload = season_workload - workload_vs
            if base_games <= 0 or base_workload <= 0:
                # Only ever faced this one opponent: no honest baseline exists.
                continue

            per_game_vs = score_vs / games_vs
            per_game_base = base_score / base_games
            rate_vs = _rate(score_vs, workload_vs)
            rate_base = _rate(base_score, base_workload)

            # Judge the best game against this club by the same eligibility
            # rule used for the season best, so the comparison below is fair.
            best_vs = _best_line(
                [l for l in vs_lines if _best_game_eligible(side, l, thresholds)]
            ) or _best_line(vs_lines)
            season_best_here = bool(best and best["game_pk"] == best_vs["game_pk"])
            home_club = _latest_line(vs_lines)["team_id"]

            rows.append(
                {
                    "side": side,
                    "player_id": player_id,
                    "player": data.player_name(player_id),
                    "team_id": home_club,
                    "team": data.team_name(home_club),
                    "team_abbrev": data.team_abbrev(home_club),
                    "opp_team_id": opponent,
                    "opponent": data.team_name(opponent),
                    "opponent_abbrev": data.team_abbrev(opponent),
                    "games_vs": games_vs,
                    "workload_vs": round(workload_vs, 2),
                    "score_vs": round(score_vs, 3),
                    "per_game_vs": round(per_game_vs, 3),
                    "rate_vs": round(rate_vs, 4),
                    "baseline_games": base_games,
                    "baseline_per_game": round(per_game_base, 3),
                    "baseline_rate": round(rate_base, 4),
                    # The three ways of asking "how much better than usual?"
                    "delta_total": round(score_vs - per_game_base * games_vs, 3),
                    "delta_per_game": round(per_game_vs - per_game_base, 3),
                    "delta_rate": round(rate_vs - rate_base, 4),
                    "season_best_vs_them": season_best_here,
                    "best_game": _game_view(data, side, best_vs),
                }
            )

    return rows


_METRIC_KEY: dict[Metric, str] = {
    "total": "delta_total",
    "per_game": "delta_per_game",
    "rate": "delta_rate",
}


def rank(rows: Sequence[dict], metric: Metric = "total", limit: int = 10,
         ascending: bool = False) -> list[dict]:
    key = _METRIC_KEY[metric]
    ordered = sorted(rows, key=lambda r: r[key], reverse=not ascending)
    trimmed = [dict(row) for row in ordered[:limit]]
    for position, row in enumerate(trimmed, start=1):
        row["rank"] = position
        row["metric"] = key
        row["metric_value"] = row[key]
    return trimmed


# -------------------------------------------------------- best-game counts


def best_game_attributions(
    data: SeasonData,
    side: Side,
    thresholds: Thresholds = Thresholds(),
    role: PitcherRole = DEFAULT_ROLE,
) -> list[dict]:
    """For every qualified player, the opponent they had their best game against.

    Ties are broken by the earlier game, so each player is counted exactly once
    and the per-team counts add up to the number of qualified players.
    """
    attributions: list[dict] = []

    for player_id, lines in _group_by_player(data.lines(side, role)).items():
        _, _, season_workload = _totals(lines)
        if not _season_qualified(side, season_workload, thresholds):
            continue
        eligible = [l for l in lines if _best_game_eligible(side, l, thresholds)]
        if not eligible:
            continue

        best = _best_line(eligible)
        runner_up = sorted((l["score"] for l in eligible), reverse=True)
        margin = best["score"] - (runner_up[1] if len(runner_up) > 1 else 0.0)

        attributions.append(
            {
                "side": side,
                "player_id": player_id,
                "player": data.player_name(player_id),
                "team_id": best["team_id"],
                "team": data.team_name(best["team_id"]),
                "opp_team_id": best["opp_team_id"],
                "opponent": data.team_name(best["opp_team_id"]),
                "opponent_abbrev": data.team_abbrev(best["opp_team_id"]),
                "score": round(best["score"], 3),
                "margin_over_second_best": round(margin, 3),
                "game": _game_view(data, side, best),
            }
        )

    return attributions


def best_game_counts(
    data: SeasonData,
    thresholds: Thresholds = Thresholds(),
    role: PitcherRole = DEFAULT_ROLE,
) -> list[dict]:
    """How many players had their single best game of the season vs each team."""
    counts: dict[int, dict] = {
        team_id: {
            "team_id": team_id,
            "team": data.team_name(team_id),
            "abbrev": data.team_abbrev(team_id),
            "batters": 0,
            "pitchers": 0,
            "total": 0,
        }
        for team_id in _teams_in_play(data)
    }

    for side in ("batting", "pitching"):
        bucket = "batters" if side == "batting" else "pitchers"
        for attribution in best_game_attributions(data, side, thresholds, role):
            entry = counts.setdefault(
                attribution["opp_team_id"],
                {
                    "team_id": attribution["opp_team_id"],
                    "team": attribution["opponent"],
                    "abbrev": attribution["opponent_abbrev"],
                    "batters": 0,
                    "pitchers": 0,
                    "total": 0,
                },
            )
            entry[bucket] += 1
            entry["total"] += 1

    return sorted(counts.values(), key=lambda row: (-row["total"], row["team"]))


def _teams_in_play(data: SeasonData) -> set[int]:
    teams = {line["opp_team_id"] for line in data.batting}
    teams |= {line["opp_team_id"] for line in data.pitching}
    return teams


# -------------------------------------------------------------- assembled


def team_report(
    data: SeasonData,
    opp_team_id: int,
    metric: Metric = "total",
    limit: int = 10,
    thresholds: Thresholds = Thresholds(),
    role: PitcherRole = DEFAULT_ROLE,
) -> dict:
    """Everything the dashboard shows for one selected opponent."""
    batting_rows = player_opponent_deltas(data, "batting", opp_team_id, thresholds)
    pitching_rows = player_opponent_deltas(
        data, "pitching", opp_team_id, thresholds, role
    )

    counts = {"batters": 0, "pitchers": 0}
    best_games = {"batting": [], "pitching": []}
    for side, bucket in (("batting", "batters"), ("pitching", "pitchers")):
        attributions = [
            a
            for a in best_game_attributions(data, side, thresholds, role)
            if a["opp_team_id"] == opp_team_id
        ]
        counts[bucket] = len(attributions)
        best_games[side] = sorted(
            attributions, key=lambda a: a["score"], reverse=True
        )[:limit]

    return {
        "season": data.season,
        "team_id": opp_team_id,
        "team": data.team_name(opp_team_id),
        "metric": metric,
        "metric_label": METRICS[metric],
        "role": role,
        "role_label": ROLES[role],
        "limit": limit,
        "best_game_counts": {**counts, "total": counts["batters"] + counts["pitchers"]},
        "batters": rank(batting_rows, metric, limit),
        "pitchers": rank(pitching_rows, metric, limit),
        "best_games": best_games,
        "qualified": {"batters": len(batting_rows), "pitchers": len(pitching_rows)},
    }


def leaderboard(
    data: SeasonData,
    side: Literal["overall", "batting", "pitching"] = "overall",
    metric: Metric = "total",
    limit: int = 25,
    thresholds: Thresholds = Thresholds(),
    role: PitcherRole = DEFAULT_ROLE,
) -> list[dict]:
    """Biggest player-vs-team deltas anywhere in the league."""
    rows: list[dict] = []
    if side in ("overall", "batting"):
        rows += player_opponent_deltas(data, "batting", None, thresholds)
    if side in ("overall", "pitching"):
        rows += player_opponent_deltas(data, "pitching", None, thresholds, role)
    return rank(rows, metric, limit)


def player_detail(
    data: SeasonData,
    player_id: int,
    side: Side,
    opp_team_id: int | None = None,
    role: PitcherRole = DEFAULT_ROLE,
) -> dict:
    """Game log for one player, optionally highlighting one opponent.

    The log honours ``role`` so its season total reconciles with the delta the
    player was ranked on — a log showing relief outings the ranking ignored
    would not add up.
    """
    lines = [l for l in data.lines(side, role) if l["player_id"] == player_id]
    lines.sort(key=lambda l: (l["date"], l["game_pk"]))
    games = [_game_view(data, side, line) for line in lines]

    # Flag the one game the rest of the app calls their best, not every game
    # that ties the top score — identical relief outings tie constantly.
    best = _best_line(lines)
    for game in games:
        game["is_season_best"] = best is not None and game["game_pk"] == best["game_pk"]
        game["is_vs_selected"] = opp_team_id is not None and game["opp_team_id"] == opp_team_id

    total = sum(l["score"] for l in lines)
    return {
        "player_id": player_id,
        "player": data.player_name(player_id),
        "side": side,
        "role": role if side == "pitching" else "all",
        "season": data.season,
        "games": games,
        "season_score": round(total, 3),
        "season_per_game": round(total / len(lines), 3) if lines else 0.0,
    }

"""Rating a single game, for a batter or a pitcher, against league average.

Both sides are expressed in the same unit — **runs above average (RAA)** — so a
batter's night and a pitcher's night can sit on one leaderboard.

Batting
    Linear weights (wOBA-style event values) give a raw run value for the
    game's events. The league's average run value per plate appearance,
    *measured from the ingested season itself*, is then subtracted::

        RAA = sum(weight_e * count_e) - league_runs_per_pa * PA  (+ SB/CS value)

    A league-average hitter scores 0 in any game, whatever the run environment,
    and the score scales with playing time the way a fan would expect: 4-for-4
    with two homers beats 1-for-1 with one homer.

Pitching
    Runs prevented relative to what a league-average pitcher gives up in the
    same number of outs::

        RAA = league_runs_per_out * outs - runs_allowed

    Seven shutout innings is +3.5-ish runs; two innings and five runs is deeply
    negative. Runs (not earned runs) are used, because a game score should
    describe what happened; ``game_score_v2`` below is offered alongside it as
    the more familiar 0-100-ish dominance scale.

Neither number is park- or opponent-adjusted. That is a deliberate limit: the
whole point of the dashboard is to attribute performance *to the opponent*, so
adjusting it away would be circular.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .config import BattingWeights

# Fallbacks used only when a database has no rows to calibrate against.
FALLBACK_RUNS_PER_PA = 0.121
FALLBACK_RUNS_PER_OUT = 0.166  # ~4.5 runs per nine innings


@dataclass(frozen=True)
class LeagueContext:
    """League-average rates measured from the ingested data."""

    season: int
    runs_per_pa: float = FALLBACK_RUNS_PER_PA
    runs_per_out: float = FALLBACK_RUNS_PER_OUT
    total_pa: int = 0
    total_outs: int = 0
    weights: BattingWeights = BattingWeights()

    @property
    def runs_per_nine(self) -> float:
        return self.runs_per_out * 27

    def as_dict(self) -> dict:
        return {
            "season": self.season,
            "runs_per_pa": round(self.runs_per_pa, 4),
            "runs_per_out": round(self.runs_per_out, 4),
            "runs_per_nine": round(self.runs_per_nine, 3),
            "total_pa": self.total_pa,
            "total_outs": self.total_outs,
        }


# ---------------------------------------------------------------- batting


def batting_event_runs(line: Mapping, weights: BattingWeights = BattingWeights()) -> float:
    """Raw linear-weights run value of a batting line, before re-centring.

    Baserunning is included here rather than added afterwards so that the
    league-wide total of :func:`batting_raa` comes out at exactly zero.
    """
    hits = line["h"]
    doubles = line["b2"]
    triples = line["b3"]
    homers = line["hr"]
    singles = max(0, hits - doubles - triples - homers)
    intentional = line.get("ibb", 0)
    unintentional = max(0, line["bb"] - intentional)
    return (
        weights.single * singles
        + weights.double * doubles
        + weights.triple * triples
        + weights.home_run * homers
        + weights.unintentional_bb * unintentional
        + weights.intentional_bb * intentional
        + weights.hbp * line.get("hbp", 0)
        + weights.stolen_base * line.get("sb", 0)
        + weights.caught_stealing * line.get("cs", 0)
    )


def batting_raa(line: Mapping, context: LeagueContext) -> float:
    """Runs above average for one batter in one game."""
    return batting_event_runs(line, context.weights) - context.runs_per_pa * line["pa"]


def batting_line_summary(line: Mapping) -> str:
    """A short human-readable line: ``3-4, 2B, HR, 3 RBI``."""
    parts = [f"{line['h']}-{line['ab']}"]
    extras = []
    for count, label in ((line["b2"], "2B"), (line["b3"], "3B"), (line["hr"], "HR")):
        if count:
            extras.append(f"{count} {label}" if count > 1 else label)
    walks = line["bb"]
    if walks:
        extras.append(f"{walks} BB" if walks > 1 else "BB")
    if line.get("hbp"):
        extras.append("HBP")
    if line.get("rbi"):
        extras.append(f"{line['rbi']} RBI")
    if line.get("r"):
        extras.append(f"{line['r']} R")
    if line.get("sb"):
        extras.append(f"{line['sb']} SB")
    return ", ".join(parts + extras)


# --------------------------------------------------------------- pitching


def pitching_raa(line: Mapping, context: LeagueContext) -> float:
    """Runs above average (i.e. runs prevented) for one pitcher in one game."""
    return context.runs_per_out * line["outs"] - line["r"]


def game_score_v2(line: Mapping) -> float:
    """Tom Tango's Game Score v2 — a familiar 0-100-ish dominance scale.

    Starts at 40; every out is worth two points, strikeouts add one more,
    and baserunners and runs are charged against the pitcher. As published,
    only unintentional walks count against him.
    """
    unintentional_bb = max(0, line["bb"] - line.get("ibb", 0))
    return (
        40.0
        + 2 * line["outs"]
        + 1 * line["so"]
        - 2 * unintentional_bb
        - 2 * line["h"]
        - 3 * line["r"]
        - 6 * line["hr"]
    )


def innings_text(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def pitching_line_summary(line: Mapping) -> str:
    """``6.2 IP, 3 H, 1 R, 2 BB, 8 K``."""
    return (
        f"{innings_text(line['outs'])} IP, {line['h']} H, {line['r']} R, "
        f"{line['bb']} BB, {line['so']} K"
    )


# -------------------------------------------------------- league context


def league_context(
    season: int,
    batting_lines: Iterable[Mapping],
    pitching_lines: Iterable[Mapping],
    weights: BattingWeights = BattingWeights(),
) -> LeagueContext:
    """Measure the season's run environment from the data that was ingested."""
    total_pa = 0
    total_event_runs = 0.0
    for line in batting_lines:
        total_pa += line["pa"]
        total_event_runs += batting_event_runs(line, weights)

    total_outs = 0
    total_runs = 0
    for line in pitching_lines:
        total_outs += line["outs"]
        total_runs += line["r"]

    return LeagueContext(
        season=season,
        runs_per_pa=(total_event_runs / total_pa) if total_pa else FALLBACK_RUNS_PER_PA,
        runs_per_out=(total_runs / total_outs) if total_outs else FALLBACK_RUNS_PER_OUT,
        total_pa=total_pa,
        total_outs=total_outs,
        weights=weights,
    )


def score_batting_lines(lines: Sequence[Mapping], context: LeagueContext) -> list[dict]:
    scored = []
    for line in lines:
        row = dict(line)
        row["score"] = batting_raa(line, context)
        row["workload"] = float(line["pa"])
        row["summary"] = batting_line_summary(line)
        scored.append(row)
    return scored


def score_pitching_lines(lines: Sequence[Mapping], context: LeagueContext) -> list[dict]:
    scored = []
    for line in lines:
        row = dict(line)
        row["score"] = pitching_raa(line, context)
        row["workload"] = line["outs"] / 27.0  # nine-inning units
        row["game_score"] = game_score_v2(line)
        row["summary"] = pitching_line_summary(line)
        scored.append(row)
    return scored

"""Project-wide configuration and tunable constants."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

DATA_DIR = Path(os.environ.get("MLBDELTA_DATA_DIR", PROJECT_DIR / "data"))
DEFAULT_DB_PATH = DATA_DIR / "mlb.sqlite3"
CACHE_DIR = DATA_DIR / "cache"

STATSAPI_BASE = os.environ.get("MLBDELTA_API_BASE", "https://statsapi.mlb.com/api/v1")

# MLB Stats API sportId 1 == Major League Baseball.
SPORT_ID = 1

# Game type codes: R regular season, F wild card, D division series,
# L league championship, W world series, S spring training, E exhibition.
DEFAULT_GAME_TYPES = ("R",)


@dataclass(frozen=True)
class BattingWeights:
    """Linear weights for batting events, on the "runs above an out" scale.

    These are wOBA-style event values. Their absolute level does not matter for
    ranking, because every score is re-centred against the league average that
    is measured from the ingested season itself (see ``scoring.LeagueContext``).
    What matters is their *relative* size, which is stable across run
    environments.
    """

    unintentional_bb: float = 0.69
    intentional_bb: float = 0.00  # excluded from wOBA; kept in the PA denominator
    hbp: float = 0.72
    single: float = 0.89
    double: float = 1.27
    triple: float = 1.62
    home_run: float = 2.10
    # Baserunning is already in runs, so it is added after re-centring.
    stolen_base: float = 0.20
    caught_stealing: float = -0.41


@dataclass(frozen=True)
class Thresholds:
    """Minimum sample sizes so that noise does not top the leaderboards."""

    # A player must have this much season-long work to be ranked at all.
    min_season_pa: int = 50
    min_season_outs: int = 30  # 10 innings

    # A player must have faced the selected opponent at least this often.
    # Batters see a division rival a dozen-plus times; a starting pitcher may
    # face a given club only twice all year, so the two sides need different
    # bars — a shared one either excludes every starter or admits noise.
    min_games_vs_opponent: int = 3  # batters
    min_games_vs_opponent_pitching: int = 2
    min_pa_vs_opponent: int = 8
    min_outs_vs_opponent: int = 15  # five innings

    # Games below this are not counted as anybody's "best game of the season".
    min_pa_for_best_game: int = 1
    min_outs_for_best_game: int = 3


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    cache_dir: Path = CACHE_DIR
    batting_weights: BattingWeights = field(default_factory=BattingWeights)
    thresholds: Thresholds = field(default_factory=Thresholds)


DEFAULT_SETTINGS = Settings()


def current_season(today=None) -> int:
    """Best guess at the season to ingest when the user does not name one.

    Before March the interesting season is still the one that just finished.
    """
    import datetime

    today = today or datetime.date.today()
    return today.year if today.month >= 3 else today.year - 1

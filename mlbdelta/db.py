"""SQLite storage for player game logs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DEFAULT_DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    abbrev      TEXT,
    short_name  TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    game_pk      INTEGER PRIMARY KEY,
    season       INTEGER NOT NULL,
    date         TEXT NOT NULL,
    game_type    TEXT NOT NULL,
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batting_lines (
    game_pk     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    date        TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    opp_team_id INTEGER NOT NULL,
    is_home     INTEGER NOT NULL,
    pa  INTEGER NOT NULL DEFAULT 0,
    ab  INTEGER NOT NULL DEFAULT 0,
    h   INTEGER NOT NULL DEFAULT 0,
    b2  INTEGER NOT NULL DEFAULT 0,
    b3  INTEGER NOT NULL DEFAULT 0,
    hr  INTEGER NOT NULL DEFAULT 0,
    bb  INTEGER NOT NULL DEFAULT 0,
    ibb INTEGER NOT NULL DEFAULT 0,
    hbp INTEGER NOT NULL DEFAULT 0,
    so  INTEGER NOT NULL DEFAULT 0,
    sf  INTEGER NOT NULL DEFAULT 0,
    sh  INTEGER NOT NULL DEFAULT 0,
    sb  INTEGER NOT NULL DEFAULT 0,
    cs  INTEGER NOT NULL DEFAULT 0,
    rbi INTEGER NOT NULL DEFAULT 0,
    r   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_pk, player_id)
);

CREATE TABLE IF NOT EXISTS pitching_lines (
    game_pk     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    date        TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    opp_team_id INTEGER NOT NULL,
    is_home     INTEGER NOT NULL,
    is_start    INTEGER NOT NULL DEFAULT 0,
    outs INTEGER NOT NULL DEFAULT 0,
    bf   INTEGER NOT NULL DEFAULT 0,
    h    INTEGER NOT NULL DEFAULT 0,
    r    INTEGER NOT NULL DEFAULT 0,
    er   INTEGER NOT NULL DEFAULT 0,
    bb   INTEGER NOT NULL DEFAULT 0,
    ibb  INTEGER NOT NULL DEFAULT 0,
    hbp  INTEGER NOT NULL DEFAULT 0,
    so   INTEGER NOT NULL DEFAULT 0,
    hr   INTEGER NOT NULL DEFAULT 0,
    pitches INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_pk, player_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_bat_season_opp ON batting_lines (season, opp_team_id);
CREATE INDEX IF NOT EXISTS idx_bat_player     ON batting_lines (season, player_id);
CREATE INDEX IF NOT EXISTS idx_pit_season_opp ON pitching_lines (season, opp_team_id);
CREATE INDEX IF NOT EXISTS idx_pit_player     ON pitching_lines (season, player_id);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database and make sure the schema is there."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def ingested_game_pks(conn: sqlite3.Connection, season: int) -> set[int]:
    rows = conn.execute("SELECT game_pk FROM games WHERE season = ?", (season,))
    return {row["game_pk"] for row in rows}


def seasons(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT season FROM games ORDER BY season DESC")
    return [row["season"] for row in rows]

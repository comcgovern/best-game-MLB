"""Pull a season of box scores from the MLB Stats API into SQLite."""

from __future__ import annotations

import datetime
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Sequence

from . import db
from .config import DEFAULT_GAME_TYPES
from .statsapi import StatsApiClient, StatsApiError

log = logging.getLogger(__name__)

# Only completed games have a meaningful box score.
FINAL_STATES = {"Final", "Completed Early", "Game Over"}

# A postponed or cancelled game is sometimes reported with an abstract state of
# "Final" even though it was never played, and its box score is empty.
UNPLAYED_STATES = {"Postponed", "Cancelled", "Canceled", "Suspended", "Forfeit"}


@dataclass
class IngestResult:
    season: int
    games_scheduled: int
    games_ingested: int
    games_skipped: int
    batting_lines: int
    pitching_lines: int
    errors: list[str]


# --------------------------------------------------------------------- parsing


def _int(stats: dict, key: str, default: int = 0) -> int:
    value = stats.get(key, default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def outs_from_innings(innings_pitched: str | float | None) -> int:
    """``"6.1"`` (six and a third) -> 19 outs."""
    if innings_pitched in (None, ""):
        return 0
    text = str(innings_pitched)
    whole, _, part = text.partition(".")
    try:
        outs = int(float(whole or 0)) * 3
    except ValueError:
        return 0
    if part:
        # The API uses .0/.1/.2 for thirds of an inning.
        outs += min(int(part[0]) if part[0].isdigit() else 0, 2)
    return outs


def parse_boxscore(boxscore: dict, game: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Split one box score into batting lines, pitching lines and player rows."""
    batting: list[dict] = []
    pitching: list[dict] = []
    people: dict[int, str] = {}

    teams = boxscore.get("teams", {})
    for side in ("home", "away"):
        side_data = teams.get(side) or {}
        team_id = (side_data.get("team") or {}).get("id")
        opp_data = teams.get("away" if side == "home" else "home") or {}
        opp_team_id = (opp_data.get("team") or {}).get("id")
        if not team_id or not opp_team_id:
            continue

        common = {
            "game_pk": game["game_pk"],
            "season": game["season"],
            "date": game["date"],
            "team_id": team_id,
            "opp_team_id": opp_team_id,
            "is_home": 1 if side == "home" else 0,
        }

        # `pitchers` lists the staff in the order they appeared, so its first
        # entry is the starter. Kept as a fallback: everything downstream can
        # filter to starters, and if gamesStarted ever goes missing from the
        # payload the whole pitching side would silently read as relief work.
        appearance_order = side_data.get("pitchers") or []
        first_pitcher = appearance_order[0] if appearance_order else None

        for player in (side_data.get("players") or {}).values():
            person = player.get("person") or {}
            player_id = person.get("id")
            if not player_id:
                continue
            people[player_id] = person.get("fullName") or f"Player {player_id}"
            stats = player.get("stats") or {}

            bat = stats.get("batting") or {}
            pa = _int(bat, "plateAppearances")
            if pa > 0:
                hits = _int(bat, "hits")
                batting.append(
                    {
                        **common,
                        "player_id": player_id,
                        "pa": pa,
                        "ab": _int(bat, "atBats"),
                        "h": hits,
                        "b2": _int(bat, "doubles"),
                        "b3": _int(bat, "triples"),
                        "hr": _int(bat, "homeRuns"),
                        "bb": _int(bat, "baseOnBalls"),
                        "ibb": _int(bat, "intentionalWalks"),
                        "hbp": _int(bat, "hitByPitch"),
                        "so": _int(bat, "strikeOuts"),
                        "sf": _int(bat, "sacFlies"),
                        "sh": _int(bat, "sacBunts"),
                        "sb": _int(bat, "stolenBases"),
                        "cs": _int(bat, "caughtStealing"),
                        "rbi": _int(bat, "rbi"),
                        "r": _int(bat, "runs"),
                    }
                )

            pit = stats.get("pitching") or {}
            outs = _int(pit, "outs") or outs_from_innings(pit.get("inningsPitched"))
            batters_faced = _int(pit, "battersFaced")
            if outs > 0 or batters_faced > 0:
                pitching.append(
                    {
                        **common,
                        "player_id": player_id,
                        "is_start": 1
                        if (_int(pit, "gamesStarted") or player_id == first_pitcher)
                        else 0,
                        "outs": outs,
                        "bf": batters_faced,
                        "h": _int(pit, "hits"),
                        "r": _int(pit, "runs"),
                        "er": _int(pit, "earnedRuns"),
                        "bb": _int(pit, "baseOnBalls"),
                        "ibb": _int(pit, "intentionalWalks"),
                        "hbp": _int(pit, "hitByPitch"),
                        "so": _int(pit, "strikeOuts"),
                        "hr": _int(pit, "homeRuns"),
                        "pitches": _int(pit, "numberOfPitches")
                        or _int(pit, "pitchesThrown"),
                    }
                )

    player_rows = [{"player_id": pid, "name": name} for pid, name in people.items()]
    return batting, pitching, player_rows


def parse_schedule_game(raw: dict, season: int) -> dict | None:
    """Normalise one schedule entry, or ``None`` if it is not a finished game."""
    status = raw.get("status") or {}
    detailed = status.get("detailedState")
    if detailed in UNPLAYED_STATES:
        return None
    if status.get("abstractGameState") != "Final" and detailed not in FINAL_STATES:
        return None
    teams = raw.get("teams") or {}
    home = ((teams.get("home") or {}).get("team") or {}).get("id")
    away = ((teams.get("away") or {}).get("team") or {}).get("id")
    game_pk = raw.get("gamePk")
    if not (home and away and game_pk):
        return None
    return {
        "game_pk": game_pk,
        "season": int(raw.get("season") or season),
        "date": raw.get("officialDate") or (raw.get("gameDate") or "")[:10],
        "game_type": raw.get("gameType") or "R",
        "home_team_id": home,
        "away_team_id": away,
    }


# ------------------------------------------------------------------- writing


def _insert_many(conn: sqlite3.Connection, table: str, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    conn.executemany(sql, [tuple(row[c] for c in columns) for row in rows])
    return len(rows)


def store_teams(conn: sqlite3.Connection, teams: Iterable[dict]) -> None:
    rows = [
        {
            "team_id": team["id"],
            "name": team.get("name") or "",
            "abbrev": team.get("abbreviation") or "",
            "short_name": team.get("teamName") or team.get("shortName") or "",
        }
        for team in teams
        if team.get("id")
    ]
    _insert_many(conn, "teams", rows)


# ------------------------------------------------------------------ driving


def month_windows(season: int, today: datetime.date | None = None) -> Iterator[tuple[str, str]]:
    """Chunk a season into month-sized schedule queries (Feb through Nov)."""
    today = today or datetime.date.today()
    for month in range(2, 12):
        start = datetime.date(season, month, 1)
        end = datetime.date(season, month + 1, 1) - datetime.timedelta(days=1)
        if start > today:
            return
        yield start.isoformat(), min(end, today).isoformat()


def ingest_season(
    conn: sqlite3.Connection,
    client: StatsApiClient,
    season: int,
    game_types: Sequence[str] = DEFAULT_GAME_TYPES,
    refresh: bool = False,
    workers: int = 4,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    today: datetime.date | None = None,
) -> IngestResult:
    """Fetch every finished game of ``season`` and store the player game logs."""
    store_teams(conn, client.teams(season))
    conn.commit()

    scheduled: dict[int, dict] = {}
    for start, end in month_windows(season, today=today):
        for raw in client.schedule(season, start, end, game_types):
            game = parse_schedule_game(raw, season)
            if game:
                scheduled[game["game_pk"]] = game

    known = set() if refresh else db.ingested_game_pks(conn, season)
    outstanding = [game for pk, game in sorted(scheduled.items()) if pk not in known]
    todo = outstanding[:limit] if limit is not None else outstanding

    result = IngestResult(
        season=season,
        games_scheduled=len(scheduled),
        games_ingested=0,
        games_skipped=len(scheduled) - len(outstanding),
        batting_lines=0,
        pitching_lines=0,
        errors=[],
    )

    if not todo:
        db.set_meta(conn, f"ingested_at:{season}", datetime.datetime.now().isoformat(timespec="seconds"))
        conn.commit()
        return result

    def fetch(game: dict):
        return game, client.boxscore(game["game_pk"])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch, game) for game in todo]
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                game, boxscore = future.result()
            except StatsApiError as exc:
                result.errors.append(str(exc))
                continue

            batting, pitching, players = parse_boxscore(boxscore, game)
            if not batting and not pitching:
                # Recording the game here would mark it done for good, because
                # the resume check reads the games table. Leave it out so the
                # next run retries it.
                client.forget_boxscore(game["game_pk"])
                result.errors.append(
                    f"game {game['game_pk']} ({game['date']}) has an empty box score"
                )
                continue

            _insert_many(conn, "players", players)
            _insert_many(conn, "games", [game])
            result.batting_lines += _insert_many(conn, "batting_lines", batting)
            result.pitching_lines += _insert_many(conn, "pitching_lines", pitching)
            result.games_ingested += 1

            if done % 50 == 0:
                conn.commit()
            if progress:
                progress(done, len(todo))

    db.set_meta(conn, f"ingested_at:{season}", datetime.datetime.now().isoformat(timespec="seconds"))
    db.set_meta(conn, "latest_season", str(season))
    conn.commit()
    return result

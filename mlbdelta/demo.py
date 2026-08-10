"""A seeded, fictional season, so the pipeline can be exercised offline.

Everything here is invented: the teams and players do not exist and the numbers
are not real baseball results. The point is that the *shape* of the data is
exact — the generator emits box scores in the MLB Stats API's own JSON layout
and feeds them through :func:`mlbdelta.ingest.parse_boxscore`, the same parser
the live ingest uses.

A handful of "nemesis" pairings are planted deliberately (a player who hits or
pitches far better against one particular club) so the dashboard has something
to find, and so the tests can assert that it finds them.
"""

from __future__ import annotations

import datetime
import random
import sqlite3
from dataclasses import dataclass, field

from .ingest import _insert_many, parse_boxscore

CITIES = [
    "Akron", "Boise", "Camden", "Dover", "Elgin", "Fargo", "Galway", "Hobart",
    "Ithaca", "Joplin", "Kanata", "Lodi", "Merida", "Norfolk", "Orillia",
    "Provo", "Quincy", "Reno", "Salem", "Tacoma", "Utica", "Vernon", "Waco",
    "Xenia", "Yuma", "Zanesville", "Bristol", "Cortez", "Dayton", "Eureka",
]
NICKNAMES = [
    "Foundry", "Bluecaps", "Ironsides", "Harbor Dogs", "Sparrows", "Threshers",
    "Quarry", "Lamplighters", "Cordwainers", "Bison", "Kestrels", "Drifters",
    "Anvils", "Watermen", "Ramblers", "Millers", "Stonecutters", "Gales",
    "Pioneers", "Loggers", "Mariners of Utica", "Vintners", "Longhorns",
    "Coopers", "Prospectors", "Buckeyes", "Tinkers", "Cardinals of Cortez",
    "Aviators", "Redwoods",
]
FIRST_NAMES = [
    "Amos", "Bo", "Cal", "Dax", "Eli", "Foster", "Gil", "Hank", "Ike", "Jules",
    "Kip", "Lonnie", "Milo", "Nash", "Otis", "Pike", "Quinn", "Rye", "Sol",
    "Tobin", "Urban", "Vance", "Wes", "Xavier", "Yates", "Zeb",
]
LAST_NAMES = [
    "Ackerly", "Bramble", "Cobbett", "Dunmore", "Ellery", "Fairbrass",
    "Gallows", "Hollins", "Inchcape", "Jessup", "Kirkby", "Lachlan",
    "Merriwether", "Northrop", "Ovington", "Pettibone", "Quillen", "Rooker",
    "Standish", "Thackery", "Underhill", "Vosburgh", "Wexler", "Yardley",
]

# Per plate appearance, before talent adjustment.
BASE_RATES = {
    "bb": 0.085,
    "hbp": 0.011,
    "1b": 0.140,
    "2b": 0.045,
    "3b": 0.004,
    "hr": 0.032,
}
STRIKEOUT_SHARE_OF_OUTS = 0.30


@dataclass
class Person:
    player_id: int
    name: str
    talent: float


@dataclass
class Club:
    team_id: int
    name: str
    abbrev: str
    division: int = 0
    batters: list[Person] = field(default_factory=list)
    pitchers: list[Person] = field(default_factory=list)

    @property
    def regulars(self) -> list[Person]:
        return self.batters[:9]

    @property
    def bench(self) -> list[Person]:
        return self.batters[9:]

    @property
    def rotation(self) -> list[Person]:
        return self.pitchers[:5]

    @property
    def bullpen(self) -> list[Person]:
        return self.pitchers[5:]


def build_league(rng: random.Random, team_count: int = 30) -> list[Club]:
    clubs: list[Club] = []
    player_id = 600000
    for index in range(team_count):
        city = CITIES[index % len(CITIES)]
        nickname = NICKNAMES[index % len(NICKNAMES)]
        club = Club(
            team_id=100 + index,
            name=f"{city} {nickname}",
            abbrev=(city[:2] + nickname[:1]).upper(),
            division=index // 5,
        )
        for _ in range(14):
            player_id += 1
            club.batters.append(
                Person(player_id, _person_name(rng), rng.gauss(1.0, 0.16))
            )
        for _ in range(11):
            player_id += 1
            club.pitchers.append(
                Person(player_id, _person_name(rng), rng.gauss(1.0, 0.13))
            )
        clubs.append(club)
    return clubs


def _person_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def plant_nemeses(rng: random.Random, clubs: list[Club], count: int = 12) -> list[dict]:
    """Give a few everyday players an outsized edge against one division rival.

    Division rivals meet often enough that the effect has room to show up,
    which is what makes these useful as a fixture for the analytics tests.
    """
    planted = []
    for _ in range(count):
        club = rng.choice(clubs)
        rivals = [c for c in clubs if c.division == club.division and c is not club]
        victim = rng.choice(rivals)
        side = rng.choice(["batting", "pitching"])
        person = rng.choice(club.regulars if side == "batting" else club.rotation)
        planted.append(
            {
                "player_id": person.player_id,
                "player": person.name,
                "side": side,
                "team_id": club.team_id,
                "opp_team_id": victim.team_id,
                "boost": 1.9 if side == "batting" else 0.55,
            }
        )
    return planted


# --------------------------------------------------------------- simulation


def _draw_event(rng: random.Random, scale: float) -> str:
    rates = {key: value * scale for key, value in BASE_RATES.items()}
    on_base = sum(rates.values())
    if on_base > 0.75:  # keep the distribution sane for extreme talent draws
        rates = {k: v * (0.75 / on_base) for k, v in rates.items()}
        on_base = 0.75
    roll = rng.random()
    cumulative = 0.0
    for key, value in rates.items():
        cumulative += value
        if roll < cumulative:
            return key
    return "so" if rng.random() < STRIKEOUT_SHARE_OF_OUTS else "out"


def _advance(bases: list[int | None], batter_id: int, event: str,
             rng: random.Random) -> list[int]:
    """Move runners for one event.

    ``bases`` holds the player on first, second and third (or ``None``). The
    return value is the list of players who scored, so runs can be credited to
    the right person rather than guessed at.
    """
    first, second, third = bases
    scored: list[int] = []

    if event == "hr":
        scored = [p for p in (third, second, first) if p] + [batter_id]
        first = second = third = None
    elif event == "3b":
        scored = [p for p in (third, second, first) if p]
        first = second = None
        third = batter_id
    elif event == "2b":
        scored = [p for p in (third, second) if p]
        held = None
        if first:
            if rng.random() < 0.45:
                scored.append(first)
            else:
                held = first
        first, second, third = None, batter_id, held
    elif event == "1b":
        scored = [p for p in (third,) if p]
        held = None
        if second:
            if rng.random() < 0.55:
                scored.append(second)
            else:
                held = second
        first, second, third = batter_id, first, held
    else:  # walk or hit by pitch: only forced runners move
        first, second, third, scored = _force_advance(first, second, third, batter_id)

    bases[:] = [first, second, third]
    return scored


def _force_advance(first, second, third, batter_id) -> tuple:
    """Walk/HBP: push only the runners who are forced to move."""
    if first is None:
        return batter_id, second, third, []
    if second is None:
        return batter_id, first, third, []
    if third is None:
        return batter_id, first, second, []
    return batter_id, first, second, [third]


def _blank_batting() -> dict:
    return {
        "plateAppearances": 0, "atBats": 0, "hits": 0, "doubles": 0, "triples": 0,
        "homeRuns": 0, "baseOnBalls": 0, "intentionalWalks": 0, "hitByPitch": 0,
        "strikeOuts": 0, "sacFlies": 0, "sacBunts": 0, "stolenBases": 0,
        "caughtStealing": 0, "rbi": 0, "runs": 0,
    }


def _blank_pitching() -> dict:
    return {
        "gamesStarted": 0, "outs": 0, "battersFaced": 0, "hits": 0, "runs": 0,
        "earnedRuns": 0, "baseOnBalls": 0, "intentionalWalks": 0, "hitByPitch": 0,
        "strikeOuts": 0, "homeRuns": 0, "numberOfPitches": 0,
    }


def _play_half_innings(
    lineup: list[Person],
    staff: list[Person],
    opponent_team_id: int,
    nemesis: dict[tuple[int, int], float],
    rng: random.Random,
    innings: int = 9,
) -> tuple[dict[int, dict], dict[int, dict]]:
    """Simulate one team's nine offensive innings against one pitching staff."""
    batting: dict[int, dict] = {p.player_id: _blank_batting() for p in lineup}
    pitching: dict[int, dict] = {}

    starter, *bullpen = staff
    pitcher_plan = [(starter, rng.randint(12, 21))] + [
        (arm, 3) for arm in bullpen
    ]
    plan_index = 0
    current, outs_remaining_for_pitcher = pitcher_plan[0]
    pitching[current.player_id] = _blank_pitching()
    pitching[current.player_id]["gamesStarted"] = 1

    order = 0
    for _ in range(innings):
        bases: list[int | None] = [None, None, None]
        outs = 0
        while outs < 3:
            batter = lineup[order % len(lineup)]
            order += 1

            batter_edge = nemesis.get((batter.player_id, opponent_team_id), 1.0)
            pitcher_edge = nemesis.get((current.player_id, 0), 1.0)
            scale = batter.talent / max(0.6, current.talent) * batter_edge * pitcher_edge
            event = _draw_event(rng, scale)

            bat = batting[batter.player_id]
            pit = pitching[current.player_id]
            bat["plateAppearances"] += 1
            pit["battersFaced"] += 1
            pit["numberOfPitches"] += rng.randint(3, 6)

            if event in ("out", "so"):
                bat["atBats"] += 1
                outs += 1
                pit["outs"] += 1
                if event == "so":
                    bat["strikeOuts"] += 1
                    pit["strikeOuts"] += 1
            else:
                if event == "bb":
                    bat["baseOnBalls"] += 1
                    pit["baseOnBalls"] += 1
                elif event == "hbp":
                    bat["hitByPitch"] += 1
                    pit["hitByPitch"] += 1
                else:
                    bat["atBats"] += 1
                    bat["hits"] += 1
                    pit["hits"] += 1
                    if event == "2b":
                        bat["doubles"] += 1
                    elif event == "3b":
                        bat["triples"] += 1
                    elif event == "hr":
                        bat["homeRuns"] += 1
                        pit["homeRuns"] += 1

                scored = _advance(bases, batter.player_id, event, rng)
                if scored:
                    bat["rbi"] += len(scored)
                    pit["runs"] += len(scored)
                    pit["earnedRuns"] += len(scored)
                    for runner_id in scored:
                        if runner_id in batting:
                            batting[runner_id]["runs"] += 1

            if bases[0] is not None and bases[1] is None and rng.random() < 0.06:
                runner = batting.get(bases[0])
                if runner is not None:
                    if rng.random() < 0.75:
                        runner["stolenBases"] += 1
                        bases[0], bases[1] = None, bases[0]
                    else:
                        runner["caughtStealing"] += 1
                        bases[0] = None
                        outs += 1
                        pit["outs"] += 1

            # Hand the ball to the next arm at the end of a pitcher's workload —
            # or early, the way a manager pulls someone getting hit around.
            spent = pit["outs"] >= outs_remaining_for_pitcher or pit["runs"] >= 7
            if spent and plan_index + 1 < len(pitcher_plan):
                plan_index += 1
                current, outs_remaining_for_pitcher = pitcher_plan[plan_index]
                pitching.setdefault(current.player_id, _blank_pitching())

    return batting, pitching


def _lineup(club: Club, rng: random.Random) -> list[Person]:
    """The nine regulars, with the occasional day off covered by the bench."""
    lineup: list[Person] = []
    for player in club.regulars:
        if rng.random() < 0.88:
            lineup.append(player)
            continue
        available = [p for p in club.bench if p not in lineup] or [player]
        lineup.append(rng.choice(available))
    return lineup


def _staff(club: Club, turn: int, rng: random.Random) -> list[Person]:
    """A starter working through the rotation, plus enough relief for any night.

    The bullpen has to be deep enough to cover a blow-up; if it runs out, the
    last arm ends up absorbing an implausible number of runs.
    """
    return [club.rotation[turn % len(club.rotation)]] + rng.sample(
        club.bullpen, len(club.bullpen)
    )


def simulate_game(
    game_pk: int,
    season: int,
    date: str,
    home: Club,
    away: Club,
    nemesis: dict[tuple[int, int], float],
    rng: random.Random,
    home_turn: int = 0,
    away_turn: int = 0,
) -> tuple[dict, dict]:
    """Return a (game, boxscore) pair in MLB Stats API shape."""
    home_lineup = _lineup(home, rng)
    away_lineup = _lineup(away, rng)
    home_staff = _staff(home, home_turn, rng)
    away_staff = _staff(away, away_turn, rng)

    away_bat, home_pit = _play_half_innings(
        away_lineup, home_staff, home.team_id, nemesis, rng
    )
    home_bat, away_pit = _play_half_innings(
        home_lineup, away_staff, away.team_id, nemesis, rng
    )

    def side(club: Club, batting: dict, pitching: dict) -> dict:
        people = {p.player_id: p for p in club.batters + club.pitchers}
        players = {}
        for player_id in set(batting) | set(pitching):
            person = people[player_id]
            stats = {}
            if player_id in batting:
                stats["batting"] = batting[player_id]
            if player_id in pitching:
                stats["pitching"] = pitching[player_id]
            players[f"ID{player_id}"] = {
                "person": {"id": player_id, "fullName": person.name},
                "stats": stats,
            }
        return {
            "team": {"id": club.team_id, "name": club.name},
            "players": players,
        }

    boxscore = {
        "teams": {
            "home": side(home, home_bat, home_pit),
            "away": side(away, away_bat, away_pit),
        }
    }
    game = {
        "game_pk": game_pk,
        "season": season,
        "date": date,
        "game_type": "R",
        "home_team_id": home.team_id,
        "away_team_id": away.team_id,
    }
    return game, boxscore


# ------------------------------------------------------------------ schedule


def build_schedule(
    clubs: list[Club],
    rng: random.Random,
    division_meetings: int = 13,
    interdivision_meetings: int = 4,
) -> list[list[tuple[Club, Club]]]:
    """Group every matchup into days on which no club plays twice.

    Division rivals meet often and everybody else a handful of times, which is
    roughly the shape of a real schedule — and it matters here, because the
    number of meetings is exactly what decides whether a delta is meaningful.
    """
    matchups: list[tuple[Club, Club]] = []
    for i, home_club in enumerate(clubs):
        for away_club in clubs[i + 1:]:
            same_division = home_club.division == away_club.division
            meetings = division_meetings if same_division else interdivision_meetings
            for meeting in range(meetings):
                # Alternate the host so home/away splits stay even.
                pair = (home_club, away_club) if meeting % 2 == 0 else (away_club, home_club)
                matchups.append(pair)
    rng.shuffle(matchups)

    days: list[list[tuple[Club, Club]]] = []
    booked: list[set[int]] = []
    for home_club, away_club in matchups:
        for index, taken in enumerate(booked):
            if home_club.team_id not in taken and away_club.team_id not in taken:
                days[index].append((home_club, away_club))
                taken.update({home_club.team_id, away_club.team_id})
                break
        else:
            days.append([(home_club, away_club)])
            booked.append({home_club.team_id, away_club.team_id})
    return days


# ------------------------------------------------------------------- driver


def generate(
    conn: sqlite3.Connection,
    season: int,
    days: int = 162,
    seed: int = 20260810,
    progress=None,
) -> dict:
    """Fill ``conn`` with a fictional season and return a small summary."""
    rng = random.Random(seed)
    clubs = build_league(rng)
    planted = plant_nemeses(rng, clubs)

    # Batting boosts key on (player, opponent team); pitching boosts damp the
    # opposing offence and key on (pitcher, 0).
    nemesis: dict[tuple[int, int], float] = {}
    for entry in planted:
        if entry["side"] == "batting":
            nemesis[(entry["player_id"], entry["opp_team_id"])] = entry["boost"]

    _insert_many(
        conn,
        "teams",
        [
            {"team_id": c.team_id, "name": c.name, "abbrev": c.abbrev,
             "short_name": c.name.split(" ", 1)[-1]}
            for c in clubs
        ],
    )

    pitching_nemesis = {
        (e["player_id"], e["opp_team_id"]): e["boost"]
        for e in planted
        if e["side"] == "pitching"
    }

    schedule = build_schedule(clubs, rng)[:days]
    opening_day = datetime.date(season, 3, 27)
    game_pk = 800000
    batting_rows = pitching_rows = 0
    rotation_turn: dict[int, int] = {club.team_id: 0 for club in clubs}

    for day, matchups in enumerate(schedule):
        date = (opening_day + datetime.timedelta(days=day)).isoformat()
        for home, away in matchups:
            game_pk += 1

            # A planted pitcher only gets the edge when facing their victim.
            active = dict(nemesis)
            for (pid, opp), boost in pitching_nemesis.items():
                if opp in (home.team_id, away.team_id):
                    active[(pid, 0)] = boost

            game, boxscore = simulate_game(
                game_pk, season, date, home, away, active, rng,
                home_turn=rotation_turn[home.team_id],
                away_turn=rotation_turn[away.team_id],
            )
            rotation_turn[home.team_id] += 1
            rotation_turn[away.team_id] += 1

            batting, pitching, players = parse_boxscore(boxscore, game)
            _insert_many(conn, "players", players)
            _insert_many(conn, "games", [game])
            batting_rows += _insert_many(conn, "batting_lines", batting)
            pitching_rows += _insert_many(conn, "pitching_lines", pitching)

        if progress and day % 20 == 0:
            progress(day + 1, len(schedule))
        if day % 10 == 0:
            conn.commit()

    if progress:
        progress(len(schedule), len(schedule))
    conn.commit()
    return {
        "season": season,
        "games": game_pk - 800000,
        "batting_lines": batting_rows,
        "pitching_lines": pitching_rows,
        "planted": planted,
    }

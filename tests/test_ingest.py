"""Parser tests against a box score in the MLB Stats API's real shape."""

import datetime
import unittest

from mlbdelta import db
from mlbdelta.ingest import (
    month_windows,
    outs_from_innings,
    parse_boxscore,
    parse_schedule_game,
    store_teams,
)

GAME = {
    "game_pk": 777001,
    "season": 2026,
    "date": "2026-06-01",
    "game_type": "R",
    "home_team_id": 147,
    "away_team_id": 111,
}

# Trimmed to the fields the parser reads, but with the API's exact nesting,
# key names and string-typed innings.
BOXSCORE = {
    "teams": {
        "home": {
            "team": {"id": 147, "name": "New York Yankees"},
            "players": {
                "ID592450": {
                    "person": {"id": 592450, "fullName": "Slugging Outfielder"},
                    "stats": {
                        "batting": {
                            "plateAppearances": 5, "atBats": 4, "hits": 3,
                            "doubles": 1, "triples": 0, "homeRuns": 2,
                            "baseOnBalls": 1, "intentionalWalks": 1,
                            "hitByPitch": 0, "strikeOuts": 1, "sacFlies": 0,
                            "sacBunts": 0, "stolenBases": 1, "caughtStealing": 0,
                            "rbi": 5, "runs": 3,
                        }
                    },
                },
                "ID543037": {
                    "person": {"id": 543037, "fullName": "Front End Starter"},
                    "stats": {
                        "pitching": {
                            "gamesStarted": 1, "inningsPitched": "6.2",
                            "battersFaced": 25, "hits": 4, "runs": 2,
                            "earnedRuns": 2, "baseOnBalls": 1,
                            "intentionalWalks": 0, "hitByPitch": 1,
                            "strikeOuts": 9, "homeRuns": 1,
                            "numberOfPitches": 98,
                        }
                    },
                },
                "ID000001": {
                    "person": {"id": 1, "fullName": "Did Not Play"},
                    "stats": {"batting": {}, "pitching": {}},
                },
            },
        },
        "away": {
            "team": {"id": 111, "name": "Boston Red Sox"},
            "players": {
                "ID646240": {
                    "person": {"id": 646240, "fullName": "Two Way Player"},
                    "stats": {
                        "batting": {"plateAppearances": 4, "atBats": 4, "hits": 0,
                                    "strikeOuts": 2},
                        "pitching": {"outs": 3, "battersFaced": 4, "hits": 1,
                                     "runs": 0, "earnedRuns": 0, "strikeOuts": 2},
                    },
                }
            },
        },
    }
}


class InningsTests(unittest.TestCase):
    def test_thirds_of_an_inning(self):
        self.assertEqual(outs_from_innings("6.2"), 20)
        self.assertEqual(outs_from_innings("0.1"), 1)
        self.assertEqual(outs_from_innings("7.0"), 21)
        self.assertEqual(outs_from_innings(5), 15)

    def test_missing_or_junk_values(self):
        self.assertEqual(outs_from_innings(None), 0)
        self.assertEqual(outs_from_innings(""), 0)
        self.assertEqual(outs_from_innings("not innings"), 0)


class BoxscoreParsingTests(unittest.TestCase):
    def setUp(self):
        self.batting, self.pitching, self.players = parse_boxscore(BOXSCORE, GAME)

    def test_only_players_who_appeared_produce_lines(self):
        self.assertEqual(len(self.batting), 2)
        self.assertEqual(len(self.pitching), 2)
        self.assertNotIn(1, [line["player_id"] for line in self.batting])

    def test_opponent_is_the_other_club(self):
        batter = next(l for l in self.batting if l["player_id"] == 592450)
        self.assertEqual(batter["team_id"], 147)
        self.assertEqual(batter["opp_team_id"], 111)
        self.assertEqual(batter["is_home"], 1)

        visitor = next(l for l in self.batting if l["player_id"] == 646240)
        self.assertEqual(visitor["opp_team_id"], 147)
        self.assertEqual(visitor["is_home"], 0)

    def test_batting_fields_are_mapped(self):
        batter = next(l for l in self.batting if l["player_id"] == 592450)
        self.assertEqual(batter["pa"], 5)
        self.assertEqual(batter["h"], 3)
        self.assertEqual(batter["b2"], 1)
        self.assertEqual(batter["hr"], 2)
        self.assertEqual(batter["bb"], 1)
        self.assertEqual(batter["ibb"], 1)
        self.assertEqual(batter["sb"], 1)
        self.assertEqual(batter["rbi"], 5)

    def test_innings_pitched_string_becomes_outs(self):
        starter = next(l for l in self.pitching if l["player_id"] == 543037)
        self.assertEqual(starter["outs"], 20)
        self.assertEqual(starter["is_start"], 1)
        self.assertEqual(starter["so"], 9)
        self.assertEqual(starter["pitches"], 98)

    def test_a_two_way_player_gets_both_lines(self):
        self.assertIn(646240, [l["player_id"] for l in self.batting])
        reliever = next(l for l in self.pitching if l["player_id"] == 646240)
        self.assertEqual(reliever["outs"], 3)
        self.assertEqual(reliever["is_start"], 0)

    def test_player_names_are_collected(self):
        names = {p["player_id"]: p["name"] for p in self.players}
        self.assertEqual(names[592450], "Slugging Outfielder")

    def test_rows_round_trip_through_sqlite(self):
        conn = db.connect(":memory:")
        store_teams(conn, [{"id": 147, "name": "New York Yankees",
                            "abbreviation": "NYY", "teamName": "Yankees"}])
        from mlbdelta.ingest import _insert_many

        _insert_many(conn, "games", [GAME])
        _insert_many(conn, "players", self.players)
        _insert_many(conn, "batting_lines", self.batting)
        _insert_many(conn, "pitching_lines", self.pitching)
        conn.commit()

        stored = conn.execute(
            "SELECT * FROM batting_lines WHERE player_id = 592450"
        ).fetchone()
        self.assertEqual(stored["hr"], 2)
        self.assertEqual(db.ingested_game_pks(conn, 2026), {777001})

    def test_reingesting_a_game_does_not_duplicate_rows(self):
        conn = db.connect(":memory:")
        from mlbdelta.ingest import _insert_many

        for _ in range(2):
            _insert_many(conn, "games", [GAME])
            _insert_many(conn, "batting_lines", self.batting)
        count = conn.execute("SELECT COUNT(*) AS n FROM batting_lines").fetchone()["n"]
        self.assertEqual(count, 2)


class ScheduleParsingTests(unittest.TestCase):
    def _entry(self, **overrides):
        entry = {
            "gamePk": 777001,
            "gameDate": "2026-06-01T23:05:00Z",
            "officialDate": "2026-06-01",
            "gameType": "R",
            "season": "2026",
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
            "teams": {"home": {"team": {"id": 147}}, "away": {"team": {"id": 111}}},
        }
        entry.update(overrides)
        return entry

    def test_final_games_are_kept(self):
        game = parse_schedule_game(self._entry(), 2026)
        self.assertEqual(game["game_pk"], 777001)
        self.assertEqual(game["date"], "2026-06-01")
        self.assertEqual(game["home_team_id"], 147)

    def test_unplayed_games_are_dropped(self):
        for state in ("Preview", "Live"):
            entry = self._entry(status={"abstractGameState": state,
                                        "detailedState": "Scheduled"})
            self.assertIsNone(parse_schedule_game(entry, 2026))

    def test_postponed_games_are_dropped(self):
        entry = self._entry(status={"abstractGameState": "Preview",
                                    "detailedState": "Postponed"})
        self.assertIsNone(parse_schedule_game(entry, 2026))

    def test_completed_early_counts_as_final(self):
        entry = self._entry(status={"abstractGameState": "Final",
                                    "detailedState": "Completed Early"})
        self.assertIsNotNone(parse_schedule_game(entry, 2026))

    def test_missing_team_is_dropped(self):
        entry = self._entry(teams={"home": {}, "away": {"team": {"id": 111}}})
        self.assertIsNone(parse_schedule_game(entry, 2026))


class WindowTests(unittest.TestCase):
    def test_windows_stop_at_today(self):
        windows = list(month_windows(2026, today=datetime.date(2026, 4, 15)))
        self.assertEqual(windows[0], ("2026-02-01", "2026-02-28"))
        self.assertEqual(windows[-1], ("2026-04-01", "2026-04-15"))

    def test_finished_season_covers_february_to_november(self):
        windows = list(month_windows(2025, today=datetime.date(2026, 8, 10)))
        self.assertEqual(len(windows), 10)
        self.assertEqual(windows[-1], ("2025-11-01", "2025-11-30"))


if __name__ == "__main__":
    unittest.main()

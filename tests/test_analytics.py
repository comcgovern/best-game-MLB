"""Analytics tests: hand-built seasons with a known answer, plus the demo season."""

import unittest

from mlbdelta import analytics, db, demo
from mlbdelta.analytics import (
    best_game_attributions,
    best_game_counts,
    leaderboard,
    load_season,
    player_opponent_deltas,
    rank,
    team_report,
)
from mlbdelta.config import Thresholds
from mlbdelta.ingest import _insert_many

LOOSE = Thresholds(
    min_season_pa=1,
    min_season_outs=1,
    min_games_vs_opponent=1,
    min_games_vs_opponent_pitching=1,
    min_pa_vs_opponent=1,
    min_outs_vs_opponent=1,
)


def batting_row(game_pk, player_id, opp, **kwargs):
    row = {
        "game_pk": game_pk, "player_id": player_id, "season": 2026,
        "date": f"2026-04-{game_pk:02d}", "team_id": 1, "opp_team_id": opp,
        "is_home": 1, "pa": 4, "ab": 4, "h": 0, "b2": 0, "b3": 0, "hr": 0,
        "bb": 0, "ibb": 0, "hbp": 0, "so": 0, "sf": 0, "sh": 0, "sb": 0,
        "cs": 0, "rbi": 0, "r": 0,
    }
    row.update(kwargs)
    return row


def pitching_row(game_pk, player_id, opp, **kwargs):
    row = {
        "game_pk": game_pk, "player_id": player_id, "season": 2026,
        "date": f"2026-04-{game_pk:02d}", "team_id": 1, "opp_team_id": opp,
        "is_home": 1, "is_start": 1, "outs": 18, "bf": 24, "h": 5, "r": 3,
        "er": 3, "bb": 2, "ibb": 0, "hbp": 0, "so": 5, "hr": 1, "pitches": 90,
    }
    row.update(kwargs)
    return row


def build_db(batting=(), pitching=(), teams=()):
    conn = db.connect(":memory:")
    _insert_many(conn, "teams", [
        {"team_id": t, "name": f"Club {t}", "abbrev": f"C{t}", "short_name": f"{t}"}
        for t in (teams or {r["opp_team_id"] for r in list(batting) + list(pitching)} | {1})
    ])
    players = {r["player_id"] for r in list(batting) + list(pitching)}
    _insert_many(conn, "players", [
        {"player_id": p, "name": f"Player {p}"} for p in players
    ])
    _insert_many(conn, "games", [
        {"game_pk": r["game_pk"], "season": 2026, "date": r["date"], "game_type": "R",
         "home_team_id": 1, "away_team_id": r["opp_team_id"]}
        for r in list(batting) + list(pitching)
    ])
    if batting:
        _insert_many(conn, "batting_lines", list(batting))
    if pitching:
        _insert_many(conn, "pitching_lines", list(pitching))
    conn.commit()
    return conn


class DeltaTests(unittest.TestCase):
    def test_baseline_excludes_the_opponent_being_measured(self):
        """A hitter who only ever homers against club 2 must show a real gap."""
        rows = [batting_row(g, 10, 2, h=2, hr=2) for g in range(1, 5)]
        rows += [batting_row(g, 10, 3) for g in range(5, 21)]
        data = load_season(build_db(batting=rows), 2026)

        [vs_two] = player_opponent_deltas(data, "batting", 2, LOOSE)
        self.assertEqual(vs_two["games_vs"], 4)
        self.assertEqual(vs_two["baseline_games"], 16)
        # Baseline is the 16 hitless games only, never the four good ones.
        self.assertLess(vs_two["baseline_per_game"], 0)
        self.assertGreater(vs_two["per_game_vs"], 3)
        self.assertAlmostEqual(
            vs_two["delta_per_game"],
            vs_two["per_game_vs"] - vs_two["baseline_per_game"],
            places=3,
        )
        self.assertAlmostEqual(
            vs_two["delta_total"],
            vs_two["delta_per_game"] * 4,
            places=2,
        )

    def test_a_player_who_faced_only_one_opponent_is_excluded(self):
        rows = [batting_row(g, 10, 2, h=2, hr=1) for g in range(1, 6)]
        data = load_season(build_db(batting=rows), 2026)
        self.assertEqual(player_opponent_deltas(data, "batting", 2, LOOSE), [])

    def test_sample_thresholds_are_enforced(self):
        rows = [batting_row(1, 10, 2, h=3, hr=3)]
        rows += [batting_row(g, 10, 3) for g in range(2, 15)]
        data = load_season(build_db(batting=rows), 2026)
        strict = Thresholds(min_season_pa=1, min_games_vs_opponent=3)
        self.assertEqual(player_opponent_deltas(data, "batting", 2, strict), [])
        self.assertEqual(len(player_opponent_deltas(data, "batting", 2, LOOSE)), 1)

    def test_rate_metric_normalises_playing_time(self):
        """Two identical rate performances differ on total but not on rate."""
        heavy = [batting_row(g, 10, 2, pa=5, ab=5, h=2, hr=1) for g in range(1, 9)]
        light = [batting_row(g, 11, 2, pa=5, ab=5, h=2, hr=1) for g in range(9, 13)]
        filler = [batting_row(g, p, 3) for p in (10, 11) for g in range(20, 40)]
        data = load_season(build_db(batting=heavy + light + filler), 2026)

        rows = {r["player_id"]: r for r in player_opponent_deltas(data, "batting", 2, LOOSE)}
        self.assertGreater(rows[10]["delta_total"], rows[11]["delta_total"])
        self.assertAlmostEqual(rows[10]["delta_rate"], rows[11]["delta_rate"], places=6)

    def test_ranking_orders_by_the_chosen_metric(self):
        rows = [
            {"delta_total": 1.0, "delta_per_game": 9.0, "delta_rate": 0.1},
            {"delta_total": 5.0, "delta_per_game": 2.0, "delta_rate": 0.2},
        ]
        self.assertEqual(rank(rows, "total", 2)[0]["delta_total"], 5.0)
        self.assertEqual(rank(rows, "per_game", 2)[0]["delta_per_game"], 9.0)
        self.assertEqual(rank(rows, "rate", 2)[0]["delta_rate"], 0.2)
        self.assertEqual(rank(rows, "total", 2)[0]["rank"], 1)
        self.assertEqual(rank(rows, "total", 2, ascending=True)[0]["delta_total"], 1.0)

    def test_pitchers_get_a_lower_games_bar_than_batters(self):
        """A starter facing a club twice should still be rankable."""
        rows = [pitching_row(g, 20, 2, r=0) for g in (1, 2)]
        rows += [pitching_row(g, 20, 3, r=4) for g in range(3, 20)]
        data = load_season(build_db(pitching=rows), 2026)
        ranked = player_opponent_deltas(data, "pitching", 2, Thresholds())
        self.assertEqual(len(ranked), 1)
        self.assertGreater(ranked[0]["delta_total"], 0)


class BestGameTests(unittest.TestCase):
    def test_best_game_is_attributed_to_the_opponent_faced(self):
        rows = [batting_row(1, 10, 2, h=4, hr=3)]  # the season's best night
        rows += [batting_row(g, 10, 3) for g in range(2, 10)]
        data = load_season(build_db(batting=rows), 2026)

        [attribution] = best_game_attributions(data, "batting", LOOSE)
        self.assertEqual(attribution["opp_team_id"], 2)
        self.assertGreater(attribution["margin_over_second_best"], 0)

    def test_counts_add_up_to_the_number_of_qualified_players(self):
        rows = []
        for player_id in range(10, 20):
            rows.append(batting_row(player_id, player_id, player_id % 3 + 2, h=3, hr=2))
            rows += [batting_row(100 + player_id * 10 + g, player_id, 9)
                     for g in range(3)]
        data = load_season(build_db(batting=rows), 2026)

        counts = best_game_counts(data, LOOSE)
        self.assertEqual(sum(row["batters"] for row in counts), 10)

    def test_ties_are_credited_once(self):
        """Identical best games must not be double counted."""
        rows = [batting_row(1, 10, 2, h=2, hr=2), batting_row(2, 10, 3, h=2, hr=2)]
        rows += [batting_row(g, 10, 4) for g in range(3, 8)]
        data = load_season(build_db(batting=rows), 2026)
        attributions = best_game_attributions(data, "batting", LOOSE)
        self.assertEqual(len(attributions), 1)
        self.assertEqual(attributions[0]["opp_team_id"], 2)  # earlier game wins

    def test_only_one_game_is_flagged_as_the_season_best(self):
        """Tied scores are routine — identical relief outings score the same."""
        rows = [pitching_row(g, 20, 2 + g % 3, outs=3, h=0, r=0, bb=0, so=1, hr=0)
                for g in range(1, 9)]
        data = load_season(build_db(pitching=rows), 2026)
        detail = analytics.player_detail(data, 20, "pitching")
        scores = {g["score"] for g in detail["games"]}
        self.assertEqual(len(scores), 1, "fixture should be all ties")
        self.assertEqual(sum(1 for g in detail["games"] if g["is_season_best"]), 1)

    def test_ties_break_on_date_not_game_id(self):
        """A rained-out game keeps its original low game_pk when it is replayed."""
        rows = [
            batting_row(900, 10, 2, h=2, hr=2, date="2026-09-20"),  # low id, late date
            batting_row(950, 10, 3, h=2, hr=2, date="2026-04-05"),  # high id, early date
        ]
        rows += [batting_row(g, 10, 4) for g in range(1, 6)]
        data = load_season(build_db(batting=rows), 2026)
        [attribution] = best_game_attributions(data, "batting", LOOSE)
        self.assertEqual(attribution["opp_team_id"], 3, "the April game came first")

    def test_the_club_shown_is_the_players_most_recent_one(self):
        """A player traded mid-season should not be listed with his old club."""
        rows = [batting_row(g, 10, 2, team_id=7, date=f"2026-04-{g:02d}")
                for g in range(1, 6)]
        rows += [batting_row(g, 10, 3, team_id=9, date=f"2026-08-{g - 5:02d}")
                 for g in range(6, 11)]
        rows += [batting_row(g, 10, 2, team_id=9, date=f"2026-09-{g - 10:02d}")
                 for g in range(11, 14)]
        data = load_season(build_db(batting=rows, teams=(2, 3, 7, 9)), 2026)
        [row] = player_opponent_deltas(data, "batting", 2, LOOSE)
        self.assertEqual(row["team_id"], 9)

    def test_team_report_bundles_counts_and_rankings(self):
        batting = [batting_row(1, 10, 2, h=4, hr=3)]
        batting += [batting_row(g, 10, 3) for g in range(2, 10)]
        pitching = [pitching_row(50, 20, 2, r=0)]
        pitching += [pitching_row(g, 20, 3, r=5) for g in range(51, 60)]
        data = load_season(build_db(batting=batting, pitching=pitching), 2026)

        report = team_report(data, 2, "total", 10, LOOSE)
        self.assertEqual(report["best_game_counts"]["batters"], 1)
        self.assertEqual(report["best_game_counts"]["pitchers"], 1)
        self.assertEqual(report["best_game_counts"]["total"], 2)
        self.assertTrue(report["batters"][0]["season_best_vs_them"])
        self.assertEqual(report["best_games"]["pitching"][0]["player_id"], 20)


class DemoSeasonTests(unittest.TestCase):
    """End-to-end: a full fictional season must behave like baseball."""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect(":memory:")
        cls.summary = demo.generate(cls.conn, 2026, days=157, seed=20260810)
        cls.data = load_season(cls.conn, 2026)

    def test_season_is_the_right_size(self):
        self.assertGreater(self.summary["games"], 2000)
        self.assertEqual(len(self.data.teams), 30)

    def test_run_environment_is_plausible(self):
        self.assertTrue(3.5 < self.data.context.runs_per_nine < 6.0,
                        self.data.context.runs_per_nine)

    def test_planted_nemeses_rise_to_the_top(self):
        """The analytics must actually find an effect that was really there."""
        found = 0
        for entry in self.summary["planted"]:
            if entry["side"] != "batting":
                continue
            rows = rank(
                player_opponent_deltas(self.data, "batting", entry["opp_team_id"]),
                "total", 5,
            )
            if entry["player_id"] in [r["player_id"] for r in rows]:
                found += 1
        planted_batters = sum(1 for e in self.summary["planted"] if e["side"] == "batting")
        self.assertGreaterEqual(found, planted_batters - 1,
                                f"only {found} of {planted_batters} planted batters ranked")

    def test_every_qualified_player_is_counted_exactly_once(self):
        counts = best_game_counts(self.data)
        batters = len(best_game_attributions(self.data, "batting"))
        pitchers = len(best_game_attributions(self.data, "pitching"))
        self.assertEqual(sum(row["batters"] for row in counts), batters)
        self.assertEqual(sum(row["pitchers"] for row in counts), pitchers)
        self.assertEqual(sum(row["total"] for row in counts), batters + pitchers)

    def test_leaderboards_are_sorted_and_bounded(self):
        rows = leaderboard(self.data, "overall", "total", 25)
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows, sorted(rows, key=lambda r: -r["delta_total"]))
        self.assertEqual({r["side"] for r in leaderboard(self.data, "batting")}, {"batting"})
        self.assertEqual({r["side"] for r in leaderboard(self.data, "pitching")}, {"pitching"})

    def test_player_detail_marks_the_season_best(self):
        top = leaderboard(self.data, "batting", "total", 1)[0]
        detail = analytics.player_detail(
            self.data, top["player_id"], "batting", top["opp_team_id"]
        )
        best = [g for g in detail["games"] if g["is_season_best"]]
        self.assertEqual(len(best), 1)
        self.assertEqual(best[0]["score"], max(g["score"] for g in detail["games"]))
        self.assertTrue(any(g["is_vs_selected"] for g in detail["games"]))

    def test_league_wide_scores_cancel_out(self):
        self.assertAlmostEqual(
            sum(line["score"] for line in self.data.batting), 0, places=4
        )
        self.assertAlmostEqual(
            sum(line["score"] for line in self.data.pitching), 0, places=4
        )


if __name__ == "__main__":
    unittest.main()

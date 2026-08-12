"""The JSON API, exercised over a real socket."""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from mlbdelta import db, demo
from mlbdelta.config import Thresholds
from mlbdelta.server import DashboardHandler, SeasonCache


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.sqlite3"
        conn = db.connect(cls.db_path)
        demo.generate(conn, 2026, days=20, seed=7)
        conn.close()

        handler = type(
            "TestHandler",
            (DashboardHandler,),
            {"cache": SeasonCache(cls.db_path), "thresholds": Thresholds(
                min_season_pa=10, min_season_outs=10, min_games_vs_opponent=1,
                min_games_vs_opponent_pitching=1, min_pa_vs_opponent=1,
                min_outs_vs_opponent=1,
            )},
        )
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()

    def get_json(self, path):
        status, body = self.get(path)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_index_and_assets_are_served(self):
        for path in ("/", "/static/app.js", "/static/styles.css"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)

    def test_meta_lists_teams_and_league_context(self):
        meta = self.get_json("/api/meta")
        self.assertEqual(meta["season"], 2026)
        self.assertEqual(len(meta["teams"]), 30)
        self.assertGreater(meta["league"]["runs_per_nine"], 0)
        self.assertIn("total", meta["metrics"])

    def test_team_report(self):
        team_id = self.get_json("/api/meta")["teams"][0]["team_id"]
        report = self.get_json(f"/api/team?team_id={team_id}&limit=5&metric=per_game")
        self.assertEqual(report["team_id"], team_id)
        self.assertLessEqual(len(report["batters"]), 5)
        self.assertEqual(report["metric"], "per_game")
        self.assertIn("best_game_counts", report)

    def test_leaderboard_sides(self):
        for side in ("overall", "batting", "pitching"):
            payload = self.get_json(f"/api/leaderboard?side={side}&limit=4")
            self.assertLessEqual(len(payload["rows"]), 4)
            if side != "overall" and payload["rows"]:
                self.assertEqual({r["side"] for r in payload["rows"]}, {side})

    def test_player_detail(self):
        row = self.get_json("/api/leaderboard?side=batting&limit=1")["rows"][0]
        detail = self.get_json(
            f"/api/player?player_id={row['player_id']}&side=batting&team_id={row['opp_team_id']}"
        )
        self.assertEqual(detail["player_id"], row["player_id"])
        self.assertTrue(detail["games"])

    def test_bad_requests_are_rejected_not_crashed(self):
        for path in ("/api/team", "/api/player?side=batting"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 400)

    def test_unknown_routes_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_static_path_traversal_is_not_possible(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/static/../../../../etc/passwd")
        self.assertEqual(caught.exception.code, 404)

    def test_the_pitching_side_defaults_to_starters(self):
        team_id = self.get_json("/api/meta")["teams"][0]["team_id"]
        report = self.get_json(f"/api/team?team_id={team_id}")
        self.assertEqual(report["role"], "starters")
        self.assertEqual(self.get_json("/api/meta")["default_role"], "starters")

    def test_role_can_be_widened_and_bad_values_fall_back(self):
        team_id = self.get_json("/api/meta")["teams"][0]["team_id"]
        everything = self.get_json(f"/api/team?team_id={team_id}&role=all")
        self.assertEqual(everything["role"], "all")
        self.assertGreaterEqual(
            everything["qualified"]["pitchers"],
            self.get_json(f"/api/team?team_id={team_id}")["qualified"]["pitchers"],
        )
        self.assertEqual(
            self.get_json(f"/api/team?team_id={team_id}&role=bogus")["role"], "starters"
        )

    def test_asking_for_an_old_season_does_not_pin_later_requests(self):
        """The cache is per-season; a stale one must not become the default."""
        conn = db.connect(self.db_path)
        demo.generate(conn, 2025, days=4, seed=3)
        conn.close()

        self.assertEqual(self.get_json("/api/meta")["season"], 2026)
        self.assertEqual(self.get_json("/api/meta?season=2025")["season"], 2025)
        self.assertEqual(self.get_json("/api/meta")["season"], 2026)

    def test_unknown_metric_falls_back_instead_of_failing(self):
        team_id = self.get_json("/api/meta")["teams"][0]["team_id"]
        report = self.get_json(f"/api/team?team_id={team_id}&metric=bogus")
        self.assertEqual(report["metric"], "total")


if __name__ == "__main__":
    unittest.main()

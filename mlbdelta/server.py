"""A dependency-free JSON API plus the static dashboard that consumes it."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analytics, db
from .config import DEFAULT_DB_PATH, Thresholds
from .analytics import METRICS, SeasonData

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class SeasonCache:
    """Keeps the scored season in memory, reloading when the database changes."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._data: SeasonData | None = None
        self._stamp: tuple[float, int] | None = None

    def _current_stamp(self) -> tuple[float, int]:
        try:
            stat = self.db_path.stat()
            return (stat.st_mtime, stat.st_size)
        except OSError:
            return (0.0, 0)

    def get(self, season: int | None = None) -> SeasonData:
        with self._lock:
            stamp = self._current_stamp()
            if self._data is None or stamp != self._stamp or (
                season is not None and self._data.season != season
            ):
                conn = db.connect(self.db_path)
                try:
                    self._data = analytics.load_season(conn, season)
                finally:
                    conn.close()
                self._stamp = stamp
            return self._data


def _first_int(params: dict, key: str, default=None):
    values = params.get(key)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def _first(params: dict, key: str, default=None):
    values = params.get(key)
    return values[0] if values else default


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "mlbdelta"
    cache: SeasonCache
    thresholds: Thresholds

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # quieter than the default
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
            elif route.startswith("/static/"):
                name = Path(route).name  # no traversal
                self._send_file(STATIC_DIR / name)
            elif route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            elif route.startswith("/api/"):
                self._handle_api(route, params)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep the dashboard up, report the failure
            log.exception("request failed: %s", self.path)
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api(self, route: str, params: dict) -> None:
        season = _first_int(params, "season")
        data = self.cache.get(season)
        metric = _first(params, "metric", "total")
        if metric not in METRICS:
            metric = "total"
        limit = max(1, min(_first_int(params, "limit", 10) or 10, 200))

        if route == "/api/meta":
            self._send_json(self._meta(data))
        elif route == "/api/best-game-counts":
            self._send_json(
                {
                    "season": data.season,
                    "rows": analytics.best_game_counts(data, self.thresholds),
                }
            )
        elif route == "/api/team":
            team_id = _first_int(params, "team_id")
            if team_id is None:
                self._send_json({"error": "team_id required"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                analytics.team_report(data, team_id, metric, limit, self.thresholds)
            )
        elif route == "/api/leaderboard":
            side = _first(params, "side", "overall")
            if side not in ("overall", "batting", "pitching"):
                side = "overall"
            self._send_json(
                {
                    "season": data.season,
                    "side": side,
                    "metric": metric,
                    "metric_label": METRICS[metric],
                    "rows": analytics.leaderboard(
                        data, side, metric, limit, self.thresholds
                    ),
                }
            )
        elif route == "/api/player":
            player_id = _first_int(params, "player_id")
            side = _first(params, "side", "batting")
            if player_id is None or side not in ("batting", "pitching"):
                self._send_json(
                    {"error": "player_id and side required"}, HTTPStatus.BAD_REQUEST
                )
                return
            self._send_json(
                analytics.player_detail(
                    data, player_id, side, _first_int(params, "team_id")
                )
            )
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _meta(self, data: SeasonData) -> dict:
        team_ids = sorted(
            {line["opp_team_id"] for line in data.batting}
            | {line["opp_team_id"] for line in data.pitching}
        )
        return {
            "season": data.season,
            "league": data.context.as_dict(),
            "metrics": METRICS,
            "thresholds": {
                "min_season_pa": self.thresholds.min_season_pa,
                "min_season_outs": self.thresholds.min_season_outs,
                "min_games_vs_opponent": self.thresholds.min_games_vs_opponent,
                "min_games_vs_opponent_pitching": self.thresholds.min_games_vs_opponent_pitching,
                "min_outs_vs_opponent": self.thresholds.min_outs_vs_opponent,
            },
            "counts": {
                "batting_lines": len(data.batting),
                "pitching_lines": len(data.pitching),
                "players": len(data.players),
            },
            "teams": [
                {
                    "team_id": team_id,
                    "name": data.team_name(team_id),
                    "abbrev": data.team_abbrev(team_id),
                }
                for team_id in team_ids
            ],
        }


def serve(
    db_path: Path = DEFAULT_DB_PATH,
    host: str = "127.0.0.1",
    port: int = 8000,
    thresholds: Thresholds = Thresholds(),
) -> None:
    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"cache": SeasonCache(db_path), "thresholds": thresholds},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"mlbdelta dashboard on http://{host}:{port}  (database: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()

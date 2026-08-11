"""A very small MLB Stats API client.

Standard library only: ``urllib`` honours ``HTTPS_PROXY``/``REQUESTS_CA_BUNDLE``
style environments through :func:`urllib.request.getproxies`, so this works
behind corporate proxies without extra dependencies.

The API is free and public; there is no key. Be polite with it: responses are
cached on disk so a re-run costs nothing, and requests are rate limited.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from .config import CACHE_DIR, SPORT_ID, STATSAPI_BASE

log = logging.getLogger(__name__)

USER_AGENT = "mlbdelta/1.0 (+https://github.com/comcgovern/best-game-mlb)"


class StatsApiError(RuntimeError):
    """Raised when the API cannot be reached or returns an unusable response."""


class StatsApiClient:
    def __init__(
        self,
        base_url: str = STATSAPI_BASE,
        cache_dir: Path | None = CACHE_DIR,
        timeout: float = 30.0,
        max_retries: int = 4,
        min_interval: float = 0.10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0

    # ------------------------------------------------------------------ http

    def _wait_turn(self) -> None:
        with self._throttle_lock:
            gap = time.monotonic() - self._last_request
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last_request = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait_turn()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx other than rate limiting will not fix themselves.
                if exc.code not in (429, 500, 502, 503, 504):
                    raise StatsApiError(f"{exc.code} {exc.reason} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt == self.max_retries - 1:
                break  # no point sleeping before giving up
            backoff = (2**attempt) + random.uniform(0, 0.5)
            log.warning("retrying %s in %.1fs (%s)", url, backoff, last_error)
            time.sleep(backoff)

        raise StatsApiError(f"giving up on {url}: {last_error}")

    # ----------------------------------------------------------------- cache

    def _cache_path(self, kind: str, key: str | int) -> Path | None:
        if not self.cache_dir:
            return None
        directory = self.cache_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.json.gz"

    def _cached_get(self, kind: str, key: str | int, path: str, params=None) -> dict:
        cache_path = self._cache_path(kind, key)
        if cache_path and cache_path.exists():
            try:
                with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, json.JSONDecodeError):
                log.warning("discarding corrupt cache entry %s", cache_path)
                cache_path.unlink(missing_ok=True)

        payload = self.get(path, params)
        if cache_path:
            tmp = cache_path.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            tmp.replace(cache_path)
        return payload

    # ------------------------------------------------------------ endpoints

    def teams(self, season: int) -> list[dict]:
        payload = self.get("teams", {"sportId": SPORT_ID, "season": season})
        return payload.get("teams", [])

    def schedule(
        self,
        season: int,
        start_date: str,
        end_date: str,
        game_types: Iterable[str],
    ) -> list[dict]:
        """Return the flattened list of scheduled games in a date window."""
        payload = self.get(
            "schedule",
            {
                "sportId": SPORT_ID,
                "season": season,
                "startDate": start_date,
                "endDate": end_date,
                "gameType": list(game_types),
            },
        )
        games: list[dict] = []
        for day in payload.get("dates", []):
            games.extend(day.get("games", []))
        return games

    def boxscore(self, game_pk: int) -> dict:
        """Box score for one game. Cached on disk — final games never change."""
        return self._cached_get("boxscore", game_pk, f"game/{game_pk}/boxscore")

    def forget_boxscore(self, game_pk: int) -> None:
        """Drop a cached box score so the next run fetches it again.

        Used when a response parses to nothing: caching that would make the
        empty result permanent.
        """
        cache_path = self._cache_path("boxscore", game_pk)
        if cache_path:
            cache_path.unlink(missing_ok=True)

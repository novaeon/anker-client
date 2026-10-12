"""Steam's achievement list for an app: names, descriptions, icons, hidden flag and rarity.

Source (verified 2026-10-13; no Web API key needed)::

    GET https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid=<id>&language=english
    → {"response": {"achievements": [{
          "internal_name": "VIVIAN_CAMPAIGN",           # = the name emulators store unlocks under
          "localized_name": "Good Girl Gone Vlad",
          "localized_desc": "Complete all of Vivian's major commissions.",   # also for hidden ones
          "icon": "<sha1>.jpg", "icon_gray": "<sha1>.jpg",
          "hidden": false, "player_percent_unlocked": "4.9", ...}]}}
    → {"response": {}} for an app without achievements (or an unknown app id).

Icons live at ``https://shared.fastly.steamstatic.com/community_assets/images/apps/<id>/<file>``.

Lists are cached in the ``achievement_schemas`` table (``[]`` = the app has no
achievements) and refreshed after ``max_age`` (rarity drifts slowly). When Steam
cannot be reached a stale cached list is used; ``None`` means nothing is known.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from anker_client.core.db import Database
from anker_client.core.errors import AnkerError
from anker_client.core.models import Achievement
from anker_client.core.tasks import CancelToken
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

SCHEMA_URL = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/"
ICON_BASE = "https://shared.fastly.steamstatic.com/community_assets/images/apps"
DEFAULT_LANGUAGE = "english"
MAX_AGE_SECONDS = 7 * 24 * 3600
_TIMEOUT = (5.0, 10.0)


def icon_url(app_id: int, file_name: str) -> str:
    file_name = (file_name or "").strip()
    if not file_name:
        return ""
    if file_name.startswith(("http://", "https://")):
        return file_name
    return f"{ICON_BASE}/{app_id}/{file_name}"


def _percent(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def parse_schema(app_id: int, document: Any) -> list[Achievement]:
    """``GetGameAchievements`` JSON → definitions in Steam's order (unknown fields ignored)."""
    response = document.get("response") if isinstance(document, dict) else None
    if not isinstance(response, dict):
        raise ValueError("missing 'response' object")
    entries = response.get("achievements") or []
    if not isinstance(entries, list):
        raise ValueError("'achievements' is not a list")
    result: list[Achievement] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("internal_name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        result.append(Achievement(
            api_name=name,
            name=str(entry.get("localized_name") or ""),
            description=str(entry.get("localized_desc") or ""),
            hidden=bool(entry.get("hidden")),
            icon_url=icon_url(app_id, str(entry.get("icon") or "")),
            icon_gray_url=icon_url(app_id, str(entry.get("icon_gray") or "")),
            global_percent=_percent(entry.get("player_percent_unlocked")),
        ))
    return result


class SteamAchievementSchemas:
    """Thread-safe: the database serialises writes; concurrent fetches of one app just race benignly."""

    def __init__(self, http: HttpClient, db: Database, *, max_age: float = MAX_AGE_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._http = http
        self._db = db
        self._max_age = max_age
        self._clock = clock

    def cached(self, app_id: int, language: str = DEFAULT_LANGUAGE) -> list[Achievement] | None:
        """The cached list, however old (no network). ``None`` when the app was never fetched."""
        row = self._db.query_one(
            "SELECT data FROM achievement_schemas WHERE app_id = ? AND language = ?", (app_id, language)
        )
        if row is None:
            return None
        try:
            return [Achievement.from_dict(item) for item in json.loads(row["data"])]
        except (ValueError, TypeError):
            log.warning("Discarding an unreadable cached achievement list for app %s", app_id)
            return None

    def is_fresh(self, app_id: int, language: str = DEFAULT_LANGUAGE) -> bool:
        fetched = self._db.scalar(
            "SELECT fetched_at FROM achievement_schemas WHERE app_id = ? AND language = ?", (app_id, language)
        )
        if not fetched:
            return False
        try:
            age = self._clock() - datetime.fromisoformat(fetched).timestamp()
        except ValueError:
            return False
        return 0 <= age < self._max_age

    def get(self, app_id: int, language: str = DEFAULT_LANGUAGE, *, refresh: bool = False,
            token: CancelToken | None = None) -> list[Achievement] | None:
        """Steam's list for ``app_id``: cached when fresh, else fetched; stale cache when offline."""
        if not refresh and self.is_fresh(app_id, language):
            return self.cached(app_id, language)
        try:
            achievements = self.fetch(app_id, language, token=token)
        except (AnkerError, ValueError) as exc:
            log.info("Could not fetch Steam achievements for app %s: %s", app_id, exc)
            return self.cached(app_id, language)
        self._store(app_id, language, achievements)
        return achievements

    def fetch(self, app_id: int, language: str = DEFAULT_LANGUAGE,
              *, token: CancelToken | None = None) -> list[Achievement]:
        document = self._http.get_json(
            SCHEMA_URL, params={"appid": app_id, "language": language}, timeout=_TIMEOUT, token=token
        )
        return parse_schema(app_id, document)

    def _store(self, app_id: int, language: str, achievements: list[Achievement]) -> None:
        stamp = datetime.fromtimestamp(self._clock(), UTC).isoformat()
        self._db.execute(
            "INSERT INTO achievement_schemas (app_id, language, data, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(app_id, language) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            (app_id, language, json.dumps([a.to_dict() for a in achievements]), stamp),
        )

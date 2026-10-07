"""Game update detection and AnkerClient self-update check.

Game updates (``UpdateService.check``):
* For every managed install with a slug (optionally only ``install_ids``):
  ``catalog.details(slug, max_age=1h)`` (sequential, cancellable,
  ``token.sleep(0.5)`` before each network fetch — not when the details cache
  is fresh, and each slug is looked up once per check).
* Update available when ``versions_differ(installed.version, details.version)``
  and ``compare_versions(installed, latest) <= 0``; if either version is
  unknown, fall back to ``details.updated_date > installed.source_updated_date``
  (both ISO dates/datetimes, both non-empty; compared by calendar date when
  either side has no time).
* Patch detection: a ``DownloadKind.PATCH`` option whose ``from_version``
  normalises equal to the installed version → ``GameUpdate.patch_option``
  (preferring one whose ``to_version`` is the latest version).
  ``full_option`` = ``details.primary_option``.
* Persist via ``library.set_update_state`` for every checked game (which also
  clears stale flags), publish ``UpdatesFound`` (only the games with updates,
  and only when there is at least one) and store ``meta['updates_checked_at']``
  after a check of all games. Network errors (or a removed game page) for one
  game are logged and skipped; ``OperationCancelled`` propagates.
* ``pending`` rebuilds the list from the library's DB flags + cached details.

App updates (``AppUpdateChecker.check``): ``GET LATEST_RELEASE_API``; when
``tag_name`` (strip leading "v"; numeric dotted comparison, so "1.0.10" >
"1.0.9" and "1.1" == "1.1.0") > ``anker_client.__version__`` → ``AppRelease``
(+ the first ``.exe`` asset URL) and ``AppUpdateAvailable``. Drafts,
prereleases and malformed payloads → None. Rate-limit/HTTP errors → None (logged).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from anker_client.constants import LATEST_RELEASE_API, RELEASES_URL
from anker_client.core.db import Database
from anker_client.core.errors import AnkerError, OperationCancelled
from anker_client.core.events import AppUpdateAvailable, EventBus, UpdatesFound
from anker_client.core.formatting import compare_versions, normalize_version, versions_differ
from anker_client.core.models import (
    AppRelease,
    DownloadKind,
    DownloadOption,
    GameDetails,
    GameUpdate,
    InstalledGame,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.catalog import CatalogService
from anker_client.services.library import LibraryService
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

DETAILS_MAX_AGE = timedelta(hours=1)
PAUSE_BETWEEN_GAMES_SECONDS = 0.5
META_UPDATES_CHECKED_AT = "updates_checked_at"

_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_RELEASE_VERSION_RE = re.compile(r"^\s*(?:version|v)?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)


# --- pure helpers -----------------------------------------------------------------------


def _parse_moment(value: str) -> tuple[datetime, bool] | None:
    """(aware datetime, has_time) for an ISO date or datetime string."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    has_time = "T" in text or " " in text
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)), has_time


def _is_newer_date(latest: str, installed: str) -> bool:
    """True when ISO ``latest`` is after ISO ``installed`` (by date when either has no time)."""
    a, b = _parse_moment(latest), _parse_moment(installed)
    if a is None or b is None:
        return False
    (latest_dt, latest_has_time), (installed_dt, installed_has_time) = a, b
    if not (latest_has_time and installed_has_time):
        return latest_dt.date() > installed_dt.date()
    return latest_dt > installed_dt


def _update_available(game: InstalledGame, details: GameDetails) -> bool:
    installed, latest = game.version, details.version
    if normalize_version(installed) and normalize_version(latest):
        return versions_differ(installed, latest) and compare_versions(installed, latest) <= 0
    return _is_newer_date(details.updated_date, game.source_updated_date)


def _find_patch_option(details: GameDetails, installed_version: str) -> DownloadOption | None:
    installed = normalize_version(installed_version)
    if not installed:
        return None
    patches = [
        option
        for option in details.download_options
        if option.kind is DownloadKind.PATCH and normalize_version(option.from_version) == installed
    ]
    latest = normalize_version(details.version)
    for option in patches:
        if latest and normalize_version(option.to_version) == latest:
            return option
    return patches[0] if patches else None


def _build_update(game: InstalledGame, details: GameDetails | None) -> GameUpdate:
    if details is None:
        return GameUpdate(
            install_id=game.install_id,
            slug=game.slug,
            title=game.title,
            installed_version=game.version,
            latest_version=game.latest_version,
        )
    return GameUpdate(
        install_id=game.install_id,
        slug=game.slug,
        title=game.title,
        installed_version=game.version,
        latest_version=details.version or game.latest_version,
        patch_option=_find_patch_option(details, game.version),
        full_option=details.primary_option,
    )


def _release_version(text: str) -> tuple[int, ...] | None:
    match = _RELEASE_VERSION_RE.match(text or "")
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:  # "1.1" == "1.1.0"
        parts.pop()
    return tuple(parts)


# --- game updates -----------------------------------------------------------------------


class UpdateService:
    def __init__(
        self,
        db: Database,
        library: LibraryService,
        catalog: CatalogService,
        events: EventBus,
        *,
        pause_seconds: float = PAUSE_BETWEEN_GAMES_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._library = library
        self._catalog = catalog
        self._events = events
        self._pause = max(0.0, pause_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    def last_checked(self) -> str:
        return self._db.get_meta(META_UPDATES_CHECKED_AT) or ""

    def pending(self) -> list[GameUpdate]:
        """Updates found by the most recent check (from DB flags + cached details)."""
        updates = []
        for game in self._library.games():
            if game.update_available and game.slug:
                updates.append(_build_update(game, self._catalog.cached_details(game.slug)))
        return updates

    def check(
        self,
        *,
        token: CancelToken,
        install_ids: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[GameUpdate]:
        wanted = set(install_ids) if install_ids is not None else None
        games = [
            g
            for g in self._library.games()
            if g.managed and g.slug and (wanted is None or g.install_id in wanted)
        ]
        total = len(games)
        log.info("Checking %d installed games for updates", total)
        updates: list[GameUpdate] = []
        details_by_slug: dict[str, GameDetails | None] = {}
        fetched_any = False
        for done, game in enumerate(games, start=1):
            token.raise_if_cancelled()
            if game.slug not in details_by_slug:
                needs_network = not self._has_fresh_details(game.slug)
                if needs_network and fetched_any:
                    token.sleep(self._pause)
                fetched_any = fetched_any or needs_network
                details_by_slug[game.slug] = self._fetch_details(game, token)
            details = details_by_slug[game.slug]
            if details is not None:
                update = self._evaluate(game, details)
                if update is not None:
                    updates.append(update)
            if on_progress is not None:
                on_progress(done, total)
        if wanted is None:
            self._db.set_meta(META_UPDATES_CHECKED_AT, self._clock().astimezone(UTC).replace(microsecond=0).isoformat())
        if updates:
            self._events.publish(UpdatesFound(updates=tuple(u.copy() for u in updates)))
        log.info("Update check finished: %d of %d games have updates", len(updates), total)
        return updates

    # --- internals --------------------------------------------------------------------
    def _has_fresh_details(self, slug: str) -> bool:
        cached = self._catalog.cached_details(slug)
        if cached is None:
            return False
        fetched = _parse_moment(cached.fetched_at)
        if fetched is None:
            return False
        age = self._clock() - fetched[0]
        return timedelta(0) <= age < DETAILS_MAX_AGE

    def _fetch_details(self, game: InstalledGame, token: CancelToken) -> GameDetails | None:
        try:
            return self._catalog.details(game.slug, max_age=DETAILS_MAX_AGE, token=token)
        except OperationCancelled:
            raise
        except AnkerError as exc:
            log.warning("Skipping the update check for %s: %s", game.title, exc)
            return None
        except Exception:
            log.exception("Unexpected error while checking %s for updates", game.title)
            return None

    def _evaluate(self, game: InstalledGame, details: GameDetails) -> GameUpdate | None:
        available = _update_available(game, details)
        try:
            self._library.set_update_state(game.install_id, latest_version=details.version, available=available)
        except OperationCancelled:
            raise
        except AnkerError as exc:
            log.warning("Could not record the update state of %s: %s", game.title, exc)
        except Exception:
            log.exception("Unexpected error while recording the update state of %s", game.title)
        return _build_update(game, details) if available else None


# --- app updates ------------------------------------------------------------------------


class AppUpdateChecker:
    def __init__(self, http: HttpClient, events: EventBus, current_version: str) -> None:
        self._http = http
        self._events = events
        self._current_version = current_version
        self._current = _release_version(current_version) or (0,)

    def check(self, *, token: CancelToken | None = None) -> AppRelease | None:
        try:
            data = self._http.get_json(LATEST_RELEASE_API, headers=_GITHUB_HEADERS, timeout=(10, 20), token=token)
        except OperationCancelled:
            raise
        except AnkerError as exc:
            log.info("App update check failed: %s", exc)
            return None
        release = self._parse_release(data)
        if release is None:
            return None
        latest = _release_version(release.version)
        if latest is None or latest <= self._current:
            log.info("AnkerClient %s is up to date (latest release %s)", self._current_version, release.version)
            return None
        log.info("AnkerClient %s is available (running %s)", release.version, self._current_version)
        self._events.publish(AppUpdateAvailable(release=release.copy()))
        return release

    @staticmethod
    def _parse_release(data: Any) -> AppRelease | None:
        if not isinstance(data, dict):
            log.info("App update check: unexpected response %r", type(data).__name__)
            return None
        tag = data.get("tag_name")
        if not isinstance(tag, str) or _release_version(tag) is None:
            log.info("App update check: release without a usable tag_name (%r)", tag)
            return None
        if data.get("draft") or data.get("prerelease"):
            return None
        version = re.sub(r"^\s*v\s*", "", tag.strip(), flags=re.IGNORECASE)
        assets = data.get("assets") if isinstance(data.get("assets"), list) else []
        download_url = next(
            (
                str(asset.get("browser_download_url") or "")
                for asset in assets
                if isinstance(asset, dict)
                and str(asset.get("name") or "").lower().endswith(".exe")
                and asset.get("browser_download_url")
            ),
            "",
        )
        return AppRelease(
            version=version,
            url=str(data.get("html_url") or RELEASES_URL),
            notes=str(data.get("body") or ""),
            published_at=str(data.get("published_at") or ""),
            download_url=download_url,
        )

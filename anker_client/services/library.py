"""Installed games: discovery, metadata, user data, uninstall.

Source of truth: game folders under each ``settings.library_dirs`` root.

* Managed install = folder with a manifest (``install.installer.read_manifest``).
  ``install_id`` = manifest slug when set, else ``"local:" + folder.casefold()``.
  If two folders claim the same id, the one the ``installs`` row already points
  at (else the first in scan order: roots in settings order, folders by name)
  keeps it; the other gets ``"<slug>#<folder>"`` (folder casefolded;
  ``"local:<folder>#<n>"`` for same-named folders in different roots, and a
  ``#<n>`` suffix for any remaining clash). An id previously assigned
  to a folder (row with the same path) is reused so user data stays attached.
  ``register_install``/``adopt`` follow the same rules from the DB as well as
  the cache, so they agree with the next scan even before the first one.
* Unmanaged folder = any other direct sub-directory (ignoring
  ``LIBRARY_IGNORED_DIRNAMES``, hidden/system/dot folders, ``*.ankerclient-old-*``
  backups, other configured library roots, the download folder, empty folders,
  folders holding AnkerClient itself — its executable or data folders, e.g. a
  portable copy kept in a library — and plain files). ``scan`` returns them as
  ``InstalledGame(managed=False)``
  with ``install_id = "local:<folder>"``, title = folder name, enriched from
  the catalog via ``catalog.match_title`` (slug/cover/genres) when available;
  ``executable`` = ``executables.find_executable`` result (not persisted;
  cached per folder mtime). Managed installs whose manifest has no slug are
  enriched the same way (display only, the id stays ``local:``).
* ``adopt`` writes a manifest for an unmanaged folder (import / legacy
  migration) — slug may be "" if unmatched; ``installed_at`` = folder mtime.
  User data stored under the old id (playtime…) moves to the new id.
* Setters that need a manifest (exe/args/admin/title/redist) on an unmanaged
  folder write a minimal manifest with an empty slug, so the id never changes
  under the caller.
* Per-user data lives in the ``installs`` table, keyed by ``install_id``
  (favorite, hidden, playtime, last_played, cached size, update flags) and is
  merged into the returned ``InstalledGame`` objects. Rows for vanished games
  are kept (re-installs keep playtime) but not returned.
* ``games()`` returns cached copies sorted by title (call ``scan`` to
  refresh); ``scan`` runs on a worker thread, holds a lock, publishes
  ``LibraryChanged``. Every mutation is serialised with scans through the same
  lock so a scan can never resurrect stale data.
* ``uninstall``: refuse unless the path is strictly inside a configured library
  root (lexically and after resolving links), is not itself a link/junction,
  does not contain a library root or AnkerClient itself, is not
  ``is_dangerous_delete_target`` and no program from inside it is running
  (deleting a running game would leave it half-deleted); delete bottom-up
  (extended-length paths) without following links, clearing read-only attributes and retrying Windows
  sharing violations; the manifest is deleted last so an interrupted uninstall
  leaves a recognisable folder. Cancellable between files. Remove shortcuts
  (``ShortcutService.remove(title)``, and ``remove(folder name)`` too — the
  install-time title — after a ``rename``, unless another game is titled like
  that); keep the DB row's playtime/favorite (size and update flags are
  cleared); publish ``GameUninstalled`` + ``LibraryChanged``.
* ``compute_size`` caches into ``installs.size_bytes`` (not when the game was
  uninstalled while it was being measured).
* ``register_install(result, request)`` (called by the download manager after
  a successful install): writes/updates the DB row (size, clears the update
  flag when the new version is current), rescans that folder, returns the
  ``InstalledGame``.
* Setters update the manifest (exe/args/admin/title) or DB (favorite/hidden),
  then publish ``LibraryChanged(install_ids={id})`` (DB-only setters publish
  only when something actually changed).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psutil

from anker_client.constants import LIBRARY_IGNORED_DIRNAMES, MANIFEST_FILENAME
from anker_client.core.db import Database
from anker_client.core.errors import AnkerError, InstallError, LaunchError, NotFoundError
from anker_client.core.events import EventBus, GameUninstalled, LibraryChanged
from anker_client.core.formatting import compare_versions, versions_differ
from anker_client.core.models import (
    DownloadKind,
    GameSummary,
    InstalledGame,
    InstallManifest,
    InstallRequest,
    InstallResult,
    utc_now_iso,
)
from anker_client.core.paths import AppPaths, is_dangerous_delete_target, sanitize_windows_name
from anker_client.core.settings import Settings, SettingsStore
from anker_client.core.tasks import NEVER, CancelToken
from anker_client.services import _library_fs as fs
from anker_client.services.catalog import CatalogService
from anker_client.services.install import executables as _executables
from anker_client.services.install import installer as _installer
from anker_client.services.install.shortcuts import ShortcutService

log = logging.getLogger(__name__)

_LOCAL_PREFIX = "local:"
_BACKUP_MARKER = ".ankerclient-old-"
_IGNORED_FOLDED = frozenset(name.casefold() for name in LIBRARY_IGNORED_DIRNAMES)

_UPSERT_IDENTITY = """
    INSERT INTO installs(install_id, path, slug, title) VALUES(?, ?, ?, ?)
    ON CONFLICT(install_id) DO UPDATE SET path = excluded.path, slug = excluded.slug, title = excluded.title
"""

Row = dict[str, Any]


@dataclass(slots=True)
class _Folder:
    """A candidate game folder found under a library root."""

    root: str  # the configured root string (as stored in settings)
    path: str  # absolute, normalised folder path
    name: str
    manifest: InstallManifest | None


def _base_id(folder: _Folder) -> str:
    if folder.manifest is not None and folder.manifest.slug:
        return folder.manifest.slug
    return _LOCAL_PREFIX + folder.name.casefold()


def _apply_row(game: InstalledGame, row: Row | None) -> None:
    if not row:
        return
    game.favorite = bool(row.get("favorite"))
    game.hidden = bool(row.get("hidden"))
    game.playtime_seconds = int(row.get("playtime_seconds") or 0)
    game.last_played = str(row.get("last_played") or "")
    size = row.get("size_bytes")
    game.size_bytes = int(size) if size is not None else None
    game.latest_version = str(row.get("latest_version") or "")
    game.update_available = bool(row.get("update_available"))


def _game_from_manifest(install_id: str, folder: _Folder, manifest: InstallManifest) -> InstalledGame:
    return InstalledGame(
        install_id=install_id,
        title=manifest.title or folder.name,
        path=folder.path,
        library_root=folder.root,
        slug=manifest.slug,
        managed=True,
        version=manifest.version,
        source_updated_date=manifest.source_updated_date,
        installed_at=manifest.installed_at or fs.mtime_iso(folder.path),
        executable=manifest.executable,
        launch_args=manifest.launch_args,
        run_as_admin=manifest.run_as_admin,
        applied_options=list(manifest.applied_options),
        has_redist=manifest.has_redist,
        redist_installed=manifest.redist_installed,
        cover_url=manifest.cover_url,
        genres=list(manifest.genres),
    )


def _sorted_copies(games: Iterable[InstalledGame]) -> list[InstalledGame]:
    return sorted((g.copy() for g in games), key=lambda g: (g.title.casefold(), g.install_id))


def _client_paths() -> list[str]:
    """Where AnkerClient itself lives: its executable and data folders (portable installs may sit in a library)."""
    paths = [sys.executable] if sys.executable else []
    try:
        app = AppPaths.default()
    except Exception:
        log.debug("Could not determine the application folders", exc_info=True)
        return paths
    return [*paths, *(str(d) for d in (app.config_dir, app.cache_dir, app.logs_dir, app.webengine_dir))]


def _holds_any(folder: str, paths: Iterable[str]) -> bool:
    return any(fs.same_path(p, folder) or fs.is_strictly_inside(p, folder) for p in paths)


def _link_stem(title: str) -> str:
    return sanitize_windows_name(title).casefold()


def _running_executables() -> list[str]:
    """Executable paths of the live processes we may inspect (protected ones are skipped)."""
    exes: list[str] = []
    for proc in psutil.process_iter(["exe"]):
        exe = proc.info.get("exe")
        if exe:
            exes.append(str(exe))
    return exes


class LibraryService:
    def __init__(
        self,
        db: Database,
        settings: SettingsStore,
        events: EventBus,
        shortcuts: ShortcutService,
        catalog: CatalogService | None = None,
        *,
        delete_retry_delay: float = 0.25,
    ) -> None:
        self._db = db
        self._settings = settings
        self._events = events
        self._shortcuts = shortcuts
        self._catalog = catalog
        self._delete_retry_delay = delete_retry_delay
        # Guards the in-memory cache only (fast; never held during I/O).
        self._lock = threading.Lock()
        # Serialises scans and every mutation (manifest writes, DB writes + cache refresh).
        self._io_lock = threading.RLock()
        self._games: dict[str, InstalledGame] = {}
        self._uninstalling: set[str] = set()
        # norm_key(path) → (folder mtime_ns, title used, detected exe) for unmanaged folders.
        self._exe_cache: dict[str, tuple[int, str, str]] = {}

    # --- queries ----------------------------------------------------------------------
    def scan(self, *, token: CancelToken | None = None) -> list[InstalledGame]:
        token = token or NEVER
        with self._io_lock:
            settings = self._settings.get()
            roots = self._roots(settings)
            skip_keys = {fs.norm_key(root) for root in roots}
            if settings.download_dir.strip():
                skip_keys.add(fs.norm_key(settings.download_dir))
            client_paths = _client_paths()
            folders: list[_Folder] = []
            for root in roots:
                token.raise_if_cancelled()
                folders.extend(self._list_folders(root, skip_keys, token, client_paths))
            rows = self._load_rows()
            games: dict[str, InstalledGame] = {}
            for install_id, folder in self._assign_ids(folders, rows):
                token.raise_if_cancelled()
                games[install_id] = self._build_game(folder, install_id, rows.get(install_id))
            self._sync_identity_rows(games.values(), rows)
            with self._lock:
                self._games = games
            result = _sorted_copies(games.values())
        log.info("Library scan found %d game(s) in %d folder(s)", len(result), len(roots))
        self._events.publish(LibraryChanged())
        return result

    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]:
        with self._lock:
            selected = [g for g in self._games.values() if include_hidden or not g.hidden]
            return _sorted_copies(selected)

    def get(self, install_id: str) -> InstalledGame | None:
        with self._lock:
            game = self._games.get(install_id)
            return game.copy() if game is not None else None

    def find_by_slug(self, slug: str) -> InstalledGame | None:
        if not slug:
            return None
        with self._lock:
            matches = [g for g in self._games.values() if g.slug == slug]
            if not matches:
                return None
            # Prefer a managed install, then a visible one, then the canonical (un-suffixed) id.
            best = min(matches, key=lambda g: (not g.managed, g.hidden, g.install_id != slug, g.install_id))
            return best.copy()

    def find_by_path(self, path: str) -> InstalledGame | None:
        """The game installed at ``path`` (or containing ``path``, e.g. an executable)."""
        if not path:
            return None
        key = fs.norm_key(path)
        with self._lock:
            for game in self._games.values():
                if fs.norm_key(game.path) == key:
                    return game.copy()
            for game in self._games.values():
                if fs.is_strictly_inside(key, game.path):
                    return game.copy()
        return None

    def executable_candidates(self, install_id: str) -> list[str]:
        game = self._require(install_id)
        try:
            ranked = _executables.executable_candidates(game.path, game.title)
        except AnkerError:
            raise
        except Exception:
            log.warning("Executable search failed for %s", game.path, exc_info=True)
            return []
        candidates = [relative for relative, _score in ranked]
        if game.executable and game.executable not in candidates:
            candidates.insert(0, game.executable)
        return candidates

    # --- mutations --------------------------------------------------------------------
    def register_install(self, result: InstallResult, request: InstallRequest) -> InstalledGame:
        path = os.path.normpath(os.path.abspath(result.install_path))
        if not os.path.isdir(path):
            raise InstallError("The installed game folder is missing.", detail=path)
        with self._io_lock:
            manifest = self._read_manifest(path)
            if manifest is None:
                log.warning("No manifest after install at %s; writing one from the request", path)
                manifest = self._manifest_from_request(result, request)
                self._write_manifest(path, manifest)
            folder = _Folder(self._root_for(path, request.library_root), path, os.path.basename(path), manifest)
            rows = self._load_rows()
            install_id = self._id_for_single(folder, rows)
            previous_ids = self._ids_at_path(path, rows) - {install_id}
            for old_id in sorted(previous_ids):
                self._merge_rows(old_id, install_id)
            self._db.execute(_UPSERT_IDENTITY, (install_id, path, manifest.slug, manifest.title or folder.name))
            self._record_install_facts(install_id, manifest, result, request)
            game = self._build_game(folder, install_id, self._load_row(install_id))
            with self._lock:
                for gid in [gid for gid, g in self._games.items() if fs.same_path(g.path, path)]:
                    del self._games[gid]
                self._games[install_id] = game
            if not self._root_is_configured(folder.root):
                log.warning("Installed into %s, which is not a configured library folder", folder.root)
            copy = game.copy()
        log.info("Registered install %s at %s", install_id, path)
        self._publish_changed(install_id, *previous_ids)
        return copy

    def adopt(
        self,
        install_id: str,
        *,
        slug: str = "",
        title: str = "",
        cover_url: str = "",
        genres: list[str] | None = None,
    ) -> InstalledGame:
        """Write a manifest for an unmanaged folder (optionally linking it to a catalog slug).

        ``cover_url``/``genres`` override what the catalog knows (used by the legacy migration).
        Adopting an already managed game re-links it to ``slug`` and/or renames it.
        """
        slug = slug.strip()
        title = " ".join(title.split())
        with self._io_lock:
            game = self._require(install_id)
            summary = self._catalog_get(slug) if slug else None
            existing = self._read_manifest(game.path)
            if existing is None:
                manifest = self._new_manifest(game.path, title=title or (summary.title if summary else game.title))
                manifest.executable = game.executable
            else:
                manifest = existing
                manifest.title = title or manifest.title
            keep_artwork = existing is not None and existing.slug == slug
            # The enrichment of an unmanaged game came from the same catalog entry → reuse it.
            enriched_same = bool(slug) and slug == game.slug
            manifest.slug = slug
            manifest.cover_url = (
                cover_url
                or (summary.cover_url if summary is not None else "")
                or (manifest.cover_url if keep_artwork else "")
                or (game.cover_url if enriched_same else "")
            )
            if genres:
                manifest.genres = [g for g in genres if g]
            elif summary is not None and summary.primary_genre:
                manifest.genres = [summary.primary_genre]
            elif not keep_artwork:
                manifest.genres = list(game.genres) if enriched_same else []
            self._write_manifest(game.path, manifest)

            folder = _Folder(game.library_root, game.path, game.folder_name, manifest)
            rows = self._load_rows()
            new_id = self._id_for_single(folder, rows)
            if new_id != install_id:
                self._merge_rows(install_id, new_id)
            adopted = self._build_game(folder, new_id, self._load_row(new_id))
            self._db.execute(_UPSERT_IDENTITY, (new_id, adopted.path, adopted.slug, adopted.title))
            with self._lock:
                self._games.pop(install_id, None)
                self._games[new_id] = adopted
            copy = adopted.copy()
        log.info("Adopted %s as %s (slug=%r)", game.path, new_id, slug)
        self._publish_changed(install_id, new_id)
        return copy

    def set_executable(self, install_id: str, relative_path: str) -> None:
        game = self._require(install_id)
        relative = self._validated_executable(game, relative_path)

        def mutate(manifest: InstallManifest) -> None:
            manifest.executable = relative

        self._mutate_manifest(install_id, mutate)

    def set_launch_options(self, install_id: str, *, args: str = "", run_as_admin: bool = False) -> None:
        def mutate(manifest: InstallManifest) -> None:
            manifest.launch_args = args.strip()
            manifest.run_as_admin = bool(run_as_admin)

        self._mutate_manifest(install_id, mutate)

    def rename(self, install_id: str, title: str) -> None:
        """Change the display title (manifest only; the folder is not renamed)."""
        cleaned = " ".join(title.split())
        if not cleaned:
            raise InstallError("Enter a name for the game.")

        def mutate(manifest: InstallManifest) -> None:
            manifest.title = cleaned

        self._mutate_manifest(install_id, mutate)

    def set_favorite(self, install_id: str, favorite: bool) -> None:
        self._set_flag(install_id, "favorite", bool(favorite))

    def set_hidden(self, install_id: str, hidden: bool) -> None:
        self._set_flag(install_id, "hidden", bool(hidden))

    def mark_redist_installed(self, install_id: str) -> None:
        def mutate(manifest: InstallManifest) -> None:
            manifest.redist_installed = True

        self._mutate_manifest(install_id, mutate)

    def record_play_session(self, install_id: str, started_at: str, ended_at: str, seconds: int) -> None:
        seconds = max(0, int(seconds))
        with self._io_lock:
            with self._lock:
                cached = self._games.get(install_id)
                path, slug, title = (cached.path, cached.slug, cached.title) if cached else ("", "", "")
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO play_sessions(install_id, started_at, ended_at, seconds) VALUES(?, ?, ?, ?)",
                    (install_id, started_at, ended_at, seconds),
                )
                conn.execute(
                    """
                    INSERT INTO installs(install_id, path, slug, title, playtime_seconds, last_played)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(install_id) DO UPDATE SET
                        playtime_seconds = playtime_seconds + excluded.playtime_seconds,
                        last_played = MAX(last_played, excluded.last_played)
                    """,
                    (install_id, path, slug, title, seconds, ended_at),
                )
            row = self._load_row(install_id)
            with self._lock:
                game = self._games.get(install_id)
                if game is not None and row is not None:
                    game.playtime_seconds = int(row["playtime_seconds"] or 0)
                    game.last_played = str(row["last_played"] or "")
        log.info("Recorded %ds of play for %s", seconds, install_id)
        self._publish_changed(install_id)

    def set_update_state(self, install_id: str, *, latest_version: str, available: bool) -> None:
        available = bool(available)
        with self._io_lock:
            with self._lock:
                cached = self._games.get(install_id)
                snapshot = cached.copy() if cached is not None else None
            now = utc_now_iso()
            if snapshot is not None:
                self._db.execute(
                    """
                    INSERT INTO installs(install_id, path, slug, title, latest_version, update_available,
                                         update_checked_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(install_id) DO UPDATE SET latest_version = excluded.latest_version,
                        update_available = excluded.update_available, update_checked_at = excluded.update_checked_at
                    """,
                    (install_id, snapshot.path, snapshot.slug, snapshot.title, latest_version, int(available), now),
                )
            else:
                self._db.execute(
                    "UPDATE installs SET latest_version = ?, update_available = ?, update_checked_at = ? "
                    "WHERE install_id = ?",
                    (latest_version, int(available), now, install_id),
                )
                return
            changed = (snapshot.latest_version, snapshot.update_available) != (latest_version, available)
            with self._lock:
                game = self._games.get(install_id)
                if game is not None:
                    game.latest_version = latest_version
                    game.update_available = available
        if changed:
            self._publish_changed(install_id)

    def compute_size(self, install_id: str, *, token: CancelToken | None = None) -> int:
        token = token or NEVER
        game = self._require(install_id)
        try:
            size = int(_installer.directory_size(game.path, token=token))
        except AnkerError:
            raise
        except OSError as exc:
            raise InstallError("Could not measure the game's size.", detail=str(exc)) from exc
        with self._io_lock:
            with self._lock:
                cached = self._games.get(install_id)
                still_there = cached is not None and fs.same_path(cached.path, game.path)
            if not still_there or install_id in self._uninstalling:
                # Uninstalled (or being uninstalled) while measuring: the figure is stale/partial and
                # must not overwrite the cleared value that a re-install would show.
                log.debug("Not caching the size of %s: it was removed meanwhile", install_id)
                return size
            self._db.execute(
                """
                INSERT INTO installs(install_id, path, slug, title, size_bytes, size_checked_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(install_id) DO UPDATE SET size_bytes = excluded.size_bytes,
                    size_checked_at = excluded.size_checked_at
                """,
                (install_id, game.path, game.slug, game.title, size, utc_now_iso()),
            )
            with self._lock:
                cached = self._games.get(install_id)
                changed = cached is not None and cached.size_bytes != size
                if cached is not None:
                    cached.size_bytes = size
        if changed:
            self._publish_changed(install_id)
        return size

    def uninstall(self, install_id: str, *, token: CancelToken | None = None) -> None:
        token = token or NEVER
        with self._io_lock:
            game = self._require(install_id)
            if install_id in self._uninstalling:
                raise InstallError(f"{game.title} is already being uninstalled.")
            self._check_deletable(game)
            self._uninstalling.add(install_id)
        log.info("Uninstalling %s from %s", install_id, game.path)
        # The id stays marked until the cache is updated, so a second uninstall cannot slip in
        # between the deletion and the bookkeeping (and report the game uninstalled twice).
        try:
            try:
                if os.path.lexists(game.path):
                    progress = fs.delete_tree(
                        game.path, token=token, keep_last=MANIFEST_FILENAME, retry_delay=self._delete_retry_delay
                    )
                    log.info("Deleted %d file(s), %d folder(s), %d link(s) of %s",
                             progress.files, progress.dirs, progress.links, game.title)
            except BaseException:
                # Partially deleted: the manifest (deleted last) still identifies the folder; the
                # cached size is now wrong.
                self._invalidate_size(install_id)
                self._publish_changed(install_id)
                raise

            for name in self._shortcut_names(game):
                try:
                    self._shortcuts.remove(name)
                except Exception:
                    log.warning("Could not remove shortcuts for %s", name, exc_info=True)

            with self._io_lock:
                self._db.execute(
                    "UPDATE installs SET size_bytes = NULL, size_checked_at = '', update_available = 0, "
                    "latest_version = '', update_checked_at = '' WHERE install_id = ?",
                    (install_id,),
                )
                with self._lock:
                    current = self._games.get(install_id)
                    if current is not None and fs.same_path(current.path, game.path):
                        del self._games[install_id]
        finally:
            with self._io_lock:
                self._uninstalling.discard(install_id)
        self._events.publish(GameUninstalled(install_id=install_id, title=game.title))
        self._publish_changed(install_id)

    # --- scanning helpers -----------------------------------------------------------------
    @staticmethod
    def _roots(settings: Settings) -> list[str]:
        roots: list[str] = []
        seen: set[str] = set()
        for root in settings.library_dirs:
            if not root or not root.strip():
                continue
            key = fs.norm_key(root)
            if key not in seen:
                seen.add(key)
                roots.append(root)
        return roots

    def _list_folders(
        self, root: str, skip_keys: set[str], token: CancelToken, client_paths: list[str]
    ) -> list[_Folder]:
        root_path = os.path.normpath(os.path.abspath(root))
        if not os.path.isdir(root_path):
            log.debug("Library folder %s does not exist", root_path)
            return []
        try:
            with os.scandir(root_path) as iterator:
                entries = sorted(iterator, key=lambda e: e.name.casefold())
        except OSError as exc:
            log.warning("Cannot read library folder %s: %s", root_path, exc)
            return []
        folders: list[_Folder] = []
        for entry in entries:
            token.raise_if_cancelled()
            if not self._is_candidate(entry, skip_keys):
                continue
            path = os.path.join(root_path, entry.name)
            if _holds_any(path, client_paths):
                log.debug("Skipping %s: it holds AnkerClient itself", path)
                continue
            manifest = self._read_manifest(path)
            if manifest is None and self._is_empty_dir(path):
                continue
            folders.append(_Folder(root=root, path=path, name=entry.name, manifest=manifest))
        return folders

    @staticmethod
    def _is_candidate(entry: os.DirEntry[str], skip_keys: set[str]) -> bool:
        folded = entry.name.casefold()
        if folded in _IGNORED_FOLDED or entry.name.startswith(".") or _BACKUP_MARKER in folded:
            return False
        try:
            if not entry.is_dir():  # follows links: a linked game folder is still a game
                return False
        except OSError:
            return False
        if fs.entry_attributes(entry) & (fs.FILE_ATTRIBUTE_HIDDEN | fs.FILE_ATTRIBUTE_SYSTEM):
            return False
        return fs.norm_key(entry.path) not in skip_keys

    @staticmethod
    def _is_empty_dir(path: str) -> bool:
        try:
            with os.scandir(path) as iterator:
                return next(iterator, None) is None
        except OSError:
            return True  # unreadable folders are not shown

    def _assign_ids(self, folders: list[_Folder], rows: dict[str, Row]) -> list[tuple[str, _Folder]]:
        groups: dict[str, list[int]] = {}
        for index, folder in enumerate(folders):
            groups.setdefault(_base_id(folder), []).append(index)
        ids: list[str] = [""] * len(folders)
        used: set[str] = set()
        for base, members in groups.items():
            primary = members[0]
            row = rows.get(base)
            if row is not None and len(members) > 1:
                primary = next((i for i in members if fs.same_path(row.get("path"), folders[i].path)), primary)
            ids[primary] = base
            used.add(base)
        rows_by_path = self._rows_by_path(rows)
        for base, members in groups.items():
            for index in members:
                if not ids[index]:
                    ids[index] = self._secondary_id(base, folders[index], used, rows_by_path)
                    used.add(ids[index])
        return list(zip(ids, folders, strict=True))

    @staticmethod
    def _secondary_id(base: str, folder: _Folder, used: set[str], rows_by_path: dict[str, list[str]]) -> str:
        for previous in rows_by_path.get(fs.norm_key(folder.path), []):
            if previous.startswith(base + "#") and previous not in used:
                return previous
        stem = base if base.startswith(_LOCAL_PREFIX) else f"{base}#{folder.name.casefold()}"
        if stem not in used:
            return stem
        number = 2
        while f"{stem}#{number}" in used:
            number += 1
        return f"{stem}#{number}"

    def _id_for_single(self, folder: _Folder, rows: dict[str, Row]) -> str:
        """Id for one (re)scanned folder, consistent with what a full scan would assign.

        The cache alone is not enough: before the first scan (e.g. an install finishing at
        startup) it is empty, and a second copy of a game must not take over the id — and the
        user data — of a copy whose DB row points at another, still existing folder.
        """
        base = _base_id(folder)
        with self._lock:
            used = {gid for gid, g in self._games.items() if not fs.same_path(g.path, folder.path)}
        rows_by_path = self._rows_by_path(rows)
        for previous in rows_by_path.get(fs.norm_key(folder.path), []):
            if (previous == base or previous.startswith(base + "#")) and previous not in used:
                return previous
        owner_path = str((rows.get(base) or {}).get("path") or "")
        if owner_path and not fs.same_path(owner_path, folder.path) and self._is_live_copy(owner_path, base):
            used.add(base)
        if base not in used:
            return base
        return self._secondary_id(base, folder, used, rows_by_path)

    def _is_live_copy(self, path: str, base: str) -> bool:
        """True when ``path`` still holds a game folder whose own id would be ``base``."""
        if not path or not os.path.isdir(path):
            return False
        name = os.path.basename(os.path.normpath(path))
        return _base_id(_Folder("", path, name, self._read_manifest(path))) == base

    def _ids_at_path(self, path: str, rows: dict[str, Row]) -> set[str]:
        """Ids whose cached game or *local* DB row sits at ``path`` (candidates for a data merge)."""
        with self._lock:
            ids = {gid for gid, g in self._games.items() if fs.same_path(g.path, path)}
        ids.update(gid for gid in self._rows_by_path(rows).get(fs.norm_key(path), []) if gid.startswith(_LOCAL_PREFIX))
        return ids

    @staticmethod
    def _rows_by_path(rows: dict[str, Row]) -> dict[str, list[str]]:
        by_path: dict[str, list[str]] = {}
        for install_id, row in rows.items():
            if row.get("path"):
                by_path.setdefault(fs.norm_key(row["path"]), []).append(install_id)
        return by_path

    def _build_game(self, folder: _Folder, install_id: str, row: Row | None) -> InstalledGame:
        manifest = folder.manifest
        if manifest is not None:
            game = _game_from_manifest(install_id, folder, manifest)
            if not manifest.slug:
                self._enrich(game, game.title)
        else:
            game = InstalledGame(
                install_id=install_id,
                title=folder.name,
                path=folder.path,
                library_root=folder.root,
                managed=False,
                installed_at=fs.mtime_iso(folder.path),
            )
            match = self._enrich(game, folder.name)
            game.executable = self._detect_executable(folder.path, match.title if match else folder.name)
        _apply_row(game, row)
        return game

    def _enrich(self, game: InstalledGame, name: str) -> GameSummary | None:
        match = self._match_catalog(name)
        if match is not None:
            game.slug = game.slug or match.slug
            game.cover_url = game.cover_url or match.cover_url
            if not game.genres and match.primary_genre:
                game.genres = [match.primary_genre]
        return match

    def _match_catalog(self, name: str) -> GameSummary | None:
        if self._catalog is None or not name:
            return None
        try:
            return self._catalog.match_title(name)
        except Exception:
            log.debug("Catalog match failed for %r", name, exc_info=True)
            return None

    def _catalog_get(self, slug: str) -> GameSummary | None:
        if self._catalog is None:
            return None
        try:
            return self._catalog.get(slug)
        except Exception:
            log.debug("Catalog lookup failed for %r", slug, exc_info=True)
            return None

    def _detect_executable(self, path: str, title: str) -> str:
        key = fs.norm_key(path)
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = 0
        cached = self._exe_cache.get(key)
        if cached is not None and cached[0] == stamp and cached[1] == title:
            return cached[2]
        try:
            exe = _executables.find_executable(path, title) or ""
        except Exception:
            log.debug("Executable detection failed for %s", path, exc_info=True)
            exe = ""
        self._exe_cache[key] = (stamp, title, exe)
        return exe

    # --- manifest helpers ------------------------------------------------------------------
    @staticmethod
    def _read_manifest(path: str) -> InstallManifest | None:
        try:
            return _installer.read_manifest(path)
        except Exception:
            log.warning("Could not read the manifest in %s", path, exc_info=True)
            return None

    @staticmethod
    def _write_manifest(path: str, manifest: InstallManifest) -> None:
        try:
            _installer.write_manifest(path, manifest)
        except AnkerError:
            raise
        except OSError as exc:
            raise InstallError(
                "Could not save the game's settings file. Check that the folder is writable.",
                detail=f"{path}: {exc}",
            ) from exc

    def _new_manifest(self, path: str, *, title: str) -> InstallManifest:
        now = utc_now_iso()
        try:
            has_redist = bool(_installer.find_redist_dirs(path))
        except Exception:
            log.debug("Redist detection failed for %s", path, exc_info=True)
            has_redist = False
        return InstallManifest(
            title=title,
            installed_at=fs.mtime_iso(path) or now,
            updated_at=now,
            has_redist=has_redist,
        )

    @staticmethod
    def _manifest_from_request(result: InstallResult, request: InstallRequest) -> InstallManifest:
        now = utc_now_iso()
        return InstallManifest(
            slug=request.slug,
            title=request.title,
            version=request.version or request.option.to_version,
            source_updated_date=request.source_updated_date,
            installed_at=now,
            updated_at=now,
            executable=result.executable,
            applied_options=[request.option.label] if request.option.label else [],
            has_redist=result.has_redist,
            cover_url=request.cover_url,
            genres=list(request.genres),
        )

    def _mutate_manifest(self, install_id: str, mutate: Callable[[InstallManifest], None]) -> InstalledGame:
        with self._io_lock:
            game = self._require(install_id)
            manifest = self._read_manifest(game.path)
            if manifest is None:
                # Unmanaged folder: a minimal manifest with no slug keeps the install id stable.
                manifest = self._new_manifest(game.path, title=game.title)
                manifest.executable = game.executable
            mutate(manifest)
            self._write_manifest(game.path, manifest)
            folder = _Folder(game.library_root, game.path, game.folder_name, manifest)
            updated = self._build_game(folder, install_id, self._load_row(install_id))
            self._db.execute(_UPSERT_IDENTITY, (install_id, updated.path, updated.slug, updated.title))
            with self._lock:
                self._games[install_id] = updated
            copy = updated.copy()
        self._publish_changed(install_id)
        return copy

    @staticmethod
    def _validated_executable(game: InstalledGame, path: str) -> str:
        path = path.strip().strip('"')
        if not path:
            return ""
        absolute = path if os.path.isabs(path) else os.path.join(game.path, path)
        if not fs.is_strictly_inside(absolute, game.path):
            raise LaunchError("Choose a program inside the game's folder.")
        relative = os.path.relpath(os.path.normpath(os.path.abspath(absolute)), os.path.abspath(game.path))
        if not os.path.isfile(os.path.join(game.path, relative)):
            raise LaunchError(f'"{relative}" was not found in the game\'s folder.')
        return relative

    # --- DB helpers --------------------------------------------------------------------------
    def _load_rows(self) -> dict[str, Row]:
        try:
            return {row["install_id"]: dict(row) for row in self._db.query("SELECT * FROM installs")}
        except sqlite3.Error:
            log.exception("Could not read library data from the database")
            return {}

    def _load_row(self, install_id: str) -> Row | None:
        row = self._db.query_one("SELECT * FROM installs WHERE install_id = ?", (install_id,))
        return dict(row) if row is not None else None

    def _sync_identity_rows(self, games: Iterable[InstalledGame], rows: dict[str, Row]) -> None:
        pending = []
        for game in games:
            row = rows.get(game.install_id)
            identity = (game.path, game.slug, game.title)
            if row is None or (row.get("path"), row.get("slug"), row.get("title")) != identity:
                pending.append((game.install_id, *identity))
        if pending:
            try:
                self._db.executemany(_UPSERT_IDENTITY, pending)
            except sqlite3.Error:
                log.exception("Could not update library rows")

    def _merge_rows(self, old_id: str, new_id: str) -> None:
        """Move user data from ``old_id`` to ``new_id`` (adding playtime when both exist)."""
        if old_id == new_id:
            return
        with self._db.transaction() as conn:
            old = conn.execute("SELECT * FROM installs WHERE install_id = ?", (old_id,)).fetchone()
            if old is not None:
                exists = conn.execute("SELECT 1 FROM installs WHERE install_id = ?", (new_id,)).fetchone()
                if exists is None:
                    conn.execute("UPDATE installs SET install_id = ? WHERE install_id = ?", (new_id, old_id))
                else:
                    conn.execute(
                        """
                        UPDATE installs SET favorite = MAX(favorite, ?), hidden = MAX(hidden, ?),
                            playtime_seconds = playtime_seconds + ?, last_played = MAX(last_played, ?)
                        WHERE install_id = ?
                        """,
                        (old["favorite"], old["hidden"], old["playtime_seconds"], old["last_played"], new_id),
                    )
                    conn.execute("DELETE FROM installs WHERE install_id = ?", (old_id,))
            conn.execute("UPDATE play_sessions SET install_id = ? WHERE install_id = ?", (new_id, old_id))
        log.debug("Moved library data from %s to %s", old_id, new_id)

    def _record_install_facts(
        self, install_id: str, manifest: InstallManifest, result: InstallResult, request: InstallRequest
    ) -> None:
        now = utc_now_iso()
        if result.size_bytes > 0:
            self._db.execute(
                "UPDATE installs SET size_bytes = ?, size_checked_at = ? WHERE install_id = ?",
                (int(result.size_bytes), now, install_id),
            )
        if request.option.kind in (DownloadKind.FULL, DownloadKind.PATCH):
            row = self._load_row(install_id) or {}
            latest = str(row.get("latest_version") or "")
            still_outdated = bool(
                latest
                and versions_differ(manifest.version, latest)
                and compare_versions(manifest.version, latest) < 0
            )
            self._db.execute(
                "UPDATE installs SET update_available = ? WHERE install_id = ?", (int(still_outdated), install_id)
            )

    def _set_flag(self, install_id: str, column: str, value: bool) -> None:
        if column not in ("favorite", "hidden"):  # interpolated into SQL below
            raise ValueError(column)
        with self._io_lock:
            game = self._require(install_id)
            self._db.execute(
                f"""
                INSERT INTO installs(install_id, path, slug, title, {column}) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(install_id) DO UPDATE SET {column} = excluded.{column}
                """,
                (install_id, game.path, game.slug, game.title, int(value)),
            )
            with self._lock:
                cached = self._games.get(install_id)
                changed = cached is not None and getattr(cached, column) != value
                if cached is not None:
                    setattr(cached, column, value)
        if changed:
            self._publish_changed(install_id)

    def _invalidate_size(self, install_id: str) -> None:
        try:
            with self._io_lock:
                self._db.execute(
                    "UPDATE installs SET size_bytes = NULL, size_checked_at = '' WHERE install_id = ?", (install_id,)
                )
                with self._lock:
                    cached = self._games.get(install_id)
                    if cached is not None:
                        cached.size_bytes = None
        except sqlite3.Error:
            log.warning("Could not reset the cached size of %s", install_id, exc_info=True)

    # --- misc helpers -------------------------------------------------------------------------
    def _require(self, install_id: str) -> InstalledGame:
        game = self.get(install_id)
        if game is None:
            raise NotFoundError("This game is no longer in your library.")
        return game

    def _root_for(self, path: str, fallback: str) -> str:
        roots = self._roots(self._settings.get())
        parent = os.path.dirname(path)
        # The direct parent first: with nested roots (C:\Games and C:\Games\More) both contain the path.
        for root in roots:
            if fs.same_path(root, parent):
                return root
        for root in roots:
            if fs.is_strictly_inside(path, root):
                return root
        return fallback or parent

    def _root_is_configured(self, root: str) -> bool:
        return any(fs.same_path(root, configured) for configured in self._roots(self._settings.get()))

    def _check_deletable(self, game: InstalledGame) -> None:
        roots = self._roots(self._settings.get())
        path = game.path
        if not any(fs.is_safely_inside(path, root) for root in roots):
            raise InstallError(
                f"{game.title} is not inside one of your library folders, so AnkerClient will not delete it.",
                detail=path,
            )
        if fs.is_link(path):
            raise InstallError(
                f"The folder of {game.title} is a link to another location. Remove it manually.", detail=path
            )
        if any(fs.is_strictly_inside(root, path) or fs.same_path(root, path) for root in roots):
            raise InstallError(f"The folder of {game.title} contains a library folder and was not deleted.",
                               detail=path)
        if is_dangerous_delete_target(path):
            raise InstallError(f"AnkerClient will not delete {path}.", detail=path)
        if _holds_any(path, _client_paths()):
            raise InstallError(f"The folder of {game.title} contains AnkerClient itself and was not deleted.",
                               detail=path)
        try:
            folders = {path, os.path.realpath(path)}  # Windows reports images by their resolved path
            running = [exe for exe in _running_executables() if any(fs.is_strictly_inside(exe, f) for f in folders)]
        except Exception:
            log.debug("Could not list running programs", exc_info=True)
            running = []
        if running:
            # Deleting now would remove the unlocked files and then fail on the locked ones,
            # leaving a half-deleted game behind.
            raise InstallError(f"Close {game.title} before uninstalling it.",
                               detail=f"running: {', '.join(sorted(set(running)))}")

    def _shortcut_names(self, game: InstalledGame) -> list[str]:
        """Titles whose shortcuts belong to ``game``.

        Shortcuts are named after the title at install time, which is also the folder name
        (``sanitize_windows_name(title)``); after a ``rename`` only the folder still carries it.
        The folder name is skipped when another game in the library is titled like that.
        """
        names = [game.title]
        folder = game.folder_name
        if not folder or _link_stem(folder) == _link_stem(game.title):
            return names
        with self._lock:
            taken = {_link_stem(g.title) for g in self._games.values() if g.install_id != game.install_id}
        if _link_stem(folder) not in taken:
            names.append(folder)
        return names

    def _publish_changed(self, *install_ids: str) -> None:
        self._events.publish(LibraryChanged(install_ids=frozenset(i for i in install_ids if i)))

"""One-time migration from the pre-1.0 client.

Legacy data (``%APPDATA%\\AnkerClient``):
* ``settings.json`` — handled by ``SettingsStore`` already.
* ``library_cache.json`` — ``{folder_name: {"title", "slug", "cover_url",
  "genres", "size_gb", "release_date", "description", "screenshots",
  "file_size"}}``. Installs live at ``<games_dir>\\<folder_name>`` with no manifest.
* ``covers\\<legacy_cover_name(folder)>.png`` — cached cover images (the
  title-based name is tried too).
* Shortcuts at Desktop and ``Start Menu\\Programs\\<Safe Title>.lnk``
  (``ShortcutService.remove`` already cleans both locations on uninstall).

``migrate`` (idempotent, guarded by ``meta['legacy_migrated'] == "1"``):
scan the library, then for every folder in the configured library roots that
has a legacy cache entry (folder names compared case-insensitively) with a
slug and no manifest → ``library.adopt(install_id, slug=…, title=…,
cover_url=…, genres=…)`` (version unknown, ``installed_at`` = folder mtime);
seed the image cache with the legacy cover for each entry's ``cover_url``.
Legacy files are left untouched so downgrading still works. A missing or
unreadable cache file means there is nothing to migrate (a cache written in
the ANSI code page instead of UTF-8 is still read); the guard is set after
every completed run — not when the library scan itself failed, so the
migration is retried on the next start. Returns a report.
"""

from __future__ import annotations

import json
import locale
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anker_client.core.db import Database
from anker_client.core.models import InstalledGame
from anker_client.core.paths import AppPaths, legacy_cover_name
from anker_client.services.images import ImageCache
from anker_client.services.library import LibraryService

log = logging.getLogger(__name__)

_META_KEY = "legacy_migrated"
_COVER_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


@dataclass(slots=True)
class MigrationReport:
    adopted: list[str] = field(default_factory=list)
    covers_imported: int = 0
    skipped: list[str] = field(default_factory=list)
    already_done: bool = False


@dataclass(frozen=True, slots=True)
class _LegacyEntry:
    folder: str
    title: str
    slug: str
    cover_url: str
    genres: tuple[str, ...]


def migrate(paths: AppPaths, db: Database, library: LibraryService, images: ImageCache) -> MigrationReport:
    if db.get_meta(_META_KEY) == "1":
        return MigrationReport(already_done=True)
    report = MigrationReport()
    entries = _load_entries(paths.legacy_library_cache_file)
    if entries:
        log.info("Migrating %d entr(ies) from the legacy library cache", len(entries))
        _import_covers(paths.legacy_covers_dir, entries, images, report)
        if not _adopt_folders(entries, library, report):
            return report  # the library could not be read: try again next start
    db.set_meta(_META_KEY, "1")
    if entries:
        log.info(
            "Legacy migration finished: %d adopted, %d skipped, %d cover(s) imported",
            len(report.adopted), len(report.skipped), report.covers_imported,
        )
    return report


def _load_entries(cache_file: Path) -> list[_LegacyEntry]:
    if not cache_file.is_file():
        return []
    try:
        data = json.loads(_decode(cache_file.read_bytes()))
    except (OSError, ValueError) as exc:
        log.warning("Legacy library cache %s is unreadable: %s", cache_file, exc)
        return []
    if not isinstance(data, dict):
        log.warning("Legacy library cache %s has an unexpected format", cache_file)
        return []
    entries: list[_LegacyEntry] = []
    for folder, raw in data.items():
        if not isinstance(folder, str) or not folder.strip() or not isinstance(raw, dict):
            continue
        entries.append(
            _LegacyEntry(
                folder=folder,
                title=_text(raw.get("title")) or folder,
                slug=_text(raw.get("slug")),
                cover_url=_text(raw.get("cover_url")),
                genres=_genres(raw.get("genres")),
            )
        )
    return entries


def _decode(raw: bytes) -> str:
    """UTF-8 (with or without BOM); else the ANSI code page a plain ``open(..., "w")`` used on Windows."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        encoding = locale.getencoding() or "cp1252"  # the ANSI code page, even in UTF-8 mode
        log.info("Legacy library cache is not UTF-8; reading it as %s", encoding)
        return raw.decode(encoding, errors="replace")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _genres(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _import_covers(covers_dir: Path, entries: list[_LegacyEntry], images: ImageCache, report: MigrationReport) -> None:
    if not covers_dir.is_dir():
        return
    for entry in entries:
        if not entry.cover_url:
            continue
        source = _legacy_cover(covers_dir, entry)
        if source is None:
            continue
        try:
            if images.import_file(entry.cover_url, source) is not None:
                report.covers_imported += 1
        except Exception:
            log.warning("Could not import legacy cover %s", source, exc_info=True)


def _legacy_cover(covers_dir: Path, entry: _LegacyEntry) -> Path | None:
    stems = []
    for name in (entry.folder, entry.title):
        stem = legacy_cover_name(name)
        if stem and stem not in stems:
            stems.append(stem)
    for stem in stems:
        for extension in _COVER_EXTENSIONS:
            candidate = covers_dir / f"{stem}{extension}"
            if candidate.is_file():
                return candidate
    return None


def _adopt_folders(entries: list[_LegacyEntry], library: LibraryService, report: MigrationReport) -> bool:
    """Adopt the legacy installs; False when the library could not be scanned (nothing was tried)."""
    try:
        games = library.scan()
    except Exception:
        log.exception("Library scan failed during the legacy migration; it will be retried")
        report.skipped.extend(entry.folder for entry in entries)
        return False
    by_folder: dict[str, list[InstalledGame]] = {}
    for game in games:
        by_folder.setdefault(game.folder_name.casefold(), []).append(game)

    for entry in entries:
        candidates = [g for g in by_folder.get(entry.folder.casefold(), []) if not g.managed]
        if not entry.slug or not candidates:
            reason = "no slug" if not entry.slug else "folder not found or already managed"
            log.debug("Legacy entry %r skipped (%s)", entry.folder, reason)
            report.skipped.append(entry.folder)
            continue
        for game in candidates:  # the same folder name may exist in several library roots
            try:
                adopted = library.adopt(
                    game.install_id,
                    slug=entry.slug,
                    title=entry.title,
                    cover_url=entry.cover_url,
                    genres=list(entry.genres),
                )
            except Exception:
                log.warning("Could not adopt legacy install %s", game.path, exc_info=True)
                report.skipped.append(entry.folder)
                continue
            report.adopted.append(adopted.install_id)
    return True

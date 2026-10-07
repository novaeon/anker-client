"""Shared fakes/helpers for the library work-package tests (this module contains no tests).

The install package (manifest I/O, executable detection) and the catalog are
implemented in parallel, so the tests replace them with small, deterministic
fakes that honour their documented contracts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from anker_client.constants import MANIFEST_FILENAME, REDIST_DIRNAMES
from anker_client.core.db import Database
from anker_client.core.events import Event, EventBus
from anker_client.core.models import GameSummary, InstallManifest
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.install import executables as executables_module
from anker_client.services.install import installer as installer_module
from anker_client.services.library import LibraryService

# --- install package fakes ---------------------------------------------------------------


def fake_read_manifest(install_dir: str) -> InstallManifest | None:
    path = Path(install_dir) / MANIFEST_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return InstallManifest.from_dict(data) if isinstance(data, dict) else None


def fake_write_manifest(install_dir: str, manifest: InstallManifest) -> None:
    path = Path(install_dir) / MANIFEST_FILENAME
    path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")


def fake_directory_size(path: str, *, token: CancelToken | None = None) -> int:
    total = 0
    for current, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))
                   and not os.path.isjunction(os.path.join(current, d))]
        for name in files:
            full = os.path.join(current, name)
            if not os.path.islink(full):
                total += os.path.getsize(full)
    return total


def fake_find_redist_dirs(install_dir: str) -> list[str]:
    found: list[str] = []
    root = Path(install_dir)
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_dir() and child.name.casefold() in REDIST_DIRNAMES:
            found.append(child.name)
    return found


def fake_executable_candidates(install_dir: str, title: str) -> list[tuple[str, int]]:
    root = Path(install_dir)
    exes = sorted(p.relative_to(root) for p in root.rglob("*.exe"))
    return [(str(p), 100 - index) for index, p in enumerate(exes)]


def fake_find_executable(install_dir: str, title: str) -> str:
    top_level = sorted(p.name for p in Path(install_dir).glob("*.exe"))
    return top_level[0] if len(top_level) == 1 else ""


def patch_install_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_module, "read_manifest", fake_read_manifest)
    monkeypatch.setattr(installer_module, "write_manifest", fake_write_manifest)
    monkeypatch.setattr(installer_module, "directory_size", fake_directory_size)
    monkeypatch.setattr(installer_module, "find_redist_dirs", fake_find_redist_dirs)
    monkeypatch.setattr(executables_module, "executable_candidates", fake_executable_candidates)
    monkeypatch.setattr(executables_module, "find_executable", fake_find_executable)


# --- other collaborators ------------------------------------------------------------------------


class FakeShortcuts:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.fail = False

    def create(self, title: str, target_exe: str, *, arguments: str = "", desktop: bool = True,
               start_menu: bool = True) -> list[str]:
        return []

    def remove(self, title: str) -> None:
        self.removed.append(title)
        if self.fail:
            raise RuntimeError("shortcut removal failed")

    def exists(self, title: str) -> dict[str, bool]:
        return {"desktop": False, "start_menu": False}


class FakeCatalog:
    """``match_title``/``get`` over a fixed set of summaries."""

    def __init__(self, games: list[GameSummary] | None = None, *, broken: bool = False) -> None:
        self.games = {g.slug: g for g in games or []}
        self.broken = broken
        self.match_calls: list[str] = []

    def match_title(self, name: str) -> GameSummary | None:
        self.match_calls.append(name)
        if self.broken:
            raise NotImplementedError
        wanted = name.casefold()
        for game in self.games.values():
            if game.title.casefold() == wanted:
                return game.copy()
        return None

    def get(self, slug: str) -> GameSummary | None:
        if self.broken:
            raise NotImplementedError
        game = self.games.get(slug)
        return game.copy() if game else None


class EventRecorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        bus.subscribe(Event, self._on_event)

    def _on_event(self, event: Event) -> None:
        with self._condition:
            self.events.append(event)
            self._condition.notify_all()

    def of(self, kind: type) -> list[Any]:
        with self._lock:
            return [e for e in self.events if isinstance(e, kind)]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def wait_for(self, kind: type, *, count: int = 1, timeout: float = 10.0) -> list[Any]:
        with self._condition:
            self._condition.wait_for(lambda: len([e for e in self.events if isinstance(e, kind)]) >= count,
                                     timeout=timeout)
        return self.of(kind)


# --- environment ---------------------------------------------------------------------------------


@dataclass
class LibraryEnv:
    tmp: Path
    roots: list[Path]
    db: Database
    events: EventBus
    settings: SettingsStore
    shortcuts: FakeShortcuts
    catalog: FakeCatalog | None
    recorder: EventRecorder
    library: LibraryService
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.roots[0]

    def rows(self) -> dict[str, dict[str, Any]]:
        return {row["install_id"]: dict(row) for row in self.db.query("SELECT * FROM installs")}

    def sessions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.query("SELECT * FROM play_sessions ORDER BY id")]


def make_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root_count: int = 1,
    catalog: FakeCatalog | None = None,
    **settings_changes: Any,
) -> LibraryEnv:
    patch_install_package(monkeypatch)
    roots = [tmp_path / f"Games{i or ''}" for i in range(root_count)]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    events = EventBus()
    settings = SettingsStore(tmp_path / "config" / "config.json", events)
    settings.update(library_dirs=[str(r) for r in roots], default_library=str(roots[0]), **settings_changes)
    db = Database(tmp_path / "config" / "anker.db")
    shortcuts = FakeShortcuts()
    recorder = EventRecorder(events)
    library = LibraryService(db, settings, events, shortcuts, catalog, delete_retry_delay=0.02)  # type: ignore[arg-type]
    return LibraryEnv(tmp_path, roots, db, events, settings, shortcuts, catalog, recorder, library)


def make_game_dir(root: Path, name: str, *, manifest: InstallManifest | None = None,
                  files: dict[str, bytes] | None = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {"game.exe": b"MZ" + b"\0" * 64}).items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if manifest is not None:
        fake_write_manifest(str(folder), manifest)
    return folder


def set_windows_attributes(path: Path, attributes: int) -> None:
    import ctypes

    if not ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes):
        raise OSError(f"SetFileAttributesW failed for {path}")

"""LibraryService against the real install-package helpers (manifest I/O, sizes, executable detection)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.db import Database
from anker_client.core.events import EventBus
from anker_client.core.models import DownloadOption, InstallManifest, InstallRequest, InstallResult
from anker_client.core.settings import SettingsStore
from anker_client.services.install import installer
from anker_client.services.library import LibraryService
from tests.unit.test_library_support import FakeShortcuts


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[LibraryService, Path, Database]]:
    root = tmp_path / "Games"
    root.mkdir()
    events = EventBus()
    settings = SettingsStore(tmp_path / "config.json", events)
    settings.update(library_dirs=[str(root)], default_library=str(root))
    db = Database(tmp_path / "anker.db")
    library = LibraryService(db, settings, events, FakeShortcuts(), None, delete_retry_delay=0.02)  # type: ignore[arg-type]
    yield library, root, db
    db.close()


def _write(path: Path, data: bytes = b"MZ" + b"\0" * 2048) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_unmanaged_folder_round_trip(setup: tuple[LibraryService, Path, Database]) -> None:
    library, root, _db = setup
    folder = root / "Hollow Knight"
    _write(folder / "hollow_knight.exe")
    _write(folder / "hollow_knight_Data" / "level0", b"x" * 1000)
    _write(folder / "_CommonRedist" / "vcredist_x64.exe")

    library.scan()
    game = library.get("local:hollow knight")
    assert game is not None and not game.managed and game.executable == "hollow_knight.exe"

    adopted = library.adopt("local:hollow knight", slug="hollow-knight", title="Hollow Knight")
    manifest = installer.read_manifest(str(folder))
    assert manifest is not None and manifest.slug == "hollow-knight" and manifest.has_redist
    assert adopted.install_id == "hollow-knight" and adopted.executable == "hollow_knight.exe"

    library.set_launch_options("hollow-knight", args="-windowed", run_as_admin=False)
    assert installer.read_manifest(str(folder)).launch_args == "-windowed"  # type: ignore[union-attr]

    size = library.compute_size("hollow-knight")
    assert size == sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())

    library.scan()
    assert [g.install_id for g in library.games()] == ["hollow-knight"]

    library.uninstall("hollow-knight")  # the real manifest is hidden: it must still be deleted
    assert not folder.exists() and root.exists()


def test_register_install_with_real_manifest(setup: tuple[LibraryService, Path, Database]) -> None:
    library, root, _db = setup
    folder = root / "Portal 2"
    _write(folder / "portal2.exe")
    installer.write_manifest(str(folder), InstallManifest(slug="portal-2", title="Portal 2", version="v2",
                                                          executable="portal2.exe"))
    if os.name == "nt":
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(folder / MANIFEST_FILENAME))
        assert attributes & 0x2  # hidden, as the installer writes it

    request = InstallRequest(archive_path="a.7z", slug="portal-2", title="Portal 2",
                             option=DownloadOption(1, "Direct"), library_root=str(root), version="v2")
    game = library.register_install(InstallResult(install_path=str(folder), executable="portal2.exe",
                                                  size_bytes=2050), request)
    assert (game.install_id, game.version, game.size_bytes, game.executable) == ("portal-2", "v2", 2050,
                                                                                 "portal2.exe")
    library.rename("portal-2", "Portal Two")
    assert installer.read_manifest(str(folder)).title == "Portal Two"  # type: ignore[union-attr]

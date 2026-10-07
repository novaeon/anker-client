"""Legacy (pre-1.0) library migration."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from anker_client.core.models import InstallManifest
from anker_client.core.paths import AppPaths
from anker_client.services.legacy import MigrationReport, migrate
from tests.unit.test_library_support import LibraryEnv, fake_read_manifest, make_env, make_game_dir


class FakeImages:
    def __init__(self) -> None:
        self.imported: list[tuple[str, Path]] = []
        self.fail_for: set[str] = set()

    def import_file(self, url: str, source: Path) -> Path | None:
        if url in self.fail_for:
            raise OSError("disk full")
        self.imported.append((url, Path(source)))
        return Path("cache") / "x.png"


LEGACY_CACHE = {
    "Horripilant": {
        "title": "Horripilant",
        "slug": "horripilant",
        "cover_url": "https://ankergames.net/uploads/horripilant.jpg",
        "genres": ["Horror", "Indie"],
        "size_gb": 1.2,
        "release_date": "2024-01-01",
        "description": "Scary.",
        "screenshots": [],
        "file_size": "1.2 GB",
    },
    "ASSASSINS CREED: Origins?": {  # odd characters exercise legacy_cover_name
        "title": "Assassin's Creed Origins",
        "slug": "assassins-creed-origins",
        "cover_url": "https://ankergames.net/uploads/aco.jpg",
        "genres": "Action, RPG",
    },
    "No Slug Game": {"title": "No Slug Game", "slug": "", "cover_url": ""},
    "Already Managed": {"title": "Already Managed", "slug": "already-managed", "cover_url": ""},
    "Missing Folder": {"title": "Missing Folder", "slug": "missing-folder",
                       "cover_url": "https://ankergames.net/uploads/missing.jpg"},
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryEnv]:
    environment = make_env(tmp_path, monkeypatch)
    paths = AppPaths.under(tmp_path / "home").ensure()
    environment.extra["paths"] = paths
    yield environment
    environment.db.close()


def _write_legacy(paths: AppPaths, data: object = LEGACY_CACHE, *, covers: tuple[str, ...] = ()) -> None:
    paths.legacy_library_cache_file.write_text(json.dumps(data), encoding="utf-8")
    paths.legacy_covers_dir.mkdir(parents=True, exist_ok=True)
    for name in covers:
        (paths.legacy_covers_dir / name).write_bytes(b"\x89PNG fake")


def _populate_library(env: LibraryEnv) -> dict[str, Path]:
    folders = {
        "horripilant": make_game_dir(env.root, "horripilant"),  # case differs from the cache key
        "aco": make_game_dir(env.root, "ASSASSINS CREED Origins"),
        "noslug": make_game_dir(env.root, "No Slug Game"),
        "managed": make_game_dir(env.root, "Already Managed",
                                 manifest=InstallManifest(slug="already-managed", title="Already Managed",
                                                          version="v9")),
        "other": make_game_dir(env.root, "Unrelated Folder"),
    }
    os.utime(folders["horripilant"], (1_700_000_000, 1_700_000_000))
    return folders


def test_migrate_adopts_legacy_installs(env: LibraryEnv) -> None:
    paths: AppPaths = env.extra["paths"]
    folders = _populate_library(env)
    # The cache key of ACO contains characters that are invalid in folder names; only exact
    # (case-insensitive) folder names are matched, so give it a matching key too.
    data = dict(LEGACY_CACHE)
    data["ASSASSINS CREED Origins"] = data.pop("ASSASSINS CREED: Origins?")
    _write_legacy(paths, data, covers=("Horripilant.png", "Assassin s Creed Origins.png"))
    images = FakeImages()

    report = migrate(paths, env.db, env.library, images)  # type: ignore[arg-type]

    assert sorted(report.adopted) == ["assassins-creed-origins", "horripilant"]
    assert sorted(report.skipped) == ["Already Managed", "Missing Folder", "No Slug Game"]
    assert not report.already_done
    assert report.covers_imported == 2
    assert sorted(url for url, _ in images.imported) == ["https://ankergames.net/uploads/aco.jpg",
                                                         "https://ankergames.net/uploads/horripilant.jpg"]

    manifest = fake_read_manifest(str(folders["horripilant"]))
    assert manifest is not None
    assert (manifest.slug, manifest.title, manifest.version) == ("horripilant", "Horripilant", "")
    assert manifest.cover_url == "https://ankergames.net/uploads/horripilant.jpg"
    assert manifest.genres == ["Horror", "Indie"]
    assert manifest.installed_at == "2023-11-14T22:13:20+00:00"  # folder mtime
    aco = env.library.get("assassins-creed-origins")
    assert aco is not None and aco.managed and aco.genres == ["Action", "RPG"]
    assert aco.title == "Assassin's Creed Origins"

    # Untouched: the managed game keeps its manifest, unknown folders stay unmanaged.
    assert fake_read_manifest(str(folders["managed"])).version == "v9"  # type: ignore[union-attr]
    assert fake_read_manifest(str(folders["noslug"])) is None
    assert env.library.get("local:unrelated folder") is not None
    assert env.db.get_meta("legacy_migrated") == "1"
    # Legacy files are left in place.
    assert paths.legacy_library_cache_file.exists()
    assert (paths.legacy_covers_dir / "Horripilant.png").exists()


def test_migrate_is_idempotent(env: LibraryEnv) -> None:
    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    _write_legacy(paths)
    first = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert first.adopted == ["horripilant"]

    images = FakeImages()
    second = migrate(paths, env.db, env.library, images)  # type: ignore[arg-type]
    assert second == MigrationReport(already_done=True)
    assert images.imported == []

    # Even without the guard, adopted folders are managed now and are not adopted twice.
    env.db.set_meta("legacy_migrated", None)
    third = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert third.adopted == []


def test_migrate_without_legacy_files(env: LibraryEnv) -> None:
    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    report = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert report == MigrationReport()
    assert env.db.get_meta("legacy_migrated") == "1"


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", '{"Folder": "not a dict", "": {}}'])
def test_migrate_tolerates_malformed_cache(env: LibraryEnv, content: str) -> None:
    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    paths.legacy_library_cache_file.write_text(content, encoding="utf-8")
    report = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert report.adopted == []
    assert env.db.get_meta("legacy_migrated") == "1"


def test_cover_import_failures_are_ignored(env: LibraryEnv) -> None:
    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    _write_legacy(paths, covers=("Horripilant.png",))
    images = FakeImages()
    images.fail_for.add("https://ankergames.net/uploads/horripilant.jpg")
    report = migrate(paths, env.db, env.library, images)  # type: ignore[arg-type]
    assert report.covers_imported == 0
    assert report.adopted == ["horripilant"]


def test_failed_library_scan_is_retried_next_start(env: LibraryEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    _write_legacy(paths)
    real_scan = env.library.scan

    def broken_scan(**kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(env.library, "scan", broken_scan)
    first = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert first.adopted == [] and not first.already_done
    assert env.db.get_meta("legacy_migrated") is None  # not marked done: nothing was tried

    monkeypatch.setattr(env.library, "scan", real_scan)
    second = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert second.adopted == ["horripilant"]
    assert env.db.get_meta("legacy_migrated") == "1"


def test_cache_written_in_the_ansi_code_page(env: LibraryEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    import locale

    paths: AppPaths = env.extra["paths"]
    folder = make_game_dir(env.root, "Café Racer")
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    data = {"Café Racer": {"title": "Café Racer™", "slug": "cafe-racer", "cover_url": ""}}
    paths.legacy_library_cache_file.write_bytes(json.dumps(data, ensure_ascii=False).encode("cp1252"))

    report = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]

    assert report.adopted == ["cafe-racer"]
    assert fake_read_manifest(str(folder)).title == "Café Racer™"  # type: ignore[union-attr]


def test_utf8_cache_with_bom(env: LibraryEnv) -> None:
    paths: AppPaths = env.extra["paths"]
    make_game_dir(env.root, "Ünïcødé")
    data = {"Ünïcødé": {"title": "Ünïcødé", "slug": "unicode", "cover_url": ""}}
    paths.legacy_library_cache_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode())
    assert migrate(paths, env.db, env.library, FakeImages()).adopted == ["unicode"]  # type: ignore[arg-type]


def test_adopt_failure_is_reported_as_skipped(env: LibraryEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.core.errors import InstallError

    paths: AppPaths = env.extra["paths"]
    _populate_library(env)
    _write_legacy(paths)

    def broken_adopt(*args: object, **kwargs: object) -> None:
        raise InstallError("read-only folder")

    monkeypatch.setattr(env.library, "adopt", broken_adopt)
    report = migrate(paths, env.db, env.library, FakeImages())  # type: ignore[arg-type]
    assert report.adopted == []
    assert "Horripilant" in report.skipped

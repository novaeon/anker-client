"""Tests for the installer: staging, root detection, swap/rollback, overlays, manifests, cleanup."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from anker_client.constants import MANIFEST_FILENAME, START_MENU_FOLDER
from anker_client.core import paths
from anker_client.core.errors import (
    CorruptArchiveError,
    DiskSpaceError,
    InstallError,
    OperationCancelled,
)
from anker_client.core.models import DownloadKind, DownloadOption, InstallManifest, InstallRequest
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.install import _fsutil, diskspace, installer
from anker_client.services.install.extractor import Extractor
from anker_client.services.install.installer import (
    Installer,
    directory_size,
    find_redist_dirs,
    read_manifest,
    write_manifest,
)
from anker_client.services.install.sevenzip import SevenZip
from anker_client.services.install.shortcuts import ShortcutService

SEVEN_ZIP = SevenZip.locate()
needs_7z = pytest.mark.skipif(SEVEN_ZIP is None, reason="7-Zip is not installed")

TITLE = "Hollow Knight"
SLUG = "hollow-knight"
EXE_BYTES = b"\0" * 300_000  # zero-filled: valid shortcut target, compresses to nothing
GAME_FILES: dict[str, bytes] = {
    "Hollow Knight/hollow_knight.exe": EXE_BYTES,
    "Hollow Knight/hollow_knight_Data/level0": b"level zero",
    "Hollow Knight/hollow_knight_Data/Managed/Assembly-CSharp.dll": b"v1.5",
    "Hollow Knight/UnityCrashHandler64.exe": b"\0" * 2000,
    "Read Me.txt": b"junk",
    "Run Me!.bat": b"junk",
    "AnkerGames.url": b"junk",
}


# ---------------------------------------------------------------------------
# helpers & fixtures
# ---------------------------------------------------------------------------


class FakeShortcuts:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.fail = False

    def create(self, title: str, target_exe: str, *, arguments: str = "", desktop: bool = True,
               start_menu: bool = True) -> list[str]:
        self.created.append({"title": title, "target": target_exe, "arguments": arguments,
                             "desktop": desktop, "start_menu": start_menu})
        if self.fail:
            raise InstallError("Some shortcuts could not be created.")
        return []

    def remove(self, title: str) -> None:
        pass

    def exists(self, title: str) -> dict[str, bool]:
        return {"desktop": False, "start_menu": False}


def make_zip(path: Path, files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def full_option(label: str = "Direct", version: str = "") -> DownloadOption:
    return DownloadOption(download_id=1, label=label, kind=DownloadKind.FULL, to_version=version)


def make_request(archive: Path, library: Path, *, option: DownloadOption | None = None, slug: str = SLUG,
                 title: str = TITLE, existing: str = "", version: str = "1.5", **extra: Any) -> InstallRequest:
    return InstallRequest(
        archive_path=str(archive), slug=slug, title=title, option=option or full_option(), library_root=str(library),
        version=version, existing_install_path=existing, **extra,
    )


@pytest.fixture
def library(tmp_path: Path) -> Path:
    return tmp_path / "Games"


@pytest.fixture
def settings(tmp_path: Path, library: Path) -> SettingsStore:
    store = SettingsStore(tmp_path / "config.json")
    store.update(library_dirs=[str(library)], default_library=str(library))
    return store


@pytest.fixture
def shortcuts() -> FakeShortcuts:
    return FakeShortcuts()


@pytest.fixture(params=[pytest.param("7z", marks=needs_7z), "builtin"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "builtin":
        monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))
    return str(request.param)


@pytest.fixture
def inst(request: pytest.FixtureRequest, settings: SettingsStore, shortcuts: FakeShortcuts,
         monkeypatch: pytest.MonkeyPatch) -> Installer:
    """Installer with the in-process zip backend unless the test asks for ``backend`` (7z and builtin)."""
    if "backend" not in request.fixturenames:
        monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))
    return Installer(settings, Extractor(lambda: ""), shortcuts)  # type: ignore[arg-type]


@pytest.fixture
def builtin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))


def staging_entries(root: Path) -> list[Path]:
    staging = root / ".ankerclient" / "staging"
    return list(staging.iterdir()) if staging.exists() else []


def backups(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if installer.BACKUP_INFIX in p.name] if root.exists() else []


def install_base(inst: Installer, tmp_path: Path, library: Path, files: dict[str, bytes] | None = None,
                 **kwargs: Any) -> Path:
    archive = make_zip(tmp_path / "dl" / "base.zip", files or GAME_FILES)
    result = inst.install(make_request(archive, library, **kwargs), token=CancelToken())
    return Path(result.install_path)


# ---------------------------------------------------------------------------
# FULL installs
# ---------------------------------------------------------------------------


def test_fresh_install(inst: Installer, tmp_path: Path, library: Path, shortcuts: FakeShortcuts,
                       backend: str) -> None:
    archive = make_zip(tmp_path / "dl" / "Hollow-Knight-AnkerGames.zip", GAME_FILES)
    progress: list[tuple[str, float]] = []
    request = make_request(archive, library, cover_url="https://x/cover.jpg", genres=["Action"],
                           source_updated_date="2026-09-01")
    result = inst.install(request, token=CancelToken(), on_progress=lambda p, f: progress.append((p, f)))

    dest = library / TITLE
    assert result.install_path == str(dest)
    assert result.executable == "hollow_knight.exe"
    assert result.executable_candidates == ["hollow_knight.exe"]
    assert result.has_redist is False
    payload = sum(len(v) for k, v in GAME_FILES.items() if k.startswith("Hollow Knight/"))
    assert result.size_bytes == payload
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"level zero"
    assert {p.name for p in dest.iterdir()} == {
        MANIFEST_FILENAME, "hollow_knight.exe", "hollow_knight_Data", "UnityCrashHandler64.exe"
    }

    manifest = read_manifest(str(dest))
    assert manifest is not None
    assert (manifest.slug, manifest.title, manifest.version) == (SLUG, TITLE, "1.5")
    assert manifest.executable == "hollow_knight.exe"
    assert manifest.applied_options == ["Direct"]
    assert manifest.installed_at and manifest.updated_at == manifest.installed_at
    assert manifest.cover_url == "https://x/cover.jpg" and manifest.genres == ["Action"]
    assert manifest.source_updated_date == "2026-09-01"
    if os.name == "nt":
        assert _fsutil.is_hidden(str(dest / MANIFEST_FILENAME))
        assert _fsutil.is_hidden(str(library / ".ankerclient"))

    assert staging_entries(library) == []
    assert not archive.exists(), "archive deleted by default"
    assert shortcuts.created == [{"title": TITLE, "target": str(dest / "hollow_knight.exe"), "arguments": "",
                                  "desktop": True, "start_menu": True}]

    phases = [p for p, _ in progress]
    assert phases.index("installing") > max(i for i, p in enumerate(phases) if p == "extracting")
    for phase in ("extracting", "installing"):
        values = [f for p, f in progress if p == phase]
        assert values[0] == 0.0 and values[-1] == 1.0
        assert values == sorted(values) and len(set(values)) == len(values)


def test_flat_archive(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "flat.zip", {"Game.exe": EXE_BYTES, "data/a.pak": b"a", "Read Me.txt": b"x"})
    result = inst.install(make_request(archive, library, title="Flat Game", slug="flat"), token=CancelToken())
    dest = library / "Flat Game"
    assert result.install_path == str(dest)
    assert {p.name for p in dest.iterdir()} == {MANIFEST_FILENAME, "data", "Game.exe"}
    assert result.executable == "Game.exe"


def test_deep_single_folder_chain(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "deep.zip", {
        "Outer/__MACOSX/._x": b"",
        "Outer/AnkerGames.url": b"",
        "Outer/Inner/Game.exe": EXE_BYTES,
        "Outer/Inner/Game_Data/x": b"x",
    })
    result = inst.install(make_request(archive, library, title="Deep", slug="deep"), token=CancelToken())
    assert {p.name for p in Path(result.install_path).iterdir()} == {MANIFEST_FILENAME, "Game.exe", "Game_Data"}


def test_game_folder_plus_redist_folder(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "multi.zip", {
        "Game/Game.exe": EXE_BYTES,
        "Game/Game_Data/x": b"x",
        "_CommonRedist/vcredist/2019/VC_redist.x64.exe": b"\0" * 100,
        "Read Me.txt": b"junk",
    })
    result = inst.install(make_request(archive, library, title="Game", slug="game"), token=CancelToken())
    dest = Path(result.install_path)
    assert (dest / "Game.exe").exists()
    assert (dest / "_CommonRedist" / "vcredist" / "2019" / "VC_redist.x64.exe").exists()
    assert result.executable == "Game.exe"
    assert result.has_redist is True
    manifest = read_manifest(str(dest))
    assert manifest is not None and manifest.has_redist and not manifest.redist_installed


def test_sibling_content_folders_are_never_dropped(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "multi.zip", {
        "Game/Game.exe": EXE_BYTES,
        "Game/Game_Data/x": b"x",
        "_CommonRedist/vcredist/2019/VC_redist.x64.exe": b"\0" * 100,
        "Soundtrack/01.mp3": b"music",
        "Read Me.txt": b"junk",
    })
    result = inst.install(make_request(archive, library, title="Game", slug="game"), token=CancelToken())
    dest = Path(result.install_path)
    assert (dest / "Soundtrack" / "01.mp3").read_bytes() == b"music"
    assert (dest / "_CommonRedist").is_dir()
    assert result.executable == os.path.join("Game", "Game.exe")
    assert not (dest / "Read Me.txt").exists()


def test_old_unreal_layout_keeps_data_folders(inst: Installer, tmp_path: Path, library: Path) -> None:
    # Deus Ex / Unreal Tournament style: only System holds an .exe, the game needs every folder.
    archive = make_zip(tmp_path / "ut.zip", {
        "Unreal Tournament/System/UnrealTournament.exe": EXE_BYTES,
        "Unreal Tournament/Maps/DM-Deck16][.unr": b"map",
        "Unreal Tournament/Textures/Generic.utx": b"tex",
        "Unreal Tournament/Music/Godown.umx": b"music",
    })
    result = inst.install(make_request(archive, library, title="Unreal Tournament", slug="ut"), token=CancelToken())
    dest = Path(result.install_path)
    assert {p.name for p in dest.iterdir()} == {MANIFEST_FILENAME, "System", "Maps", "Textures", "Music"}
    assert result.executable == os.path.join("System", "UnrealTournament.exe")


def test_flat_bin_and_data_folders_kept_together(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "bin.zip", {"bin/game.exe": EXE_BYTES, "data/level.pak": b"x"})
    result = inst.install(make_request(archive, library, title="Binned", slug="binned"), token=CancelToken())
    dest = Path(result.install_path)
    assert (dest / "bin" / "game.exe").exists() and (dest / "data" / "level.pak").exists()
    assert result.executable == os.path.join("bin", "game.exe")


def test_name_collision_with_foreign_folders(inst: Installer, tmp_path: Path, library: Path) -> None:
    (library / TITLE).mkdir(parents=True)
    (library / TITLE / "unrelated.txt").write_text("keep me")
    other = library / f"{TITLE} (2)"
    other.mkdir()
    write_manifest(str(other), InstallManifest(slug="some-other-game", title="Other"))

    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    result = inst.install(make_request(archive, library), token=CancelToken())
    assert result.install_path == str(library / f"{TITLE} (3)")
    assert (library / TITLE / "unrelated.txt").read_text() == "keep me"
    assert read_manifest(str(other)).slug == "some-other-game"  # type: ignore[union-attr]


def test_same_slug_reinstall_preserves_launch_settings(inst: Installer, tmp_path: Path, library: Path,
                                                        shortcuts: FakeShortcuts) -> None:
    dest = install_base(inst, tmp_path, library)
    manifest = read_manifest(str(dest))
    assert manifest is not None
    manifest.launch_args = "-windowed"
    manifest.run_as_admin = True
    write_manifest(str(dest), manifest)
    (dest / "stale_file.txt").write_text("from the old version")

    files = dict(GAME_FILES)
    files["Hollow Knight/hollow_knight_Data/Managed/Assembly-CSharp.dll"] = b"v1.6"
    archive = make_zip(tmp_path / "dl" / "v16.zip", files)
    result = inst.install(make_request(archive, library, version="1.6"), token=CancelToken())

    assert result.install_path == str(dest)
    assert not (library / f"{TITLE} (2)").exists()
    assert not (dest / "stale_file.txt").exists(), "a FULL install replaces the folder"
    assert (dest / "hollow_knight_Data" / "Managed" / "Assembly-CSharp.dll").read_bytes() == b"v1.6"
    new_manifest = read_manifest(str(dest))
    assert new_manifest is not None
    assert (new_manifest.executable, new_manifest.launch_args, new_manifest.run_as_admin) == (
        "hollow_knight.exe", "-windowed", True)
    assert new_manifest.version == "1.6"
    assert shortcuts.created[-1]["arguments"] == "-windowed"
    assert backups(library) == []
    assert staging_entries(library) == []


def test_reinstall_with_vanished_executable_redetects(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    manifest = read_manifest(str(dest))
    assert manifest is not None
    manifest.executable = os.path.join("tools", "custom.exe")
    manifest.launch_args = "-custom"
    write_manifest(str(dest), manifest)
    archive = make_zip(tmp_path / "dl" / "again.zip", GAME_FILES)
    result = inst.install(make_request(archive, library), token=CancelToken())
    new_manifest = read_manifest(result.install_path)
    assert new_manifest is not None
    assert (new_manifest.executable, new_manifest.launch_args) == ("hollow_knight.exe", "")


def test_reinstall_keeps_redist_installed_flag(inst: Installer, tmp_path: Path, library: Path) -> None:
    files = {**GAME_FILES, "Hollow Knight/_CommonRedist/DirectX/DXSETUP.exe": b"\0"}
    dest = install_base(inst, tmp_path, library, files)
    manifest = read_manifest(str(dest))
    assert manifest is not None and manifest.has_redist
    manifest.redist_installed = True
    write_manifest(str(dest), manifest)
    archive = make_zip(tmp_path / "dl" / "again.zip", files)
    inst.install(make_request(archive, library), token=CancelToken())
    assert read_manifest(str(dest)).redist_installed is True  # type: ignore[union-attr]


def test_existing_install_path_is_the_destination(inst: Installer, tmp_path: Path, library: Path) -> None:
    custom = library / "HK (custom folder)"
    custom.mkdir(parents=True)
    write_manifest(str(custom), InstallManifest(slug=SLUG, title=TITLE, executable="hollow_knight.exe",
                                                launch_args="-x"))
    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    result = inst.install(make_request(archive, library, existing=str(custom)), token=CancelToken())
    assert result.install_path == str(custom)
    assert not (library / TITLE).exists()
    assert read_manifest(str(custom)).launch_args == "-x"  # type: ignore[union-attr]


def test_existing_install_path_must_not_be_a_library(inst: Installer, tmp_path: Path, library: Path) -> None:
    library.mkdir(parents=True)
    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    with pytest.raises(InstallError):
        inst.install(make_request(archive, library, existing=str(library)), token=CancelToken())
    assert archive.exists()


def test_rollback_when_final_move_fails(inst: Installer, tmp_path: Path, library: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library)
    (dest / "savegame.dat").write_text("precious")
    before = read_manifest(str(dest))

    real_replace = os.replace
    failures: list[tuple[str, str]] = []

    def replace_failing_once(src: Any, dst: Any) -> None:
        if not failures and "staging" in str(src) and os.path.normcase(str(dst)) == os.path.normcase(str(dest)):
            failures.append((str(src), str(dst)))
            raise OSError(errno.EIO, "simulated I/O failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_failing_once)
    archive = make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES)
    with pytest.raises(InstallError, match="could not be moved"):
        inst.install(make_request(archive, library, version="2.0"), token=CancelToken())

    assert len(failures) == 1
    assert (dest / "savegame.dat").read_text() == "precious", "old install restored"
    assert read_manifest(str(dest)) == before
    assert backups(library) == []
    assert staging_entries(library) == []
    assert archive.exists(), "archive kept when the install failed"


def test_locked_destination_fails_cleanly(inst: Installer, tmp_path: Path, library: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library)
    real_replace = os.replace

    def locked(src: Any, dst: Any) -> None:
        if os.path.normcase(str(src)) == os.path.normcase(str(dest)):
            raise PermissionError(errno.EACCES, "in use")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(_fsutil.time, "sleep", lambda _s: None)
    archive = make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES)
    with pytest.raises(InstallError, match="Close the game"):
        inst.install(make_request(archive, library), token=CancelToken())
    assert (dest / "hollow_knight.exe").exists()
    assert staging_entries(library) == []


def test_cancel_during_extraction(inst: Installer, tmp_path: Path, library: Path, builtin_only: None) -> None:
    files = {"Game/Game.exe": EXE_BYTES, "Game/big.bin": os.urandom(3 * 1024 * 1024)}
    archive = make_zip(tmp_path / "big.zip", files, compression=zipfile.ZIP_STORED)
    token = CancelToken()

    def on_progress(phase: str, fraction: float) -> None:
        if phase == "extracting" and fraction > 0:
            token.cancel()

    with pytest.raises(OperationCancelled):
        inst.install(make_request(archive, library, title="Game", slug="game"), token=token, on_progress=on_progress)
    assert not (library / "Game").exists()
    assert staging_entries(library) == []
    assert archive.exists()


def test_cancel_right_before_swap_keeps_old_install(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    (dest / "savegame.dat").write_text("precious")
    token = CancelToken()

    def on_progress(phase: str, fraction: float) -> None:
        if phase == "installing" and fraction >= 0.6:
            token.cancel()

    archive = make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES)
    with pytest.raises(OperationCancelled):
        inst.install(make_request(archive, library), token=token, on_progress=on_progress)
    assert (dest / "savegame.dat").exists()
    assert staging_entries(library) == [] and backups(library) == []


def test_html_page_instead_of_archive(inst: Installer, tmp_path: Path, library: Path, backend: str) -> None:
    archive = tmp_path / "Hollow-Knight-AnkerGames.zip"
    archive.write_text("<!DOCTYPE html><html><title>Just a moment...</title></html>")
    with pytest.raises(CorruptArchiveError, match="web page"):
        inst.install(make_request(archive, library), token=CancelToken())
    assert staging_entries(library) == []
    assert not (library / TITLE).exists()


def test_archive_with_only_junk(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "junk.zip", {"Read Me.txt": b"x", "AnkerGames.url": b"y", "Empty/": b""})
    with pytest.raises(InstallError, match="does not contain"):
        inst.install(make_request(archive, library), token=CancelToken())
    assert staging_entries(library) == []


def test_missing_archive(inst: Installer, tmp_path: Path, library: Path) -> None:
    with pytest.raises(InstallError, match="missing"):
        inst.install(make_request(tmp_path / "nope.zip", library), token=CancelToken())


def test_not_enough_space_for_zip(inst: Installer, tmp_path: Path, library: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diskspace, "free_bytes", lambda _path: 1000)
    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    with pytest.raises(DiskSpaceError) as info:
        inst.install(make_request(archive, library), token=CancelToken())
    assert info.value.available == 1000
    assert staging_entries(library) == []
    assert archive.exists()


@needs_7z
def test_real_7z_archive(settings: SettingsStore, shortcuts: FakeShortcuts, tmp_path: Path, library: Path) -> None:
    inst = Installer(settings, Extractor(lambda: SEVEN_ZIP or ""), shortcuts)  # type: ignore[arg-type]
    src = tmp_path / "src"
    for name, data in GAME_FILES.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    archive = tmp_path / "Hollow-Knight-AnkerGames.7z"
    subprocess.run([SEVEN_ZIP, "a", str(archive), "Hollow Knight", "Read Me.txt"], cwd=src, check=True,
                   capture_output=True, creationflags=_fsutil.CREATE_NO_WINDOW)
    result = inst.install(make_request(archive, library), token=CancelToken())
    assert result.executable == "hollow_knight.exe"
    assert not (Path(result.install_path) / "Read Me.txt").exists()


def test_long_paths_inside_archive(inst: Installer, tmp_path: Path, library: Path, backend: str) -> None:
    deep = "/".join(["very_long_directory_name_" + str(i).zfill(3) for i in range(10)])
    archive = make_zip(tmp_path / "long.zip", {"Game/Game.exe": EXE_BYTES, f"Game/{deep}/asset.bin": b"deep"})
    result = inst.install(make_request(archive, library, title="Long", slug="long"), token=CancelToken())
    assert len(os.path.join(result.install_path, deep)) > 260
    assert result.size_bytes == len(EXE_BYTES) + 4
    assert staging_entries(library) == []


# ---------------------------------------------------------------------------
# archive disposal & shortcuts
# ---------------------------------------------------------------------------


def test_keep_archive_and_setting(inst: Installer, settings: SettingsStore, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", GAME_FILES)
    inst.install(make_request(archive, library), token=CancelToken(), keep_archive=True)
    assert archive.exists()
    settings.update(delete_archive_after_install=False)
    inst.install(make_request(archive, library), token=CancelToken())
    assert archive.exists()


def test_download_job_folder_removed_with_archive(inst: Installer, library: Path) -> None:
    job_dir = library / ".ankerclient" / "downloads" / "job-123"
    archive = make_zip(job_dir / "Hollow-Knight-AnkerGames.zip", GAME_FILES)
    inst.install(make_request(archive, library), token=CancelToken())
    assert not job_dir.exists()
    assert (library / ".ankerclient" / "downloads").is_dir()


def test_no_shortcuts_when_executable_ambiguous(inst: Installer, tmp_path: Path, library: Path,
                                                shortcuts: FakeShortcuts) -> None:
    archive = make_zip(tmp_path / "amb.zip", {"Game/Alpha.exe": EXE_BYTES, "Game/Beta.exe": EXE_BYTES})
    result = inst.install(make_request(archive, library, title="Gamma", slug="gamma"), token=CancelToken())
    assert result.executable == ""
    assert sorted(result.executable_candidates) == ["Alpha.exe", "Beta.exe"]
    assert read_manifest(result.install_path).executable == ""  # type: ignore[union-attr]
    assert shortcuts.created == []


def test_shortcut_settings_respected(inst: Installer, settings: SettingsStore, tmp_path: Path, library: Path,
                                     shortcuts: FakeShortcuts) -> None:
    settings.update(create_desktop_shortcut=False, create_start_menu_shortcut=False)
    inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken())
    assert shortcuts.created == []
    settings.update(create_start_menu_shortcut=True)
    inst.install(make_request(make_zip(tmp_path / "b.zip", GAME_FILES), library), token=CancelToken())
    assert shortcuts.created[-1]["desktop"] is False and shortcuts.created[-1]["start_menu"] is True


def test_shortcut_failure_does_not_fail_install(inst: Installer, tmp_path: Path, library: Path,
                                                shortcuts: FakeShortcuts) -> None:
    shortcuts.fail = True
    result = inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken())
    assert Path(result.install_path, "hollow_knight.exe").exists()
    assert len(shortcuts.created) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcuts")
def test_real_shortcuts_integration(settings: SettingsStore, tmp_path: Path, library: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "desktop_dir", lambda: tmp_path / "Desktop")
    monkeypatch.setattr(paths, "start_menu_programs_dir", lambda: tmp_path / "Programs")
    real = Installer(settings, Extractor(lambda: ""), ShortcutService())
    real.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken())
    assert (tmp_path / "Desktop" / f"{TITLE}.lnk").exists()
    assert (tmp_path / "Programs" / START_MENU_FOLDER / f"{TITLE}.lnk").exists()


def test_broken_progress_callback_is_ignored(inst: Installer, tmp_path: Path, library: Path) -> None:
    def broken(_phase: str, _fraction: float) -> None:
        raise RuntimeError("UI went away")

    result = inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken(),
                          on_progress=broken)
    assert Path(result.install_path).is_dir()


# ---------------------------------------------------------------------------
# crash recovery & concurrency
# ---------------------------------------------------------------------------


def test_recovers_interrupted_swap_and_purges_stale_staging(inst: Installer, tmp_path: Path,
                                                            library: Path) -> None:
    missing_backup = library / f"Old Game{installer.BACKUP_INFIX}{'a' * 32}"
    missing_backup.mkdir(parents=True)
    (missing_backup / "game.exe").write_bytes(b"old")
    (library / "Present").mkdir()
    leftover_backup = library / f"Present{installer.BACKUP_INFIX}{'b' * 32}"
    leftover_backup.mkdir()
    (leftover_backup / "x").write_text("stale")
    stale = library / ".ankerclient" / "staging" / ("c" * 32) / "x"
    stale.mkdir(parents=True)
    (stale / "half.bin").write_bytes(b"half")
    not_a_backup = library / "Something.ankerclient-old-notahexuuid"
    not_a_backup.mkdir()

    inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken())

    assert (library / "Old Game" / "game.exe").read_bytes() == b"old"
    assert not missing_backup.exists()
    assert not leftover_backup.exists()
    assert (library / "Present").is_dir()
    assert staging_entries(library) == []
    assert not_a_backup.exists()


def test_concurrent_installs_do_not_purge_each_other(inst: Installer, tmp_path: Path, library: Path,
                                                     builtin_only: None) -> None:
    barrier = threading.Barrier(2, timeout=10)
    errors: list[BaseException] = []
    results: list[str] = []

    def run(name: str) -> None:
        archive = make_zip(tmp_path / f"{name}.zip", {f"{name}/{name}.exe": EXE_BYTES, f"{name}/d": b"d"})

        def on_progress(phase: str, fraction: float) -> None:
            if phase == "extracting" and fraction == 0.0:
                barrier.wait()  # both installs have created their staging folders

        try:
            result = inst.install(make_request(archive, library, title=name, slug=name.lower()),
                                  token=CancelToken(), on_progress=on_progress)
            results.append(result.install_path)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("Alpha", "Beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert sorted(results) == [str(library / "Alpha"), str(library / "Beta")]
    assert staging_entries(library) == []


# ---------------------------------------------------------------------------
# PATCH / ADDON
# ---------------------------------------------------------------------------


def patch_option(to_version: str = "1.6") -> DownloadOption:
    return DownloadOption(download_id=2, label=f"Update Only From V 1.5 To V {to_version}", kind=DownloadKind.PATCH,
                          from_version="1.5", to_version=to_version)


def addon_option(label: str = "Language Pack") -> DownloadOption:
    return DownloadOption(download_id=3, label=label, kind=DownloadKind.ADDON)


def test_patch_overlay_updates_files_and_manifest(inst: Installer, tmp_path: Path, library: Path,
                                                  backend: str) -> None:
    dest = install_base(inst, tmp_path, library)
    base_manifest = read_manifest(str(dest))
    assert base_manifest is not None
    patch = make_zip(tmp_path / "dl" / "patch.zip", {
        "Hollow Knight Update v1.6/hollow_knight_Data/Managed/Assembly-CSharp.dll": b"v1.6",
        "Hollow Knight Update v1.6/hollow_knight_Data/new_level": b"new",
        "Read Me.txt": b"junk",
    })
    request = make_request(patch, library, option=patch_option(), existing=str(dest), version="",
                           source_updated_date="2026-10-01")
    progress: list[tuple[str, float]] = []
    result = inst.install(request, token=CancelToken(), on_progress=lambda p, f: progress.append((p, f)))

    assert result.install_path == str(dest)
    assert (dest / "hollow_knight_Data" / "Managed" / "Assembly-CSharp.dll").read_bytes() == b"v1.6"
    assert (dest / "hollow_knight_Data" / "new_level").read_bytes() == b"new"
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"level zero"
    assert not (dest / "Read Me.txt").exists()
    assert not (dest / "Hollow Knight Update v1.6").exists()
    manifest = read_manifest(str(dest))
    assert manifest is not None
    assert manifest.applied_options == ["Direct", "Update Only From V 1.5 To V 1.6"]
    assert manifest.version == "1.6"
    assert manifest.source_updated_date == "2026-10-01"
    assert manifest.updated_at >= base_manifest.installed_at
    assert manifest.installed_at == base_manifest.installed_at
    assert manifest.executable == "hollow_knight.exe"
    assert result.executable == "hollow_knight.exe"
    assert not patch.exists()
    assert staging_entries(library) == []
    installing = [f for p, f in progress if p == "installing"]
    assert installing[-1] == 1.0 and installing == sorted(installing)


def test_patch_without_version_uses_request_version(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    patch = make_zip(tmp_path / "p.zip", {"hollow_knight_Data/level0": b"patched"})
    option = DownloadOption(download_id=9, label="Update Only", kind=DownloadKind.PATCH)
    inst.install(make_request(patch, library, option=option, existing=str(dest), version="1.7"),
                 token=CancelToken())
    assert read_manifest(str(dest)).version == "1.7"  # type: ignore[union-attr]
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"patched"


def test_unreal_patch_aligned_without_wrapper(inst: Installer, tmp_path: Path, library: Path) -> None:
    base_files = {
        "Stray/Stray.exe": EXE_BYTES,
        "Stray/Engine/Binaries/Win64/x.dll": b"x",
        "Stray/Hk_project/Binaries/Win64/Hk_project-Win64-Shipping.exe": b"\0" * 5000,
        "Stray/Hk_project/Content/Paks/main.pak": b"v1",
    }
    dest = install_base(inst, tmp_path, library, base_files, title="Stray", slug="stray")
    patch = make_zip(tmp_path / "p.zip", {"Hk_project/Content/Paks/main.pak": b"v2"})
    inst.install(make_request(patch, library, option=patch_option(), existing=str(dest), title="Stray",
                              slug="stray"), token=CancelToken())
    assert (dest / "Hk_project" / "Content" / "Paks" / "main.pak").read_bytes() == b"v2"
    assert not (dest / "main.pak").exists() and not (dest / "Paks").exists()


def test_addon_strips_one_wrapper_and_applies_once(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    pack = {"Hollow Knight Language Pack/lang/fr.pak": b"fr"}
    for name in ("lp1.zip", "lp2.zip"):
        inst.install(make_request(make_zip(tmp_path / name, pack), library, option=addon_option(),
                                  existing=str(dest)), token=CancelToken())
    assert (dest / "lang" / "fr.pak").read_bytes() == b"fr"
    manifest = read_manifest(str(dest))
    assert manifest is not None
    assert manifest.applied_options == ["Direct", "Language Pack"]
    assert manifest.version == "1.5", "ADDON does not change the version"


def test_addon_launcher_provides_missing_executable(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library, {"Game/data.pak": b"x"}, title="Game", slug="game")
    assert read_manifest(str(dest)).executable == ""  # type: ignore[union-attr]
    addon = make_zip(tmp_path / "launcher.zip", {"Game.exe": EXE_BYTES})
    result = inst.install(make_request(addon, library, option=addon_option("Launcher"), existing=str(dest),
                                       title="Game", slug="game"), token=CancelToken())
    assert result.executable == "Game.exe"
    assert read_manifest(str(dest)).executable == "Game.exe"  # type: ignore[union-attr]


@pytest.mark.parametrize("existing", ["", "missing", "unmanaged"])
def test_overlay_requires_managed_base(inst: Installer, tmp_path: Path, library: Path, existing: str) -> None:
    target = ""
    if existing == "missing":
        target = str(library / "Nope")
    elif existing == "unmanaged":
        (library / "Unmanaged").mkdir(parents=True)
        target = str(library / "Unmanaged")
    patch = make_zip(tmp_path / "p.zip", {"a.txt": b"a"})
    with pytest.raises(InstallError, match="Install the base game first"):
        inst.install(make_request(patch, library, option=patch_option(), existing=target), token=CancelToken())
    assert patch.exists()


def test_overlay_rolls_back_on_failure(inst: Installer, tmp_path: Path, library: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library, {"G/G.exe": EXE_BYTES, "G/a.txt": b"old a", "G/z.txt": b"old z"},
                        title="G", slug="g")
    before = read_manifest(str(dest))
    os.chmod(dest / "a.txt", stat.S_IREAD)  # read-only originals must be handled too
    patch = make_zip(tmp_path / "p.zip", {"a.txt": b"new a", "z.txt": b"new z", "c/new.txt": b"new"})
    real_move = _fsutil.move_file

    def failing_move(src: str, dst: str) -> None:
        if dst.endswith(os.path.join("c", "new.txt")):
            raise OSError(errno.EIO, "simulated failure")
        real_move(src, dst)

    monkeypatch.setattr(_fsutil, "move_file", failing_move)
    with pytest.raises(InstallError, match="could not be updated"):
        inst.install(make_request(patch, library, option=patch_option(), existing=str(dest), title="G", slug="g"),
                     token=CancelToken())
    assert (dest / "a.txt").read_bytes() == b"old a"
    assert (dest / "z.txt").read_bytes() == b"old z"
    assert not (dest / "c").exists()
    assert read_manifest(str(dest)) == before
    assert staging_entries(library) == []
    assert patch.exists()


def test_overlay_cancel_rolls_back(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library, {"G/G.exe": EXE_BYTES, "G/a.txt": b"old a", "G/b.txt": b"old b"},
                        title="G", slug="g")
    patch = make_zip(tmp_path / "p.zip", {"a.txt": b"new a", "b.txt": b"new b"})
    token = CancelToken()

    def on_progress(phase: str, fraction: float) -> None:
        if phase == "installing" and fraction > 0:
            token.cancel()

    with pytest.raises(OperationCancelled):
        inst.install(make_request(patch, library, option=patch_option(), existing=str(dest), title="G", slug="g"),
                     token=token, on_progress=on_progress)
    assert (dest / "a.txt").read_bytes() == b"old a"
    assert (dest / "b.txt").read_bytes() == b"old b"
    assert staging_entries(library) == []


def test_overlay_file_folder_conflict(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library, {"G/G.exe": EXE_BYTES, "G/data/x": b"x"}, title="G", slug="g")
    patch = make_zip(tmp_path / "p.zip", {"G.exe": EXE_BYTES, "data": b"now a file"})
    with pytest.raises(InstallError, match="conflicts"):
        inst.install(make_request(patch, library, option=patch_option(), existing=str(dest), title="G", slug="g"),
                     token=CancelToken())
    assert (dest / "data" / "x").read_bytes() == b"x"
    assert (dest / "G.exe").read_bytes() == EXE_BYTES


def test_request_option_as_dict(inst: Installer, tmp_path: Path, library: Path) -> None:
    request = make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library)
    request.option = full_option().to_dict()  # type: ignore[assignment]
    result = inst.install(request, token=CancelToken())
    assert Path(result.install_path).is_dir()


# ---------------------------------------------------------------------------
# manifest helpers
# ---------------------------------------------------------------------------


def test_manifest_roundtrip_and_hidden(tmp_path: Path) -> None:
    manifest = InstallManifest(slug="s", title="Título ✓", version="1", executable="bin\\g.exe",
                               applied_options=["Direct"], genres=["RPG"], has_redist=True)
    write_manifest(str(tmp_path), manifest)
    assert read_manifest(str(tmp_path)) == manifest
    path = tmp_path / MANIFEST_FILENAME
    if os.name == "nt":
        assert _fsutil.is_hidden(str(path))
    manifest.version = "2"
    write_manifest(str(tmp_path), manifest)  # overwriting a hidden file must work
    assert read_manifest(str(tmp_path)).version == "2"  # type: ignore[union-attr]
    if os.name == "nt":
        assert _fsutil.is_hidden(str(path))
    assert [p.name for p in tmp_path.iterdir()] == [MANIFEST_FILENAME], "no temp files left"


def test_manifest_overwrite_read_only(tmp_path: Path) -> None:
    write_manifest(str(tmp_path), InstallManifest(slug="a"))
    os.chmod(tmp_path / MANIFEST_FILENAME, stat.S_IREAD)
    write_manifest(str(tmp_path), InstallManifest(slug="b"))
    assert read_manifest(str(tmp_path)).slug == "b"  # type: ignore[union-attr]


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"\xff\xfe\x00garbage", b""])
def test_manifest_unreadable(tmp_path: Path, content: bytes) -> None:
    (tmp_path / MANIFEST_FILENAME).write_bytes(content)
    assert read_manifest(str(tmp_path)) is None


def test_manifest_missing(tmp_path: Path) -> None:
    assert read_manifest(str(tmp_path)) is None
    assert read_manifest(str(tmp_path / "nope")) is None
    assert read_manifest("") is None


def test_manifest_types_coerced_and_unknown_keys_ignored(tmp_path: Path) -> None:
    data = {"slug": "s", "title": 5, "run_as_admin": "yes", "applied_options": ["a", 3, None, "b"],
            "genres": "RPG", "schema": True, "future_field": {"x": 1}, "has_redist": True}
    (tmp_path / MANIFEST_FILENAME).write_text("﻿" + json.dumps(data), encoding="utf-8")
    manifest = read_manifest(str(tmp_path))
    assert manifest is not None
    assert manifest.slug == "s" and manifest.title == ""
    assert manifest.run_as_admin is False and manifest.has_redist is True
    assert manifest.applied_options == ["a", "b"] and manifest.genres == []
    assert manifest.schema == 1


def test_write_manifest_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(InstallError):
        write_manifest(str(tmp_path / "gone"), InstallManifest())
    assert not (tmp_path / "gone").exists()


# ---------------------------------------------------------------------------
# directory helpers
# ---------------------------------------------------------------------------


def test_directory_size(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.bin").write_bytes(b"x" * 100)
    (tmp_path / "y.bin").write_bytes(b"y" * 23)
    (tmp_path / "empty").mkdir()
    assert directory_size(str(tmp_path)) == 123
    assert directory_size(str(tmp_path / "missing")) == 0
    assert directory_size("") == 0


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_directory_size_skips_junctions(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 1000)
    game = tmp_path / "game"
    game.mkdir()
    (game / "own.bin").write_bytes(b"x" * 10)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(game / "link"), str(outside)], check=True, capture_output=True)
    assert directory_size(str(game)) == 10


def test_directory_size_cancellable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "_SIZE_CHECK_EVERY", 2)
    for index in range(3):
        (tmp_path / f"f{index}").write_bytes(b"x")
    token = CancelToken()
    assert directory_size(str(tmp_path), token=token) == 3
    token.cancel()
    with pytest.raises(OperationCancelled):
        directory_size(str(tmp_path), token=token)


def test_find_redist_dirs(tmp_path: Path) -> None:
    for folder in ("_CommonRedist/DirectX", "Game/Redist", "Engine/Extras/Redist/en-us", "a/b/c/DirectX",
                   "Prerequisites", "Data"):
        (tmp_path / folder).mkdir(parents=True)
    assert find_redist_dirs(str(tmp_path)) == sorted(
        [os.path.join("Engine", "Extras", "Redist"), os.path.join("Game", "Redist"), "_CommonRedist",
         "Prerequisites"], key=str.casefold)
    assert find_redist_dirs(str(tmp_path / "missing")) == []


def test_locate_game_root_prefers_exe_dir_next_to_prerequisites(tmp_path: Path) -> None:
    (tmp_path / "Game" / "bin").mkdir(parents=True)
    (tmp_path / "Game" / "bin" / "game.exe").write_bytes(b"\0")
    (tmp_path / "Game" / "data.pak").write_bytes(b"\0")
    (tmp_path / "DirectX").mkdir()
    (tmp_path / "DirectX" / "DXSETUP.exe").write_bytes(b"\0")
    assert installer._locate_game_root(str(tmp_path)) == str(tmp_path / "Game")
    assert (tmp_path / "Game" / "DirectX" / "DXSETUP.exe").exists(), "prerequisites move into the game"


def test_locate_game_root_keeps_level_with_other_content(tmp_path: Path) -> None:
    (tmp_path / "Game" / "bin").mkdir(parents=True)
    (tmp_path / "Game" / "bin" / "game.exe").write_bytes(b"\0")
    (tmp_path / "Extras").mkdir()
    (tmp_path / "Extras" / "artbook.pdf").write_bytes(b"\0")
    assert installer._locate_game_root(str(tmp_path)) == str(tmp_path)
    assert (tmp_path / "Extras" / "artbook.pdf").exists()


def test_locate_game_root_descends_lossless_single_folders(tmp_path: Path) -> None:
    (tmp_path / "Game" / "bin").mkdir(parents=True)
    (tmp_path / "Game" / "bin" / "game.exe").write_bytes(b"\0")
    (tmp_path / "Read Me.txt").write_bytes(b"junk")
    # "Game" holds nothing but "bin", so "bin" is the complete game.
    assert installer._locate_game_root(str(tmp_path)) == str(tmp_path / "Game" / "bin")


def test_locate_game_root_redist_only_dirs_stay(tmp_path: Path) -> None:
    for name in ("_CommonRedist", "DirectX"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "setup.exe").write_bytes(b"\0")
    assert installer._locate_game_root(str(tmp_path)) == str(tmp_path)


def test_locate_game_root_two_exe_dirs_stays(tmp_path: Path) -> None:
    for name in ("GameA", "GameB"):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.exe").write_bytes(b"\0")
    assert installer._locate_game_root(str(tmp_path)) == str(tmp_path)


def test_overlay_never_replaces_the_install_manifest(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    hostile = json.dumps({"slug": "someone-else", "executable": "evil.exe"}).encode()
    patch = make_zip(tmp_path / "p.zip", {MANIFEST_FILENAME: hostile, "hollow_knight_Data/level0": b"patched"})
    inst.install(make_request(patch, library, option=patch_option(), existing=str(dest)), token=CancelToken())
    manifest = read_manifest(str(dest))
    assert manifest is not None
    assert (manifest.slug, manifest.executable) == (SLUG, "hollow_knight.exe")
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"patched"


def test_overlay_recovers_base_game_from_interrupted_swap(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    backup = library / f"{TITLE}{installer.BACKUP_INFIX}{'d' * 32}"
    os.replace(dest, backup)  # simulate a crash between the two renames of a reinstall
    patch = make_zip(tmp_path / "p.zip", {"hollow_knight_Data/level0": b"patched"})
    inst.install(make_request(patch, library, option=patch_option(), existing=str(dest)), token=CancelToken())
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"patched"
    assert backups(library) == []


# ---------------------------------------------------------------------------
# review additions
# ---------------------------------------------------------------------------


def test_existing_install_path_must_not_contain_a_library(inst: Installer, settings: SettingsStore, tmp_path: Path,
                                                          library: Path) -> None:
    parent = tmp_path / "Parent"
    nested_library = parent / "Games"
    (nested_library / "Other Game").mkdir(parents=True)
    settings.update(library_dirs=[str(library), str(nested_library)])
    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    with pytest.raises(InstallError, match="library folder"):
        inst.install(make_request(archive, library, existing=str(parent)), token=CancelToken())
    assert (nested_library / "Other Game").is_dir(), "a library inside the destination survives"
    assert staging_entries(tmp_path) == []
    assert archive.exists()


def test_existing_install_path_inside_work_folder_refused(inst: Installer, tmp_path: Path, library: Path) -> None:
    downloads = library / ".ankerclient" / "downloads"
    (downloads / "job-1").mkdir(parents=True)
    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    with pytest.raises(InstallError, match="work folder"):
        inst.install(make_request(archive, library, existing=str(downloads)), token=CancelToken())
    assert (downloads / "job-1").is_dir()


def test_progress_error_after_commit_does_not_fail_install(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)

    def on_progress(phase: str, fraction: float) -> None:
        if phase == "installing" and fraction >= 0.85:
            raise OperationCancelled()  # e.g. the UI cancelled a moment too late

    archive = make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES)
    result = inst.install(make_request(archive, library, version="2.0"), token=CancelToken(), on_progress=on_progress)
    assert result.install_path == str(dest)
    assert read_manifest(str(dest)).version == "2.0"  # type: ignore[union-attr]
    assert not archive.exists(), "a committed install finishes its clean-up"
    assert backups(library) == [] and staging_entries(library) == []


def test_progress_error_before_commit_cancels(inst: Installer, tmp_path: Path, library: Path) -> None:
    def on_progress(phase: str, fraction: float) -> None:
        if phase == "installing" and fraction >= 0.3:
            raise OperationCancelled()

    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    with pytest.raises(OperationCancelled):
        inst.install(make_request(archive, library), token=CancelToken(), on_progress=on_progress)
    assert not (library / TITLE).exists()
    assert staging_entries(library) == []
    assert archive.exists()


def test_option_kind_as_plain_string(inst: Installer, tmp_path: Path, library: Path) -> None:
    option = DownloadOption(download_id=1, label="Direct", kind="full")  # type: ignore[arg-type]
    result = inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library, option=option),
                          token=CancelToken())
    assert result.install_path == str(library / TITLE), "treated as FULL, not as an overlay"

    bogus = DownloadOption(download_id=1, label="?", kind="bogus")  # type: ignore[arg-type]
    archive = make_zip(tmp_path / "b.zip", GAME_FILES)
    with pytest.raises(InstallError, match="incomplete"):
        inst.install(make_request(archive, library, option=bogus), token=CancelToken())


def test_fresh_name_taken_during_install_is_never_replaced(inst: Installer, tmp_path: Path, library: Path) -> None:
    foreign = library / TITLE

    def on_progress(phase: str, fraction: float) -> None:
        # Another install (or the user) creates the folder after the destination was chosen.
        if phase == "installing" and fraction >= 0.6 and not foreign.exists():
            foreign.mkdir(parents=True)
            (foreign / "save.dat").write_text("keep me")

    archive = make_zip(tmp_path / "hk.zip", GAME_FILES)
    result = inst.install(make_request(archive, library), token=CancelToken(), on_progress=on_progress)
    assert result.install_path == str(library / f"{TITLE} (2)")
    assert (foreign / "save.dat").read_text() == "keep me"
    assert (library / f"{TITLE} (2)" / "hollow_knight.exe").exists()
    assert backups(library) == []


def test_half_deleted_old_install_is_never_resurrected(inst: Installer, tmp_path: Path, library: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library)
    real_try_remove_tree = _fsutil.try_remove_tree
    # Locked files (antivirus, Explorer) make every delete fail for this reinstall.
    monkeypatch.setattr(_fsutil, "try_remove_tree", lambda _path, *, within: False)
    inst.install(make_request(make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES), library, version="2.0"),
                 token=CancelToken())
    assert backups(library) == [], "the replaced copy waits in staging, not next to the game"
    monkeypatch.setattr(_fsutil, "try_remove_tree", real_try_remove_tree)

    _fsutil.remove_tree(str(dest), within=str(library))  # the user uninstalls the game
    other = make_zip(tmp_path / "other.zip", {"Other/Other.exe": EXE_BYTES})
    inst.install(make_request(other, library, title="Other", slug="other"), token=CancelToken())
    assert not dest.exists(), "the old copy must not come back as the game"
    assert staging_entries(library) == []


def test_old_install_deleted_in_place_when_it_cannot_move_to_staging(inst: Installer, tmp_path: Path,
                                                                     library: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library)
    real_rename = _fsutil.rename_with_retry

    def no_move_into_staging(src: str, dst: str, **kwargs: Any) -> None:
        if os.path.basename(dst) == "old":
            raise PermissionError(errno.EACCES, "denied")
        real_rename(src, dst, **kwargs)

    monkeypatch.setattr(_fsutil, "rename_with_retry", no_move_into_staging)
    inst.install(make_request(make_zip(tmp_path / "dl" / "v2.zip", GAME_FILES), library), token=CancelToken())
    assert (dest / "hollow_knight.exe").exists()
    assert backups(library) == [] and staging_entries(library) == []


def test_recovery_deletes_leftover_backup_via_staging(inst: Installer, tmp_path: Path, library: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (library / "Present").mkdir(parents=True)
    leftover = library / f"Present{installer.BACKUP_INFIX}{'e' * 32}"
    leftover.mkdir()
    (leftover / "old.bin").write_bytes(b"old")
    real_try_remove_tree = _fsutil.try_remove_tree
    monkeypatch.setattr(_fsutil, "try_remove_tree", lambda _path, *, within: False)  # deletes fail
    Installer._recover_interrupted(str(library))
    monkeypatch.setattr(_fsutil, "try_remove_tree", real_try_remove_tree)
    assert backups(library) == [], "never left next to the game where it could be restored"
    assert len(staging_entries(library)) == 1
    inst.install(make_request(make_zip(tmp_path / "a.zip", GAME_FILES), library), token=CancelToken())
    assert staging_entries(library) == []


def test_purge_checks_the_live_session_registry(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = library / ".ankerclient" / "staging"
    stale = base / ("a" * 32)
    late = base / ("b" * 32)
    stale.mkdir(parents=True)
    late.mkdir()
    real_scandir = installer._scandir
    late_key = installer._norm_key(str(late))

    def scandir_then_register(path: str) -> list[os.DirEntry[str]]:
        entries = real_scandir(path)
        if os.path.normcase(path) == os.path.normcase(str(base)):
            with installer._ACTIVE_LOCK:  # a concurrent install registers right after the scan
                installer._ACTIVE_STAGING.add(late_key)
        return entries

    monkeypatch.setattr(installer, "_scandir", scandir_then_register)
    try:
        Installer._purge_stale_staging(str(base), str(library))
    finally:
        with installer._ACTIVE_LOCK:
            installer._ACTIVE_STAGING.discard(late_key)
    assert not stale.exists()
    assert late.exists(), "an active session is never purged"


def test_overlay_manifest_failure_rolls_back_files(inst: Installer, tmp_path: Path, library: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library)
    before = read_manifest(str(dest))
    patch = make_zip(tmp_path / "p.zip", {"hollow_knight_Data/level0": b"patched", "new_file.txt": b"new"})

    def failing_write(_install_dir: str, _manifest: InstallManifest) -> None:
        raise InstallError("The game information could not be saved.")

    monkeypatch.setattr(installer, "write_manifest", failing_write)
    with pytest.raises(InstallError, match="could not be saved"):
        inst.install(make_request(patch, library, option=patch_option(), existing=str(dest)), token=CancelToken())
    assert (dest / "hollow_knight_Data" / "level0").read_bytes() == b"level zero"
    assert not (dest / "new_file.txt").exists()
    assert read_manifest(str(dest)) == before
    assert staging_entries(library) == []
    assert patch.exists()


@pytest.mark.skipif(os.name != "nt", reason="read-only attribute semantics")
def test_overlay_rollback_restores_read_only(inst: Installer, tmp_path: Path, library: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    dest = install_base(inst, tmp_path, library, {"G/G.exe": EXE_BYTES, "G/a.txt": b"old a"}, title="G", slug="g")
    os.chmod(dest / "a.txt", stat.S_IREAD)
    patch = make_zip(tmp_path / "p.zip", {"a.txt": b"new a", "b.txt": b"new b"})
    real_move = _fsutil.move_file

    def failing_move(src: str, dst: str) -> None:
        if dst.endswith("b.txt"):
            raise OSError(errno.EIO, "simulated failure")
        real_move(src, dst)

    monkeypatch.setattr(_fsutil, "move_file", failing_move)
    with pytest.raises(InstallError):
        inst.install(make_request(patch, library, option=patch_option(), existing=str(dest), title="G", slug="g"),
                     token=CancelToken())
    assert (dest / "a.txt").read_bytes() == b"old a"
    assert not os.access(dest / "a.txt", os.W_OK), "read-only attribute restored"
    os.chmod(dest / "a.txt", stat.S_IWRITE | stat.S_IREAD)


@needs_7z
def test_cancel_real_7z_extraction_cleans_staging(settings: SettingsStore, shortcuts: FakeShortcuts, tmp_path: Path,
                                                  library: Path) -> None:
    src = tmp_path / "src" / "Game"
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(EXE_BYTES)
    (src / "payload.bin").write_bytes(os.urandom(16 * 1024 * 1024))  # BZip2: seconds to extract
    archive = tmp_path / "Game.7z"
    subprocess.run([SEVEN_ZIP, "a", "-m0=BZip2", str(archive), "Game"], cwd=src.parent, check=True,
                   capture_output=True, creationflags=_fsutil.CREATE_NO_WINDOW)
    real = Installer(settings, Extractor(lambda: SEVEN_ZIP or ""), shortcuts)  # type: ignore[arg-type]
    token = CancelToken()
    timer = threading.Timer(2.0, token.cancel)  # safety net if no progress arrives

    def on_progress(phase: str, fraction: float) -> None:
        if phase == "extracting" and fraction > 0:
            token.cancel()

    timer.start()
    try:
        with pytest.raises(OperationCancelled):
            real.install(make_request(archive, library, title="Game", slug="game"), token=token,
                         on_progress=on_progress)
    finally:
        timer.cancel()
    assert not (library / "Game").exists()
    assert staging_entries(library) == [], "7z.exe was terminated, so its partial output could be deleted"
    assert archive.exists()


def test_archive_inside_the_folder_it_replaces_is_refused(inst: Installer, tmp_path: Path, library: Path) -> None:
    dest = install_base(inst, tmp_path, library)
    archive = make_zip(dest / "backup" / "Hollow-Knight-AnkerGames.zip", GAME_FILES)  # kept in the game folder
    with pytest.raises(InstallError, match="inside the game folder"):
        inst.install(make_request(archive, library), token=CancelToken(), keep_archive=True)
    assert archive.exists(), "the user's archive is never deleted with the old copy"
    assert (dest / "hollow_knight.exe").exists()
    assert backups(library) == [] and staging_entries(library) == []

"""LibraryService.uninstall: safety checks, links, read-only files, locks, cancellation."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.errors import InstallError, OperationCancelled
from anker_client.core.events import GameUninstalled, LibraryChanged
from anker_client.core.models import InstallManifest
from anker_client.core.tasks import CancelToken
from anker_client.services import library as library_module
from tests.unit.test_library_support import LibraryEnv, make_env, make_game_dir, set_windows_attributes

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-specific file system behaviour")

FILES = {
    "game.exe": b"MZ" * 100,
    "data/level1.pak": b"1" * 1000,
    "data/level2.pak": b"2" * 1000,
    "data/sub/deep.bin": b"3" * 10,
    "readme.txt": b"hello",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryEnv]:
    environment = make_env(tmp_path, monkeypatch)
    yield environment
    environment.db.close()


@pytest.fixture
def game(env: LibraryEnv) -> Path:
    folder = make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"),
                           files=FILES)
    env.library.scan()
    env.library.record_play_session("portal-2", "2026-01-01T00:00:00+00:00", "2026-01-01T02:00:00+00:00", 7200)
    env.library.set_favorite("portal-2", True)
    env.library.compute_size("portal-2")
    env.library.set_update_state("portal-2", latest_version="v2", available=True)
    env.recorder.clear()
    return folder


def _make_junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_uninstall_deletes_folder_and_keeps_playtime(env: LibraryEnv, game: Path) -> None:
    env.library.uninstall("portal-2")

    assert not game.exists()
    assert env.root.exists()
    assert env.library.get("portal-2") is None
    assert env.shortcuts.removed == ["Portal 2"]
    row = env.rows()["portal-2"]
    assert (row["playtime_seconds"], row["favorite"]) == (7200, 1)
    assert (row["size_bytes"], row["update_available"], row["latest_version"]) == (None, 0, "")
    assert env.recorder.of(GameUninstalled) == [GameUninstalled("portal-2", "Portal 2")]
    assert env.recorder.of(LibraryChanged) == [LibraryChanged(frozenset({"portal-2"}))]

    # A re-install picks the playtime up again.
    make_game_dir(env.root, "Portal 2", manifest=InstallManifest(slug="portal-2", title="Portal 2"))
    env.library.scan()
    assert env.library.get("portal-2").playtime_seconds == 7200  # type: ignore[union-attr]


def test_uninstall_unmanaged_folder(env: LibraryEnv) -> None:
    folder = make_game_dir(env.root, "Some Folder")
    env.library.scan()
    env.library.uninstall("local:some folder")
    assert not folder.exists()


def test_refuses_path_outside_library_roots(env: LibraryEnv, game: Path, tmp_path: Path) -> None:
    other = tmp_path / "Other"
    other.mkdir()
    env.settings.update(library_dirs=[str(other)], default_library=str(other))  # root removed after the scan
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert (game / "game.exe").exists() and (game / MANIFEST_FILENAME).exists()
    assert env.library.get("portal-2") is not None
    assert env.shortcuts.removed == []


def test_refuses_dangerous_targets(env: LibraryEnv, game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_module, "is_dangerous_delete_target", lambda path: True)
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert (game / "game.exe").exists()


def test_refuses_folder_containing_a_library_root(env: LibraryEnv, game: Path) -> None:
    nested = game / "data"
    env.settings.update(library_dirs=[str(env.root), str(nested)])
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert (nested / "level1.pak").exists()


@windows_only
def test_junction_inside_game_is_removed_without_touching_target(env: LibraryEnv, game: Path,
                                                                 tmp_path: Path) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    _make_junction(game / "linked", outside)

    env.library.uninstall("portal-2")

    assert not game.exists()
    assert (outside / "keep.txt").read_text() == "do not delete"


@windows_only
def test_symlink_inside_game_is_removed_without_touching_target(env: LibraryEnv, game: Path,
                                                                tmp_path: Path) -> None:
    outside_file = tmp_path / "precious.txt"
    outside_file.write_text("keep")
    outside_dir = tmp_path / "precious-dir"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("keep")
    try:
        os.symlink(outside_file, game / "file-link.txt")
        os.symlink(outside_dir, game / "dir-link", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation")

    env.library.uninstall("portal-2")

    assert not game.exists()
    assert outside_file.read_text() == "keep"
    assert (outside_dir / "keep.txt").read_text() == "keep"


@windows_only
def test_refuses_game_folder_that_is_a_junction(env: LibraryEnv, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere" / "Real Game"
    make_game_dir(real.parent, "Real Game", manifest=InstallManifest(slug="real", title="Real Game"))
    _make_junction(env.root / "Real Game", real)
    env.library.scan()
    assert env.library.get("real") is not None  # linked game folders are listed…

    with pytest.raises(InstallError):  # …but never deleted through the link
        env.library.uninstall("real")
    assert (real / "game.exe").exists()


def test_read_only_files_and_folders_are_deleted(env: LibraryEnv, game: Path) -> None:
    os.chmod(game / "game.exe", stat.S_IREAD)
    os.chmod(game / "data" / "sub" / "deep.bin", stat.S_IREAD)
    if os.name == "nt":
        set_windows_attributes(game / "data" / "sub", 0x1)  # FILE_ATTRIBUTE_READONLY on a directory
    env.library.uninstall("portal-2")
    assert not game.exists()


def test_manifest_is_deleted_last(env: LibraryEnv, game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services import _library_fs

    order: list[str] = []
    real_remove = os.remove

    def recording_remove(path: str) -> None:
        order.append(os.path.basename(path))
        real_remove(path)

    monkeypatch.setattr(_library_fs.os, "remove", recording_remove)
    env.library.uninstall("portal-2")
    assert order[-1] == MANIFEST_FILENAME
    assert len(order) == len(FILES) + 1


class _CancelAfter(CancelToken):
    """Cancels itself after ``n`` cancellation checks (deterministic mid-delete cancel)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.remaining = n

    def raise_if_cancelled(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            self.cancel("test")
        super().raise_if_cancelled()


def test_uninstall_is_cancellable_between_files(env: LibraryEnv, game: Path) -> None:
    with pytest.raises(OperationCancelled):
        env.library.uninstall("portal-2", token=_CancelAfter(3))

    assert (game / MANIFEST_FILENAME).exists()  # still recognisable and retryable
    remaining = [p for p in game.rglob("*") if p.is_file() and p.name != MANIFEST_FILENAME]
    assert 0 < len(remaining) < len(FILES)
    current = env.library.get("portal-2")
    assert current is not None and current.size_bytes is None  # stale size dropped
    assert env.shortcuts.removed == []
    assert env.recorder.of(GameUninstalled) == []

    env.library.uninstall("portal-2")  # retry finishes the job
    assert not game.exists()


@windows_only
def test_locked_file_is_retried_until_released(env: LibraryEnv, game: Path) -> None:
    env.library._delete_retry_delay = 0.1  # ~1.5 s of retries: ample margin over the 0.15 s lock under load
    handle = open(game / "data" / "level1.pak", "rb")  # noqa: SIM115 - held open on purpose
    timer = threading.Timer(0.15, handle.close)
    timer.start()
    try:
        env.library.uninstall("portal-2")
    finally:
        timer.cancel()
        handle.close()
    assert not game.exists()


@windows_only
def test_permanently_locked_file_fails_cleanly(env: LibraryEnv, game: Path) -> None:
    with open(game / "data" / "level1.pak", "rb"), pytest.raises(InstallError) as info:
        env.library.uninstall("portal-2")
    assert "level1.pak" in info.value.user_message
    assert (game / MANIFEST_FILENAME).exists()
    assert env.library.get("portal-2") is not None
    assert env.recorder.of(GameUninstalled) == []

    env.library.uninstall("portal-2")  # works once the file is closed
    assert not game.exists()


def test_shortcut_failure_does_not_fail_uninstall(env: LibraryEnv, game: Path) -> None:
    env.shortcuts.fail = True
    env.library.uninstall("portal-2")
    assert not game.exists()
    assert env.recorder.of(GameUninstalled)


def test_folder_already_gone(env: LibraryEnv, game: Path) -> None:
    import shutil

    shutil.rmtree(game)
    env.library.uninstall("portal-2")
    assert env.library.get("portal-2") is None
    assert env.recorder.of(GameUninstalled) == [GameUninstalled("portal-2", "Portal 2")]


def test_uninstall_after_rename_removes_the_install_time_shortcuts(env: LibraryEnv, game: Path) -> None:
    env.library.rename("portal-2", "Portal Two")  # shortcuts were created as "Portal 2" at install time
    env.library.uninstall("portal-2")
    assert env.shortcuts.removed == ["Portal Two", "Portal 2"]


def test_uninstall_keeps_shortcuts_named_after_another_game(env: LibraryEnv, game: Path) -> None:
    make_game_dir(env.root, "Portal 2 GOTY", manifest=InstallManifest(slug="portal-2-goty", title="Portal 2"))
    env.library.scan()
    env.library.rename("portal-2", "Portal Two")
    env.library.uninstall("portal-2")
    assert env.shortcuts.removed == ["Portal Two"]  # "Portal 2" now belongs to the other install


def test_refuses_folder_holding_the_running_client(env: LibraryEnv, game: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys, "executable", str(game / "AnkerClient.exe"))  # a portable copy inside the game
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert (game / "game.exe").exists()


def test_refuses_folder_holding_the_client_data(env: LibraryEnv, game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANKERCLIENT_HOME", str(game / "portable-data"))
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert (game / "game.exe").exists()


def test_refuses_while_a_program_from_the_folder_runs(env: LibraryEnv, game: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    running = [str(game / "bin" / "game.exe"), str(env.root / "Portal 2 Mods" / "mod.exe")]
    monkeypatch.setattr(library_module, "_running_executables", lambda: list(running))
    with pytest.raises(InstallError) as info:
        env.library.uninstall("portal-2")
    assert info.value.user_message == "Close Portal 2 before uninstalling it."
    assert (game / "game.exe").exists()

    running.pop(0)  # closed; a sibling folder with a similar name does not count
    env.library.uninstall("portal-2")
    assert not game.exists()


def test_process_listing_failure_does_not_block_uninstall(env: LibraryEnv, game: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[str]:
        raise OSError("no access")

    monkeypatch.setattr(library_module, "_running_executables", broken)
    env.library.uninstall("portal-2")
    assert not game.exists()


@windows_only
def test_refuses_while_the_game_really_runs(env: LibraryEnv, game: Path) -> None:
    import shutil
    import subprocess

    ping = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "PING.EXE"
    if not ping.is_file():
        pytest.skip("PING.EXE not available")
    shutil.copy2(ping, game / "PING.EXE")
    process = subprocess.Popen([str(game / "PING.EXE"), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(InstallError):
            env.library.uninstall("portal-2")
        assert (game / "data" / "level1.pak").exists()  # nothing was deleted
    finally:
        process.kill()
        process.wait(10)
    env.library.uninstall("portal-2")
    assert not game.exists()


def test_game_stays_marked_until_the_uninstall_is_recorded(env: LibraryEnv, game: Path) -> None:
    nested: list[BaseException] = []
    real_remove = env.shortcuts.remove

    def remove_and_uninstall_again(title: str) -> None:
        real_remove(title)
        try:  # e.g. a second click while the first uninstall is still finishing
            env.library.uninstall("portal-2")
        except InstallError as exc:
            nested.append(exc)

    env.shortcuts.remove = remove_and_uninstall_again  # type: ignore[method-assign]
    env.library.uninstall("portal-2")
    assert len(nested) == 1 and "already being uninstalled" in nested[0].user_message
    assert env.recorder.of(GameUninstalled) == [GameUninstalled("portal-2", "Portal 2")]
    assert env.library._uninstalling == set()


def test_failed_uninstall_can_be_retried(env: LibraryEnv, game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services import _library_fs

    real_delete_tree = _library_fs.delete_tree
    failures = {"left": 1}

    def failing_once(*args: object, **kwargs: object) -> object:
        if failures["left"]:
            failures["left"] -= 1
            raise InstallError("boom")
        return real_delete_tree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_library_fs, "delete_tree", failing_once)
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    env.library.uninstall("portal-2")  # not refused as "already being uninstalled"
    assert not game.exists()


@windows_only
def test_uninstall_removes_names_win32_would_rewrite(env: LibraryEnv, game: Path) -> None:
    with open("\\\\?\\" + str(game / "data" / "save."), "wb") as handle:
        handle.write(b"x")
    env.library.uninstall("portal-2")
    assert not game.exists()


def test_concurrent_uninstall_of_same_game_is_refused(env: LibraryEnv, game: Path) -> None:
    env.library._uninstalling.add("portal-2")  # simulate an uninstall in progress
    with pytest.raises(InstallError):
        env.library.uninstall("portal-2")
    assert game.exists()

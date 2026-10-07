"""Tests for the PATCH/ADDON overlay journal: crash recovery by the next install, journal validation."""

from __future__ import annotations

import json
import os
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.errors import InstallError
from anker_client.core.models import DownloadKind, DownloadOption, InstallManifest, InstallRequest
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services.install import _fsutil, _overlay, installer
from anker_client.services.install.extractor import Extractor
from anker_client.services.install.installer import Installer, read_manifest, write_manifest
from anker_client.services.install.sevenzip import SevenZip

EXE_BYTES = b"\0" * 300_000
BASE_FILES = {"G/G.exe": EXE_BYTES, "G/a.txt": b"old a", "G/b.txt": b"old b", "G/data/keep.bin": b"keep"}
PATCH_FILES = {"a.txt": b"new a", "b.txt": b"new b", "c/new.txt": b"new c", "data/keep.bin": b"patched"}


class _Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


class _NoShortcuts:
    def create(self, *_args: Any, **_kwargs: Any) -> list[str]:
        return []


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    return tmp_path / "Games"


@pytest.fixture
def inst(tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch) -> Installer:
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))
    settings = SettingsStore(tmp_path / "config.json")
    settings.update(library_dirs=[str(library)], default_library=str(library), delete_archive_after_install=False)
    return Installer(settings, Extractor(lambda: ""), _NoShortcuts())  # type: ignore[arg-type]


def request(archive: Path, library: Path, *, kind: DownloadKind = DownloadKind.FULL, existing: str = "",
            title: str = "G", slug: str = "g") -> InstallRequest:
    option = DownloadOption(download_id=1, label="Direct" if kind is DownloadKind.FULL else "Update Only",
                            kind=kind, to_version="2.0" if kind is DownloadKind.PATCH else "")
    return InstallRequest(archive_path=str(archive), slug=slug, title=title, option=option,
                          library_root=str(library), version="1.0", existing_install_path=existing)


def staging_entries(library: Path) -> list[Path]:
    base = library / ".ankerclient" / "staging"
    return list(base.iterdir()) if base.exists() else []


@pytest.fixture
def base_game(inst: Installer, tmp_path: Path, library: Path) -> Iterator[Path]:
    result = inst.install(request(make_zip(tmp_path / "base.zip", BASE_FILES), library), token=CancelToken())
    dest = Path(result.install_path)
    os.chmod(dest / "a.txt", stat.S_IREAD)
    yield dest
    if (dest / "a.txt").exists():
        os.chmod(dest / "a.txt", stat.S_IREAD | stat.S_IWRITE)


@pytest.fixture
def crash_mode(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make the process "die" at a chosen point: no in-process rollback, staging left on disk."""
    original_discard = Installer.__dict__["_discard_staging"]
    original_rollback = _overlay.OverlayTransaction.rollback

    def keep_staging(session: str, _target_root: str) -> None:
        with installer._ACTIVE_LOCK:  # the dead process no longer owns its session
            installer._ACTIVE_STAGING.discard(installer._norm_key(session))

    def no_rollback(self: _overlay.OverlayTransaction) -> bool:
        self.close()
        return True

    monkeypatch.setattr(Installer, "_discard_staging", staticmethod(keep_staging))
    monkeypatch.setattr(_overlay.OverlayTransaction, "rollback", no_rollback)

    def restore() -> None:
        monkeypatch.setattr(Installer, "_discard_staging", original_discard)
        monkeypatch.setattr(_overlay.OverlayTransaction, "rollback", original_rollback)

    return {"restore": restore}


def assert_base_state(dest: Path, manifest: InstallManifest | None) -> None:
    assert (dest / "a.txt").read_bytes() == b"old a"
    assert not os.access(dest / "a.txt", os.W_OK), "read-only attribute restored"
    assert (dest / "b.txt").read_bytes() == b"old b"
    assert (dest / "data" / "keep.bin").read_bytes() == b"keep"
    assert not (dest / "c").exists()
    assert read_manifest(str(dest)) == manifest


def install_other_game(inst: Installer, tmp_path: Path, library: Path) -> None:
    archive = make_zip(tmp_path / "other.zip", {"Other/Other.exe": EXE_BYTES})
    inst.install(request(archive, library, title="Other", slug="other"), token=CancelToken())


# ---------------------------------------------------------------------------
# crash recovery through the next install
# ---------------------------------------------------------------------------


def test_crash_mid_update_is_rolled_back_by_next_install(inst: Installer, tmp_path: Path, library: Path,
                                                         base_game: Path, crash_mode: dict[str, Any],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    before = read_manifest(str(base_game))
    real_move = _fsutil.move_file

    def crash_on_b(src: str, dst: str) -> None:
        if dst.endswith("b.txt"):
            raise _Crash()
        real_move(src, dst)

    monkeypatch.setattr(_fsutil, "move_file", crash_on_b)
    patch = make_zip(tmp_path / "patch.zip", PATCH_FILES)
    with pytest.raises(_Crash):
        inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)), token=CancelToken())
    # Half-patched: a.txt replaced, b.txt moved to the backup and not yet replaced.
    assert (base_game / "a.txt").read_bytes() == b"new a"
    assert not (base_game / "b.txt").exists()
    assert len(staging_entries(library)) == 1

    crash_mode["restore"]()
    monkeypatch.setattr(_fsutil, "move_file", real_move)
    install_other_game(inst, tmp_path, library)
    assert_base_state(base_game, before)
    assert staging_entries(library) == []


def test_crash_after_manifest_update_rolls_back_files_and_manifest(inst: Installer, tmp_path: Path,
                                                                   library: Path, base_game: Path,
                                                                   crash_mode: dict[str, Any],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    before = read_manifest(str(base_game))
    real_complete = _overlay.OverlayTransaction.complete

    def crash(_self: _overlay.OverlayTransaction) -> None:
        raise _Crash()

    monkeypatch.setattr(_overlay.OverlayTransaction, "complete", crash)
    patch = make_zip(tmp_path / "patch.zip", PATCH_FILES)
    with pytest.raises(_Crash):
        inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)), token=CancelToken())
    assert read_manifest(str(base_game)).version == "2.0"  # type: ignore[union-attr]
    assert (base_game / "c" / "new.txt").exists()

    crash_mode["restore"]()
    monkeypatch.setattr(_overlay.OverlayTransaction, "complete", real_complete)
    install_other_game(inst, tmp_path, library)
    assert_base_state(base_game, before)  # files and manifest agree again
    if os.name == "nt":
        assert _fsutil.is_hidden(str(base_game / MANIFEST_FILENAME))
    assert staging_entries(library) == []


def test_next_update_of_the_same_game_recovers_first(inst: Installer, tmp_path: Path, library: Path,
                                                     base_game: Path, crash_mode: dict[str, Any],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    real_move = _fsutil.move_file

    def crash_on_new(src: str, dst: str) -> None:
        if dst.endswith("new.txt"):
            raise _Crash()
        real_move(src, dst)

    monkeypatch.setattr(_fsutil, "move_file", crash_on_new)
    patch = make_zip(tmp_path / "patch.zip", PATCH_FILES)
    with pytest.raises(_Crash):
        inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)), token=CancelToken())

    crash_mode["restore"]()
    monkeypatch.setattr(_fsutil, "move_file", real_move)
    result = inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)),
                          token=CancelToken())
    assert result.install_path == str(base_game)
    for name, data in PATCH_FILES.items():
        assert (base_game / name).read_bytes() == data
    manifest = read_manifest(str(base_game))
    assert manifest is not None and manifest.version == "2.0"
    assert manifest.applied_options == ["Direct", "Update Only"]
    assert staging_entries(library) == []


def test_successful_update_leaves_no_journal(inst: Installer, tmp_path: Path, library: Path,
                                             base_game: Path) -> None:
    patch = make_zip(tmp_path / "patch.zip", PATCH_FILES)
    inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)), token=CancelToken())
    assert staging_entries(library) == []
    install_other_game(inst, tmp_path, library)  # replaying nothing: the update stays applied
    assert (base_game / "c" / "new.txt").read_bytes() == b"new c"


# ---------------------------------------------------------------------------
# journal replay rules
# ---------------------------------------------------------------------------


def write_journal(session: Path, lines: list[Any], *, raw_tail: str = "") -> None:
    session.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(line) + "\n" for line in lines) + raw_tail
    (session / _overlay.JOURNAL_FILENAME).write_text(text, encoding="utf-8")


@pytest.fixture
def game(library: Path) -> Path:
    path = library / "Game"
    path.mkdir(parents=True)
    (path / "a.txt").write_bytes(b"new a")
    (path / "added.txt").write_bytes(b"added")
    write_manifest(str(path), InstallManifest(slug="game", version="2.0"))
    return path


@pytest.fixture
def session(library: Path) -> Path:
    path = library / ".ankerclient" / "staging" / ("f" * 32)
    (path / _overlay.BACKUP_SUBDIR).mkdir(parents=True)
    (path / _overlay.BACKUP_SUBDIR / "a.txt").write_bytes(b"old a")
    return path


def test_recover_replays_records_and_restores_manifest(library: Path, game: Path, session: Path) -> None:
    old_manifest = json.dumps(InstallManifest(slug="game", version="1.0").to_dict())
    write_journal(session, [
        {"target": str(game)},
        {"kind": "file", "rel": "a.txt", "orig": True, "ro": False},
        {"kind": "file", "rel": "added.txt", "orig": False, "ro": False},
        {"kind": "file", "rel": "never_moved.txt", "orig": True, "ro": False},  # backup missing: left alone
        {"kind": "manifest", "text": old_manifest},
    ])
    (game / "never_moved.txt").write_bytes(b"original")
    assert _overlay.recover(str(session), within=str(library)) is True
    assert (game / "a.txt").read_bytes() == b"old a"
    assert not (game / "added.txt").exists()
    assert (game / "never_moved.txt").read_bytes() == b"original"
    assert read_manifest(str(game)).version == "1.0"  # type: ignore[union-attr]
    assert not (session / _overlay.JOURNAL_FILENAME).exists()


def test_completed_journal_is_never_replayed(library: Path, game: Path, session: Path) -> None:
    write_journal(session, [
        {"target": str(game)},
        {"kind": "file", "rel": "a.txt", "orig": True, "ro": False},
        {"done": True},
    ])
    assert _overlay.recover(str(session), within=str(library)) is True
    assert (game / "a.txt").read_bytes() == b"new a"


def test_torn_last_line_is_ignored(library: Path, game: Path, session: Path) -> None:
    write_journal(session, [
        {"target": str(game)},
        {"kind": "file", "rel": "a.txt", "orig": True, "ro": False},
    ], raw_tail='{"kind": "file", "rel": "added.t')  # the crash hit mid-write
    assert _overlay.recover(str(session), within=str(library)) is True
    assert (game / "a.txt").read_bytes() == b"old a"
    assert (game / "added.txt").exists(), "the torn record is not acted on"


def test_journal_target_outside_library_is_refused(tmp_path: Path, library: Path, session: Path) -> None:
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    (outside / "added.txt").write_bytes(b"precious")
    write_journal(session, [{"target": str(outside)}, {"kind": "file", "rel": "added.txt", "orig": False}])
    assert _overlay.recover(str(session), within=str(library)) is True
    assert (outside / "added.txt").read_bytes() == b"precious"


@pytest.mark.parametrize("relative", ["../outside.txt", "..", "C:/Windows/x.txt", "/abs.txt", "", "sub/../../x"])
def test_unsafe_journal_paths_are_ignored(tmp_path: Path, library: Path, game: Path, session: Path,
                                          relative: str) -> None:
    victim = library / "outside.txt"
    victim.write_bytes(b"precious")
    write_journal(session, [{"target": str(game)}, {"kind": "file", "rel": relative, "orig": False}])
    assert _overlay.recover(str(session), within=str(library)) is True
    assert victim.read_bytes() == b"precious"


def test_failed_restore_keeps_the_session(library: Path, game: Path, session: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    write_journal(session, [{"target": str(game)}, {"kind": "file", "rel": "a.txt", "orig": True, "ro": False}])

    def locked(_src: str, _dst: str, **_kwargs: Any) -> None:
        raise PermissionError(13, "locked")

    real_rename = _fsutil.rename_with_retry
    monkeypatch.setattr(_fsutil, "rename_with_retry", locked)
    assert _overlay.recover(str(session), within=str(library)) is False
    Installer._purge_stale_staging(str(session.parent), str(library))
    assert (session / _overlay.BACKUP_SUBDIR / "a.txt").read_bytes() == b"old a", "backup kept for a retry"

    monkeypatch.setattr(_fsutil, "rename_with_retry", real_rename)
    Installer._purge_stale_staging(str(session.parent), str(library))
    assert (game / "a.txt").read_bytes() == b"old a"
    assert not session.exists()


def test_session_without_journal_is_simply_purged(library: Path, session: Path) -> None:
    assert _overlay.recover(str(session), within=str(library)) is True
    Installer._purge_stale_staging(str(session.parent), str(library))
    assert not session.exists()


def test_unfinished_rollback_keeps_backups_until_it_succeeds(inst: Installer, tmp_path: Path, library: Path,
                                                             base_game: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    before = read_manifest(str(base_game))
    real_move = _fsutil.move_file
    real_rename = _fsutil.rename_with_retry

    def fail_on_new(src: str, dst: str) -> None:
        if dst.endswith("new.txt"):
            raise OSError(5, "simulated failure")
        real_move(src, dst)

    def locked_restore(src: str, dst: str, **kwargs: Any) -> None:
        # Restoring a.txt from the backup fails: e.g. the game was started meanwhile.
        if os.sep + _overlay.BACKUP_SUBDIR + os.sep in src and dst.endswith("a.txt"):
            raise PermissionError(13, "locked")
        real_rename(src, dst, **kwargs)

    monkeypatch.setattr(_fsutil, "move_file", fail_on_new)
    monkeypatch.setattr(_fsutil, "rename_with_retry", locked_restore)
    patch = make_zip(tmp_path / "patch.zip", PATCH_FILES)
    with pytest.raises(InstallError, match="could not be updated"):
        inst.install(request(patch, library, kind=DownloadKind.PATCH, existing=str(base_game)), token=CancelToken())
    (session,) = staging_entries(library)
    assert (session / _overlay.BACKUP_SUBDIR / "a.txt").read_bytes() == b"old a", "the original is not deleted"
    assert (base_game / "b.txt").read_bytes() == b"old b"

    monkeypatch.setattr(_fsutil, "move_file", real_move)
    monkeypatch.setattr(_fsutil, "rename_with_retry", real_rename)
    install_other_game(inst, tmp_path, library)
    assert_base_state(base_game, before)
    assert staging_entries(library) == []

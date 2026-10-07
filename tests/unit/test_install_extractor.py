"""Tests for the extractor facade: type detection, backend selection and the zip fallback."""

from __future__ import annotations

import io
import itertools
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from anker_client.core.errors import (
    CorruptArchiveError,
    ExtractionError,
    OperationCancelled,
    PasswordProtectedArchiveError,
    SevenZipNotFoundError,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.install import _fsutil, extractor
from anker_client.services.install.extractor import Extractor, detect_archive_type
from anker_client.services.install.sevenzip import SevenZip

SEVEN_ZIP = SevenZip.locate()
needs_7z = pytest.mark.skipif(SEVEN_ZIP is None, reason="7-Zip is not installed")
HTML_MESSAGE = "The server sent a web page instead of the game archive."


def make_zip(path: Path, files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in files.items():
            if name.endswith("/"):
                zf.writestr(zipfile.ZipInfo(name), b"")
            else:
                zf.writestr(name, data)
    return path


@pytest.fixture
def builtin(monkeypatch: pytest.MonkeyPatch) -> Extractor:
    """An extractor that cannot find 7-Zip (zip fallback only)."""
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))
    return Extractor(lambda: "")


@pytest.fixture
def with_7z() -> Extractor:
    assert SEVEN_ZIP is not None
    return Extractor(lambda: SEVEN_ZIP or "")


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"PK\x03\x04rest", "zip"),
        (b"PK\x05\x06" + b"\0" * 18, "zip"),
        (b"PK\x07\x08rest", "zip"),
        (b"7z\xbc\xaf\x27\x1c\x00\x04", "7z"),
        (b"Rar!\x1a\x07\x00rest", "rar"),
        (b"Rar!\x1a\x07\x01\x00rest", "rar"),
        (b"<!DOCTYPE html><html>", "html"),
        (b"\xef\xbb\xbf  \r\n<!doctype html>", "html"),
        (b"\n\n   <HTML lang=en>", "html"),
        (b"<head><title>Just a moment...</title>", "html"),
        (b'{"error": "expired"}', "unknown"),
        (b"", "unknown"),
    ],
)
def test_detect_archive_type(tmp_path: Path, head: bytes, expected: str) -> None:
    path = tmp_path / "Hollow-Knight-AnkerGames.zip"
    path.write_bytes(head)
    assert detect_archive_type(str(path)) == expected


def test_detect_missing_file(tmp_path: Path) -> None:
    assert detect_archive_type(str(tmp_path / "nope.zip")) == "unknown"


def test_detect_ignores_extension(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "game.rar", {"a.txt": b"a"})
    assert detect_archive_type(str(path)) == "zip"


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------


def test_available_backend_builtin(builtin: Extractor) -> None:
    assert builtin.available_backend() == "built-in (zip only)"


@needs_7z
def test_available_backend_7z(with_7z: Extractor) -> None:
    assert with_7z.available_backend().startswith("7-Zip ")


def test_available_backend_broken_7z(tmp_path: Path) -> None:
    fake = tmp_path / "7z.exe"
    fake.write_bytes(b"not a program")
    assert Extractor(lambda: str(fake)).available_backend() == "built-in (zip only)"


def test_seven_zip_path_resolved_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str | None] = []
    exe = tmp_path / "7z.exe"
    exe.write_bytes(b"\0")

    def fake_locate(configured: str | None = None) -> str | None:
        calls.append(configured)
        return configured

    monkeypatch.setattr(SevenZip, "locate", staticmethod(fake_locate))
    configured = {"value": ""}
    ex = Extractor(lambda: configured["value"])
    assert ex._seven_zip() is None
    configured["value"] = str(exe)
    first = ex._seven_zip()
    assert first is not None and first.exe_path == str(exe)
    assert ex._seven_zip() is first  # cached while the path is unchanged
    assert calls == [None, str(exe), str(exe)]


def test_broken_settings_callable_falls_back_to_autodetect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: seen.append(configured)))

    def broken() -> str:
        raise RuntimeError("settings unavailable")

    assert Extractor(broken)._seven_zip() is None
    assert seen == [None]


# ---------------------------------------------------------------------------
# HTML / unsupported
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_7z", [False, pytest.param(True, marks=needs_7z)])
def test_html_saved_as_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_7z: bool) -> None:
    if not use_7z:
        monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: None))
    ex = Extractor(lambda: (SEVEN_ZIP or "") if use_7z else "")
    page = tmp_path / "Hollow-Knight-AnkerGames.zip"
    page.write_text("<!DOCTYPE html>\n<html><body>Just a moment...</body></html>", encoding="utf-8")
    with pytest.raises(CorruptArchiveError) as info:
        ex.extract(str(page), str(tmp_path / "out"), token=CancelToken())
    assert info.value.user_message == HTML_MESSAGE
    with pytest.raises(CorruptArchiveError, match="web page"):
        ex.test(str(page), token=CancelToken())
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("magic", "name"), [(b"7z\xbc\xaf\x27\x1c\x00\x04", "game.7z"), (b"Rar!\x1a\x07\x01\x00", "g.rar")])
def test_7z_and_rar_need_7zip(builtin: Extractor, tmp_path: Path, magic: bytes, name: str) -> None:
    archive = tmp_path / name
    archive.write_bytes(magic + b"\0" * 64)
    with pytest.raises(SevenZipNotFoundError):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    with pytest.raises(SevenZipNotFoundError):
        builtin.test(str(archive), token=CancelToken())


def test_unknown_garbage_without_7zip(builtin: Extractor, tmp_path: Path) -> None:
    archive = tmp_path / "game.zip"
    archive.write_bytes(b"\x00\x01garbage" * 100)
    with pytest.raises(CorruptArchiveError):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())


def test_missing_archive(builtin: Extractor, tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="could not be found"):
        builtin.extract(str(tmp_path / "missing.zip"), str(tmp_path / "out"), token=CancelToken())


def test_cancelled_before_start(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"a"})
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        builtin.extract(str(archive), str(tmp_path / "out"), token=token)


# ---------------------------------------------------------------------------
# zip fallback
# ---------------------------------------------------------------------------


def test_zip_fallback_extracts_nested_tree_with_monotonic_progress(builtin: Extractor, tmp_path: Path) -> None:
    files = {
        "Hollow Knight/": b"",
        "Hollow Knight/hollow_knight.exe": b"\0" * 200_000,
        "Hollow Knight/hollow_knight_Data/level.dat": os.urandom(3 * 1024 * 1024 + 17),
        "Hollow Knight/empty dir/": b"",
        "Read Me.txt": b"read me",
    }
    archive = make_zip(tmp_path / "Hollow-Knight-AnkerGames.zip", files)
    progress: list[float] = []
    out = tmp_path / "out"
    builtin.extract(str(archive), str(out), token=CancelToken(), on_progress=progress.append)
    assert (out / "Hollow Knight" / "hollow_knight.exe").read_bytes() == files["Hollow Knight/hollow_knight.exe"]
    assert (out / "Hollow Knight" / "hollow_knight_Data" / "level.dat").read_bytes() == \
        files["Hollow Knight/hollow_knight_Data/level.dat"]
    assert (out / "Hollow Knight" / "empty dir").is_dir()
    assert (out / "Read Me.txt").read_bytes() == b"read me"
    assert progress[0] == 0.0 and progress[-1] == 1.0
    assert all(b > a for a, b in itertools.pairwise(progress))
    assert len(progress) >= 4  # multi-MiB member reports per chunk


def test_zip_fallback_preserves_mtime(builtin: Extractor, tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("old.txt", date_time=(2001, 2, 3, 4, 5, 6))
        zf.writestr(info, b"x")
    builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    assert (tmp_path / "out" / "old.txt").stat().st_mtime < 1_100_000_000


@pytest.mark.parametrize(
    "evil",
    ["../evil.txt", "Game/../../evil.txt", "/abs.txt", "C:/Windows/evil.txt", "C:evil.txt", "a/file.txt:stream",
     "Game/.../x.txt", "\\\\server\\share\\x.txt"],
)
def test_zip_slip_guard(builtin: Extractor, tmp_path: Path, evil: str) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game/good.txt", b"good")
        info = zipfile.ZipInfo("placeholder")
        info.filename = evil  # bypass ZipInfo's own normalisation
        zf.writestr(info, b"evil")
    out = tmp_path / "nested" / "out"
    with pytest.raises(ExtractionError, match="unsafe"):
        builtin.extract(str(archive), str(out), token=CancelToken())
    assert not (out / "Game" / "good.txt").exists(), "nothing may be written when one member is unsafe"
    assert not list(tmp_path.rglob("evil.txt"))


def test_zip_encrypted_flag(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "locked.zip", {"Game/secret.bin": b"data"}, compression=zipfile.ZIP_STORED)
    data = bytearray(archive.read_bytes())
    # zipfile clears flag_bits on write: set the "encrypted" bit in the local (offset 6) and
    # central directory (offset 8) headers by hand.
    data[6] |= 0x1
    data[data.index(b"PK\x01\x02") + 8] |= 0x1
    archive.write_bytes(bytes(data))
    with pytest.raises(PasswordProtectedArchiveError):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    with pytest.raises(PasswordProtectedArchiveError):
        builtin.test(str(archive), token=CancelToken())


@needs_7z
def test_real_zipcrypto_archive_with_fallback(builtin: Extractor, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "game.exe").write_bytes(b"\0" * 1000)
    archive = tmp_path / "locked.zip"
    subprocess.run([SEVEN_ZIP, "a", "-tzip", "-pSECRET", str(archive), "game.exe"], cwd=tmp_path / "src",
                   check=True, capture_output=True, creationflags=_fsutil.CREATE_NO_WINDOW)
    with pytest.raises(PasswordProtectedArchiveError):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())


def _corrupt_member_data(archive: Path) -> None:
    data = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive) as zf:
        info = zf.infolist()[0]
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for index in range(start + 10, start + 200):
        data[index] ^= 0x5A
    archive.write_bytes(bytes(data))


def test_zip_crc_error(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bad.zip", {"Game/data.bin": os.urandom(5000)}, compression=zipfile.ZIP_STORED)
    _corrupt_member_data(archive)
    with pytest.raises(CorruptArchiveError):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    with pytest.raises(CorruptArchiveError):
        builtin.test(str(archive), token=CancelToken())


def test_zip_truncated(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "full.zip", {"Game/data.bin": os.urandom(50_000)})
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(archive.read_bytes()[:20_000])
    with pytest.raises(CorruptArchiveError):
        builtin.extract(str(truncated), str(tmp_path / "out"), token=CancelToken())


def test_zip_unsupported_method_asks_for_7zip(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "d64.zip", {"Game/a.bin": b"a" * 100}, compression=zipfile.ZIP_STORED)
    data = bytearray(archive.read_bytes())
    # Patch the method field (local header offset 8, central directory offset 10) to Deflate64 (9).
    data[8:10] = (9).to_bytes(2, "little")
    central = data.index(b"PK\x01\x02")
    data[central + 10:central + 12] = (9).to_bytes(2, "little")
    archive.write_bytes(bytes(data))
    with pytest.raises(SevenZipNotFoundError, match="needs 7-Zip"):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())


def test_prefixed_zip_detected_as_unknown_but_extracted(builtin: Extractor, tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Game/game.exe", b"\0" * 10)
    archive = tmp_path / "sfx.exe"
    archive.write_bytes(b"MZ-stub" + b"\0" * 100 + buffer.getvalue())
    assert detect_archive_type(str(archive)) == "unknown"
    builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    assert (tmp_path / "out" / "Game" / "game.exe").exists()


def test_zip_fallback_cancellation_mid_member(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "big.zip", {"Game/big.bin": os.urandom(4 * 1024 * 1024)},
                       compression=zipfile.ZIP_STORED)
    token = CancelToken()
    progress: list[float] = []

    def on_progress(fraction: float) -> None:
        progress.append(fraction)
        if fraction > 0:
            token.cancel()

    with pytest.raises(OperationCancelled):
        builtin.extract(str(archive), str(tmp_path / "out"), token=token, on_progress=on_progress)
    assert progress[-1] < 1.0


def test_zip_test_ok_and_progress(builtin: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"a.bin": os.urandom(2_500_000), "b/": b""})
    progress: list[float] = []
    builtin.test(str(archive), token=CancelToken(), on_progress=progress.append)
    assert progress[0] == 0.0 and progress[-1] == 1.0


def test_member_target_rules(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert extractor._member_target(root, "./") is None
    assert extractor._member_target(root, "a\\b.txt") == os.path.join(root, "a", "b.txt")
    assert extractor._member_target(root, "a/./b.txt") == os.path.join(root, "a", "b.txt")


def test_disk_full_while_writing(builtin: Extractor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = make_zip(tmp_path / "a.zip", {"Game/a.bin": b"a" * 100})

    def full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(extractor, "_extract_member", full_disk)
    with pytest.raises(ExtractionError, match="disk space"):
        builtin.extract(str(archive), str(tmp_path / "out"), token=CancelToken())


# ---------------------------------------------------------------------------
# with 7-Zip
# ---------------------------------------------------------------------------


@needs_7z
def test_zip_extracted_with_7zip(with_7z: Extractor, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "Game.zip", {"Game/game.exe": b"\0" * 5000, "Read Me.txt": b"x"})
    progress: list[float] = []
    with_7z.extract(str(archive), str(tmp_path / "out"), token=CancelToken(), on_progress=progress.append)
    assert (tmp_path / "out" / "Game" / "game.exe").stat().st_size == 5000
    assert progress[0] == 0.0 and progress[-1] == 1.0
    with_7z.test(str(archive), token=CancelToken())


@needs_7z
def test_7z_archive_with_7zip(with_7z: Extractor, tmp_path: Path) -> None:
    (tmp_path / "src" / "Game").mkdir(parents=True)
    (tmp_path / "src" / "Game" / "game.exe").write_bytes(b"\0" * 5000)
    archive = tmp_path / "Game.7z"
    subprocess.run([SEVEN_ZIP, "a", str(archive), "Game"], cwd=tmp_path / "src", check=True, capture_output=True,
                   creationflags=_fsutil.CREATE_NO_WINDOW)
    with_7z.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    assert (tmp_path / "out" / "Game" / "game.exe").exists()


@needs_7z
def test_unknown_garbage_with_7zip(with_7z: Extractor, tmp_path: Path) -> None:
    archive = tmp_path / "game.zip"
    archive.write_bytes(b"\x00\x01garbage" * 100)
    with pytest.raises(CorruptArchiveError):
        with_7z.extract(str(archive), str(tmp_path / "out"), token=CancelToken())


# ---------------------------------------------------------------------------
# review additions: limited 7-Zip builds, broken 7z.exe, Windows-invalid names
# ---------------------------------------------------------------------------

ARCHIVE_MAGIC = {"rar": b"Rar!\x1a\x07\x01\x00", "7z": b"7z\xbc\xaf\x27\x1c\x00\x04"}


def _archive_of_kind(tmp_path: Path, kind: str) -> Path:
    if kind == "zip":
        return make_zip(tmp_path / "game.zip", {"Game/game.exe": b"\0" * 10})
    path = tmp_path / f"game.{kind}"
    path.write_bytes(ARCHIVE_MAGIC[kind] + b"\0" * 64)
    return path


@pytest.mark.parametrize(
    ("exe_name", "kind", "expected"),
    [
        ("7za.exe", "rar", "unsupported"),  # 7-Zip Extra's 7za has no RAR codec
        ("7zr.exe", "rar", "unsupported"),
        ("7zr.exe", "zip", "builtin"),  # 7zr reads only .7z
        ("7za.exe", "zip", "7-zip"),
        ("7za.exe", "7z", "7-zip"),
        ("7zr.exe", "7z", "7-zip"),
        ("7z.exe", "rar", "7-zip"),
    ],
)
def test_limited_7zip_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exe_name: str, kind: str,
                             expected: str) -> None:
    exe = tmp_path / "bin" / exe_name
    exe.parent.mkdir()
    exe.write_bytes(b"\0")
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: str(exe)))
    calls: list[str] = []
    monkeypatch.setattr(SevenZip, "extract", lambda self, *_a, **_k: calls.append("extract"))
    monkeypatch.setattr(SevenZip, "test", lambda self, *_a, **_k: calls.append("test"))
    archive = _archive_of_kind(tmp_path, kind)
    ex = Extractor(lambda: "")
    out = tmp_path / "out"
    if expected == "unsupported":
        # Must never be CorruptArchiveError: the manager would delete and re-download a good archive.
        with pytest.raises(SevenZipNotFoundError, match="full 7-Zip") as info:
            ex.extract(str(archive), str(out), token=CancelToken())
        assert not isinstance(info.value, CorruptArchiveError)
        with pytest.raises(SevenZipNotFoundError):
            ex.test(str(archive), token=CancelToken())
        assert calls == []
    elif expected == "builtin":
        ex.extract(str(archive), str(out), token=CancelToken())
        ex.test(str(archive), token=CancelToken())
        assert (out / "Game" / "game.exe").exists()
        assert calls == []
    else:
        ex.extract(str(archive), str(out), token=CancelToken())
        ex.test(str(archive), token=CancelToken())
        assert calls == ["extract", "test"]


@pytest.mark.skipif(os.name != "nt", reason="a non-PE 7z.exe fails to start only on Windows")
def test_7zip_that_cannot_start_falls_back_for_zip(tmp_path: Path) -> None:
    broken = tmp_path / "7-Zip" / "7z.exe"
    broken.parent.mkdir()
    broken.write_bytes(b"not a program")
    ex = Extractor(lambda: str(broken))
    archive = make_zip(tmp_path / "game.zip", {"Game/game.exe": b"\0" * 10})
    ex.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
    assert (tmp_path / "out" / "Game" / "game.exe").exists()
    ex.test(str(archive), token=CancelToken())
    seven = _archive_of_kind(tmp_path, "7z")
    with pytest.raises(SevenZipNotFoundError):
        ex.extract(str(seven), str(tmp_path / "out7"), token=CancelToken())


ODD_NAMES = {
    "Game/CON.txt": "Game/_CON.txt",
    "Game/aux": "Game/_aux",
    "Game/nul.tar.gz": "Game/_nul.tar.gz",
    "Game/COM1": "Game/_COM1",
    "Game/a?b*c.txt": "Game/a_b_c.txt",
    'Game/q<x>|".txt': "Game/q_x___.txt",
    "Game /x.txt": "Game_/x.txt",
    "Game./y.txt": "Game_/y.txt",
    "Game/console.txt": "Game/console.txt",  # only exact device names are reserved
    "Game/ok.txt": "Game/ok.txt",
}


def _odd_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in ODD_NAMES:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name  # bypass ZipInfo's own normalisation
            zf.writestr(info, name.encode())
    return path


def _relative_files(root: Path) -> set[str]:
    base = _fsutil.long_path(str(root))
    found: set[str] = set()
    for directory, _dirs, files in os.walk(base):
        for name in files:
            found.add(os.path.relpath(os.path.join(directory, name), base).replace("\\", "/"))
    return found


def test_zip_fallback_maps_windows_invalid_names(builtin: Extractor, tmp_path: Path) -> None:
    out = tmp_path / "out"
    builtin.extract(str(_odd_zip(tmp_path / "odd.zip")), str(out), token=CancelToken())
    assert _relative_files(out) == set(ODD_NAMES.values())
    # Every file is reachable through ordinary (non-extended) paths, i.e. by Explorer and the game.
    for original, mapped in ODD_NAMES.items():
        assert (out / mapped).read_bytes() == original.encode()


@needs_7z
def test_windows_name_mapping_matches_7zip(tmp_path: Path) -> None:
    archive = _odd_zip(tmp_path / "odd.zip")
    assert SEVEN_ZIP is not None
    SevenZip(SEVEN_ZIP).extract(str(archive), str(tmp_path / "by7z"), token=CancelToken())
    extractor._extract_zip(str(archive), str(tmp_path / "builtin"), token=CancelToken(), on_progress=None)
    assert _relative_files(tmp_path / "builtin") == _relative_files(tmp_path / "by7z")


@pytest.mark.parametrize(
    ("version", "magic", "uses_7zip"),
    [
        ("7-Zip 15.05", b"Rar!\x1a\x07\x01\x00", False),  # RAR5 needs 7-Zip 15.06+
        ("7-Zip 15.06", b"Rar!\x1a\x07\x01\x00", True),
        ("7-Zip 24.09", b"Rar!\x1a\x07\x01\x00", True),
        ("7-Zip 15.05", b"Rar!\x1a\x07\x00", True),  # RAR4 works with any supported 7-Zip
        ("7-Zip", b"Rar!\x1a\x07\x01\x00", True),  # unknown version: let 7-Zip try
    ],
)
def test_rar5_needs_a_recent_7zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, magic: bytes,
                                  uses_7zip: bool) -> None:
    exe = tmp_path / "7-Zip" / "7z.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"\0")
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: str(exe)))
    monkeypatch.setattr(SevenZip, "version", lambda self: version)
    calls: list[str] = []
    monkeypatch.setattr(SevenZip, "extract", lambda self, *_a, **_k: calls.append("extract"))
    archive = tmp_path / "game.rar"
    archive.write_bytes(magic + b"\0" * 64)
    ex = Extractor(lambda: "")
    if uses_7zip:
        ex.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
        assert calls == ["extract"]
    else:
        with pytest.raises(SevenZipNotFoundError, match=r"15\.06") as info:
            ex.extract(str(archive), str(tmp_path / "out"), token=CancelToken())
        assert not isinstance(info.value, CorruptArchiveError)
        assert calls == []


@pytest.mark.parametrize("version", ["7-Zip 9.20", "7-Zip 15.03"])
def test_too_old_7zip_is_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    # 7-Zip before 15.05 rejects -bsp1/-bse1 ("Incorrect command line"): zips use the built-in
    # extractor, other archives ask for an update instead of failing with a generic error.
    exe = tmp_path / "7-Zip" / "7z.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"\0")
    monkeypatch.setattr(SevenZip, "locate", staticmethod(lambda configured=None: str(exe)))
    monkeypatch.setattr(SevenZip, "version", lambda self: version)
    calls: list[str] = []
    monkeypatch.setattr(SevenZip, "extract", lambda self, *_a, **_k: calls.append("extract"))
    ex = Extractor(lambda: "")
    zipped = make_zip(tmp_path / "game.zip", {"Game/game.exe": b"\0" * 10})
    ex.extract(str(zipped), str(tmp_path / "out"), token=CancelToken())
    assert (tmp_path / "out" / "Game" / "game.exe").exists()
    seven = tmp_path / "game.7z"
    seven.write_bytes(ARCHIVE_MAGIC["7z"] + b"\0" * 64)
    with pytest.raises(SevenZipNotFoundError, match="too old"):
        ex.extract(str(seven), str(tmp_path / "out7"), token=CancelToken())
    assert calls == []
    assert ex.available_backend() == "built-in (zip only)"

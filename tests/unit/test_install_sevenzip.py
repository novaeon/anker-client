"""Tests for the 7-Zip wrapper: output parsing, error mapping, discovery and real 7z.exe runs."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from anker_client.core.errors import (
    CorruptArchiveError,
    ExtractionError,
    OperationCancelled,
    PasswordProtectedArchiveError,
    SevenZipNotFoundError,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.install import _fsutil, sevenzip
from anker_client.services.install.sevenzip import (
    SevenZip,
    _classify_failure,
    _OutputParser,
    _parse_listing,
    _parse_version,
)

SEVEN_ZIP = SevenZip.locate()
needs_7z = pytest.mark.skipif(SEVEN_ZIP is None, reason="7-Zip is not installed")

# Recorded from 7-Zip 26.01 (``x … -bsp1 -bso0 -bse1``) while extracting through a pipe.
MODERN_STREAM = (
    b"  0M Scan\r         \r  0%\r    \r  2% 1 - src2\\G\\f0.bin\r                      \r"
    b"  5% 1 - src2\\G\\f0.bin\r                      \r 27% 2 - src2\\G\\f1.bin\r"
    b"                      \r 88% 4 - src2\\G\\f3.bin\r                      \r100%\r    \r"
)
# Older 7-Zip releases erase the progress line with backspaces instead of CR.
LEGACY_STREAM = (
    b"  0%\b\b\b\b    \b\b\b\b  7% 1 - Game\\data.pak\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b"
    b"                    \b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b 63% 2 - Game\\Game.exe"
    b"\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b                   \b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b"
)
ERROR_STREAM = (
    b"  0M Scan\r         \r  0%\r    \rERROR: Data Error in encrypted file. Wrong password? : Game.exe\r\n"
    b"100% T Game.exe\r               \r"
)


def feed_all(chunks: list[bytes]) -> tuple[list[int], list[str]]:
    parser = _OutputParser()
    updates: list[int] = []
    for chunk in chunks:
        updates += parser.feed(chunk)
    updates += parser.close()
    return updates, list(parser.messages)


# ---------------------------------------------------------------------------
# output parser
# ---------------------------------------------------------------------------


class TestOutputParser:
    def test_modern_cr_stream(self) -> None:
        updates, messages = feed_all([MODERN_STREAM])
        assert updates == [0, 2, 5, 27, 88, 100]
        assert messages == []

    def test_legacy_backspace_stream(self) -> None:
        updates, messages = feed_all([LEGACY_STREAM])
        assert updates == [0, 7, 63]
        assert messages == []

    @pytest.mark.parametrize("stream", [MODERN_STREAM, LEGACY_STREAM, ERROR_STREAM])
    def test_byte_by_byte_feed_matches_single_feed(self, stream: bytes) -> None:
        whole = feed_all([stream])
        split = feed_all([stream[i:i + 1] for i in range(len(stream))])
        assert split == whole

    @pytest.mark.parametrize("cut", range(1, 40))
    def test_every_two_way_split(self, cut: int) -> None:
        assert feed_all([MODERN_STREAM[:cut], MODERN_STREAM[cut:]]) == feed_all([MODERN_STREAM])

    def test_error_lines_are_messages(self) -> None:
        updates, messages = feed_all([ERROR_STREAM])
        assert updates == [0, 100]
        assert messages == ["ERROR: Data Error in encrypted file. Wrong password? : Game.exe"]

    def test_unterminated_token_is_reported_immediately(self) -> None:
        parser = _OutputParser()
        assert parser.feed(b"\r    \r 42% 3 - Game\\big") == [42]
        assert parser.feed(b".bin") == []
        assert parser.feed(b"\r          \r 43% 3") == [43]

    def test_split_digits_are_not_reported_early(self) -> None:
        parser = _OutputParser()
        assert parser.feed(b"\r  1") == []
        assert parser.feed(b"2% 1 - a") == [12]

    def test_progress_is_monotonic_and_clamped(self) -> None:
        updates, _ = feed_all([b" 50%\r 30%\r 50%\r 150%\r"])
        assert updates == [50, 100]

    def test_utf8_split_across_chunks(self) -> None:
        text = "ERROR: Data Error : Spiel ü 日本\\file.bin\r\n".encode()
        cut = text.index("日".encode()) + 1  # inside a multi-byte sequence
        _, messages = feed_all([text[:cut], text[cut:]])
        assert messages == ["ERROR: Data Error : Spiel ü 日本\\file.bin"]

    def test_scan_lines_ignored(self) -> None:
        _, messages = feed_all([b"  0M Scan\r  12M Scan C:\\x\r"])
        assert messages == []


# ---------------------------------------------------------------------------
# error mapping / listing / version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "error_type", "message_part"),
    [
        (["ERROR: Data Error in encrypted file. Wrong password? : Game.exe"], PasswordProtectedArchiveError, ""),
        (["Cannot open encrypted archive. Wrong password?", "ERRORS:", "Headers Error"],
         PasswordProtectedArchiveError, ""),
        (["ERROR: Data Error : Game\\big.bin"], CorruptArchiveError, ""),
        (["ERROR: CRC Failed : Game\\x"], CorruptArchiveError, ""),
        (["ERRORS:", "Unexpected end of archive"], CorruptArchiveError, ""),
        (["Open ERROR: Cannot open the file as [zip] archive", "ERRORS:", "Is not archive"],
         CorruptArchiveError, ""),
        (["ERROR: There is not enough space on the disk : Game\\x"], ExtractionError, "disk space"),
        (["ERROR: Missing volume : game.7z.002"], ExtractionError, "parts"),
        (["ERROR: Unsupported Method : x"], ExtractionError, "Update 7-Zip"),
        (["ERROR: Can not open output file : Access is denied. : C:\\x"], ExtractionError, "permissions"),
        (["something odd"], ExtractionError, "could not be extracted"),
    ],
)
def test_classify_failure(lines: list[str], error_type: type, message_part: str) -> None:
    error = _classify_failure(2, lines)
    assert type(error) is error_type
    assert message_part in error.user_message
    assert "exit code 2" in error.detail
    assert lines[-1] in error.detail


@pytest.mark.parametrize(
    ("returncode", "lines"),
    [
        (7, ["Command Line Error:", "Unsupported switch:", "-bsp1"]),
        (7, []),
        (2, ["Error:", "Incorrect command line"]),  # 7-Zip 9.20 wording
    ],
)
def test_classify_command_line_errors_as_too_old(returncode: int, lines: list[str]) -> None:
    error = _classify_failure(returncode, lines)
    assert type(error) is SevenZipNotFoundError
    assert "too old" in error.user_message


def test_classify_out_of_memory() -> None:
    error = _classify_failure(8, [])
    assert type(error) is ExtractionError
    assert "memory" in error.user_message


SLT_7Z = """
7-Zip 26.01 (x64) : Copyright (c) 1999-2026 Igor Pavlov : 2026-04-27

Listing archive: test.7z

--
Path = test.7z
Type = 7z
Physical Size = 63118736

----------
Path = Game
Size = 0
Packed Size = 0
Attributes = D
CRC =

Path = Game\\big.bin
Size = 62914560
Attributes = A
CRC = 826EFEBE

Path = Game\\Game.exe
Size = 200002
Attributes = A -rwxr-xr-x
"""

SLT_ZIP = """
--
Path = a.zip
Type = zip

----------
Path = Hollow Knight
Folder = +
Size = 0

Path = Hollow Knight/Read Me.txt
Folder = -
Size = 12
Attributes = D_ drwxr-xr-x
"""


def test_parse_listing_7z() -> None:
    entries = _parse_listing(SLT_7Z)
    assert [(e.path, e.size, e.is_dir) for e in entries] == [
        ("Game", 0, True),
        ("Game\\big.bin", 62914560, False),
        ("Game\\Game.exe", 200002, False),
    ]


def test_parse_listing_zip_folder_flag_wins() -> None:
    entries = _parse_listing(SLT_ZIP)
    assert [(e.path, e.is_dir) for e in entries] == [("Hollow Knight", True), ("Hollow Knight/Read Me.txt", False)]


def test_parse_listing_without_entries() -> None:
    assert _parse_listing("7-Zip banner only\n") == []


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        ("\n7-Zip 26.01 (x64) : Copyright (c) 1999-2026 Igor Pavlov", "7-Zip 26.01"),
        ("7-Zip (a) 23.01 (x64) : Copyright", "7-Zip 23.01"),
        ("7-Zip [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov", "7-Zip 16.02"),
        ("p7zip Version 16.02 (locale=utf8,Utf16=on)", "p7zip 16.02"),
        ("nothing useful", ""),
    ],
)
def test_parse_version(banner: str, expected: str) -> None:
    assert _parse_version(banner) == expected


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def no_system_7z(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Hide every real 7-Zip installation; returns an empty fake Program Files."""
    program_files = tmp_path / "ProgramFiles"
    program_files.mkdir()
    monkeypatch.setattr(sevenzip, "_registry_install_dirs", lambda: [])
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.setenv(env, str(program_files))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(sevenzip.shutil, "which", lambda _cmd: None)
    return program_files


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    return path


def test_locate_configured_file(no_system_7z: Path, tmp_path: Path) -> None:
    exe = _touch(tmp_path / "tools" / "7z.exe")
    assert SevenZip.locate(str(exe)) == str(exe)
    assert SevenZip.locate(f'"{exe}"') == str(exe)


def test_locate_configured_directory(no_system_7z: Path, tmp_path: Path) -> None:
    exe = _touch(tmp_path / "tools" / "7za.exe")
    assert SevenZip.locate(str(exe.parent)) == str(exe)


def test_locate_missing_configured_falls_back(no_system_7z: Path) -> None:
    exe = _touch(no_system_7z / "7-Zip" / "7z.exe")
    assert SevenZip.locate(r"Z:\nowhere\7z.exe") == str(exe)


def test_locate_prefers_registry(no_system_7z: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(no_system_7z / "7-Zip" / "7z.exe")
    registry_exe = _touch(tmp_path / "Reg7z" / "7z.exe")
    monkeypatch.setattr(sevenzip, "_registry_install_dirs", lambda: [str(registry_exe.parent)])
    assert SevenZip.locate() == str(registry_exe)


def test_locate_scoop_and_path(no_system_7z: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scoop = _touch(tmp_path / "profile" / "scoop" / "apps" / "7zip" / "current" / "7z.exe")
    assert SevenZip.locate() == str(scoop)
    scoop.unlink()
    on_path = _touch(tmp_path / "bin" / "7za.exe")
    monkeypatch.setattr(sevenzip.shutil, "which", lambda cmd: str(on_path) if cmd == "7za" else None)
    assert SevenZip.locate() == str(on_path)


def test_locate_nothing(no_system_7z: Path) -> None:
    assert SevenZip.locate() is None
    assert SevenZip.locate("") is None


def test_constructor_rejects_missing_exe(tmp_path: Path) -> None:
    with pytest.raises(SevenZipNotFoundError):
        SevenZip(str(tmp_path / "7z.exe"))
    with pytest.raises(SevenZipNotFoundError):
        SevenZip("")


def test_registry_lookup_does_not_crash() -> None:
    assert isinstance(sevenzip._registry_install_dirs(), list)


@pytest.mark.parametrize("gui", ["7zFM.exe", "7zG.exe", "7ZFM.EXE"])
def test_locate_configured_gui_program_uses_console_7z(no_system_7z: Path, tmp_path: Path, gui: str) -> None:
    # Running the GUI file manager would open a window and never return.
    console = _touch(tmp_path / "7-Zip" / "7z.exe")
    gui_exe = _touch(tmp_path / "7-Zip" / gui)
    assert SevenZip.locate(str(gui_exe)) == str(console)


def test_constructor_never_runs_gui_programs(tmp_path: Path) -> None:
    console = _touch(tmp_path / "7-Zip" / "7z.exe")
    assert SevenZip(str(_touch(tmp_path / "7-Zip" / "7zG.exe"))).exe_path == str(console)
    with pytest.raises(SevenZipNotFoundError, match="window programs"):
        SevenZip(str(_touch(tmp_path / "Lonely" / "7zFM.exe")))


def test_locate_configured_gui_program_without_console_falls_back(no_system_7z: Path, tmp_path: Path) -> None:
    gui_exe = _touch(tmp_path / "Lonely" / "7zFM.exe")
    detected = _touch(no_system_7z / "7-Zip" / "7z.exe")
    assert SevenZip.locate(str(gui_exe)) == str(detected)


def test_list_timeout_kills_7z_and_closes_pipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seven = SevenZip(str(_touch(tmp_path / "7z.exe")))
    archive = _touch(tmp_path / "game.7z")
    spawned: list[subprocess.Popen[bytes]] = []

    def hanging_spawn(_args: list[str]) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_fsutil.CREATE_NO_WINDOW)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(seven, "_spawn", hanging_spawn)
    monkeypatch.setattr(sevenzip, "_LIST_TIMEOUT", 0.5)
    with pytest.raises(ExtractionError, match="did not respond"):
        seven.list(str(archive))
    (proc,) = spawned
    assert proc.poll() is not None, "the hung process is killed"
    assert proc.stdout is not None and proc.stdout.closed
    assert proc.stderr is not None and proc.stderr.closed


# ---------------------------------------------------------------------------
# process tree termination
# ---------------------------------------------------------------------------


def test_kill_process_tree_kills_grandchildren() -> None:
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            creationflags=_fsutil.CREATE_NO_WINDOW)
    try:
        assert proc.stdout is not None
        child_pid = int(proc.stdout.readline())
        child = psutil.Process(child_pid)
        _fsutil.kill_process_tree(proc)
        proc.wait(timeout=10)
        child.wait(timeout=10)
        assert not child.is_running()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


def test_kill_process_tree_on_finished_process_is_noop() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=_fsutil.CREATE_NO_WINDOW)
    proc.wait(timeout=30)
    _fsutil.kill_process_tree(proc)  # must not touch a recycled pid
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# real 7z.exe
# ---------------------------------------------------------------------------


def run_7z(*args: str, cwd: Path) -> None:
    subprocess.run([SEVEN_ZIP, *args], cwd=cwd, check=True, capture_output=True,
                   creationflags=_fsutil.CREATE_NO_WINDOW)


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "Game" / "Data").mkdir(parents=True)
    (src / "Game" / "Game.exe").write_bytes(b"\0" * 150_000)
    (src / "Game" / "Data" / "level.pak").write_bytes(os.urandom(300_000))
    (src / "Game" / "Spiel ü 日本.txt").write_text("unicode", encoding="utf-8")
    return src


@pytest.fixture
def seven(tmp_path: Path) -> SevenZip:
    assert SEVEN_ZIP is not None
    return SevenZip(SEVEN_ZIP)


@needs_7z
def test_real_version(seven: SevenZip) -> None:
    version = seven.version()
    assert version.startswith("7-Zip")
    assert seven.version() is version  # cached


@needs_7z
def test_real_extract_list_test(seven: SevenZip, sample_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "game.7z"
    run_7z("a", "-mx1", str(archive), "Game", cwd=sample_tree)
    progress: list[float] = []
    out = tmp_path / "out dir"
    seven.extract(str(archive), str(out), token=CancelToken(), on_progress=progress.append)
    assert (out / "Game" / "Game.exe").stat().st_size == 150_000
    assert (out / "Game" / "Spiel ü 日本.txt").read_text(encoding="utf-8") == "unicode"
    assert progress[0] == 0.0 and progress[-1] == 1.0
    assert progress == sorted(set(progress))

    entries = {e.path.replace("\\", "/"): e for e in seven.list(str(archive))}
    assert entries["Game"].is_dir
    assert entries["Game/Data/level.pak"].size == 300_000
    assert "Game/Spiel ü 日本.txt" in entries

    test_progress: list[float] = []
    seven.test(str(archive), token=CancelToken(), on_progress=test_progress.append)
    assert test_progress[-1] == 1.0


@needs_7z
def test_real_password_protected(seven: SevenZip, sample_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "secret.7z"
    run_7z("a", "-pSECRET", str(archive), "Game", cwd=sample_tree)
    with pytest.raises(PasswordProtectedArchiveError):
        seven.extract(str(archive), str(tmp_path / "x1"), token=CancelToken())
    with pytest.raises(PasswordProtectedArchiveError):
        seven.test(str(archive), token=CancelToken())
    seven.extract(str(archive), str(tmp_path / "x2"), token=CancelToken(), password="SECRET")
    assert (tmp_path / "x2" / "Game" / "Game.exe").exists()


@needs_7z
def test_real_encrypted_headers(seven: SevenZip, sample_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "headers.7z"
    run_7z("a", "-pSECRET", "-mhe=on", str(archive), "Game", cwd=sample_tree)
    with pytest.raises(PasswordProtectedArchiveError):
        seven.extract(str(archive), str(tmp_path / "x"), token=CancelToken())
    with pytest.raises(PasswordProtectedArchiveError):
        seven.list(str(archive))


@needs_7z
def test_real_corrupt_and_truncated(seven: SevenZip, sample_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "game.7z"
    run_7z("a", "-mx1", str(archive), "Game", cwd=sample_tree)
    data = bytearray(archive.read_bytes())
    for index in range(64, len(data) - 64, 997):
        data[index] ^= 0xFF
    corrupt = tmp_path / "corrupt.7z"
    corrupt.write_bytes(bytes(data))
    with pytest.raises(CorruptArchiveError):
        seven.extract(str(corrupt), str(tmp_path / "c"), token=CancelToken())
    with pytest.raises(CorruptArchiveError):
        seven.test(str(corrupt), token=CancelToken())
    truncated = tmp_path / "truncated.7z"
    truncated.write_bytes(archive.read_bytes()[: len(data) // 2])
    with pytest.raises(CorruptArchiveError):
        seven.extract(str(truncated), str(tmp_path / "t"), token=CancelToken())


@needs_7z
def test_real_not_an_archive_and_missing(seven: SevenZip, tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.zip"
    bogus.write_bytes(b"this is not an archive at all" * 10)
    with pytest.raises(CorruptArchiveError):
        seven.extract(str(bogus), str(tmp_path / "b"), token=CancelToken())
    with pytest.raises(ExtractionError, match="could not be found"):
        seven.extract(str(tmp_path / "missing.7z"), str(tmp_path / "m"), token=CancelToken())


@needs_7z
def test_real_cancel_before_start_spawns_nothing(seven: SevenZip, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c")
    token = CancelToken()
    token.cancel()
    monkeypatch.setattr(sevenzip.subprocess, "Popen", None)  # would explode if called
    with pytest.raises(OperationCancelled):
        seven.extract(str(archive), str(tmp_path / "o"), token=token)


@pytest.fixture(scope="module")
def slow_archive(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """BZip2 over random data: ~1 s to build, several seconds to extract. Returns (archive, sha256)."""
    if SEVEN_ZIP is None:
        pytest.skip("7-Zip is not installed")
    base = tmp_path_factory.mktemp("slow")
    payload = os.urandom(16 * 1024 * 1024)
    (base / "Game").mkdir()
    (base / "Game" / "payload.bin").write_bytes(payload)
    run_7z("a", "-m0=BZip2", str(base / "slow.7z"), "Game", cwd=base)
    (base / "Game" / "payload.bin").unlink()
    return base / "slow.7z", hashlib.sha256(payload).hexdigest()


@needs_7z
def test_real_cancel_mid_extraction_terminates_7z(
    seven: SevenZip, slow_archive: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, digest = slow_archive
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sevenzip.subprocess, "Popen", recording_popen)
    token = CancelToken()
    progress: list[float] = []

    def on_progress(fraction: float) -> None:
        progress.append(fraction)
        if fraction > 0:
            token.cancel()

    timer = threading.Timer(1.0, token.cancel)  # safety net if no progress arrives
    timer.start()
    out = tmp_path / "out"
    started = time.monotonic()
    try:
        with pytest.raises(OperationCancelled):
            seven.extract(str(archive), str(out), token=token, on_progress=on_progress)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    assert len(spawned) == 1
    assert spawned[0].poll() is not None, "7z.exe must be terminated"
    assert not psutil.pid_exists(spawned[0].pid) or psutil.Process(spawned[0].pid).name().lower() != "7z.exe"
    # 7-Zip pre-allocates the output file, so compare contents rather than sizes.
    partial = out / "Game" / "payload.bin"
    assert not partial.exists() or hashlib.sha256(partial.read_bytes()).hexdigest() != digest
    assert progress == sorted(progress)

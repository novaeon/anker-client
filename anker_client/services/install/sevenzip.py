"""7-Zip command-line wrapper.

* ``locate(configured)``: the configured path if it exists (a folder holding
  ``7z.exe`` is accepted too; the GUI programs ``7zFM.exe``/``7zG.exe`` are
  replaced by the ``7z.exe`` next to them, also by the ``SevenZip`` constructor,
  which raises ``SevenZipNotFoundError`` when there is none), else ``HKLM/HKCU\\SOFTWARE\\7-Zip``
  ``Path``/``Path64`` registry values (64- and 32-bit views),
  ``%ProgramFiles%\\7-Zip\\7z.exe``, ``%ProgramW6432%\\7-Zip\\7z.exe``,
  ``%ProgramFiles(x86)%\\7-Zip\\7z.exe``,
  ``%USERPROFILE%\\scoop\\apps\\7zip\\current\\7z.exe``, ``7z``/``7za``/``7zz`` on PATH.
  Returns ``None`` when nothing is found.
* ``extract``: ``7z x -o<dest> -y -bsp1 -bso0 -bse1 -p<password or ""> -sccUTF-8 -- <archive>``.
  With ``-bse1`` error messages share stdout with the progress stream. The
  progress stream is a sequence of fragments such as ``"  12% 3 - path"``
  separated by ``\\b`` backspaces (old versions) or ``\\r`` (new versions); it is
  read incrementally from the pipe (never line-by-line) and parsed by
  :class:`_OutputParser`; ``NN%`` tokens are reported as a monotonic 0..1
  fraction, everything else is kept as message lines for error mapping.
  Runs with ``CREATE_NO_WINDOW``. Cancel → kill the process tree, raise
  ``OperationCancelled``. Exit code 0 = ok, 1 = warnings (ok, logged),
  anything else is fatal and mapped from the collected messages + stderr:
  exit code 7 / "Incorrect command line"/"Unsupported switch" (7-Zip older
  than ``MIN_VERSION`` = 15.05 does not know ``-bs*``) → ``SevenZipNotFoundError``
  asking for an update;
  "Wrong password"/"Cannot open encrypted archive" → ``PasswordProtectedArchiveError``;
  "not enough space"/"There is not enough space" → ``ExtractionError`` with a
  disk-space message; "Data Error"/"CRC Failed"/"Unexpected end"/"Headers Error"/
  "Is not archive"/"Cannot open the file as archive" → ``CorruptArchiveError``;
  "Missing volume" → ``ExtractionError`` (split archive incomplete);
  otherwise ``ExtractionError`` with the last messages as ``detail``.
* ``test``: ``7z t`` with the same progress/cancel/error handling.
* ``list``: ``7z l -slt`` parsed into ``ArchiveEntry`` objects.
* ``version``: parsed from the banner of ``7z i`` (e.g. "7-Zip 24.09"); cached.
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import IO

from anker_client.core.errors import (
    CorruptArchiveError,
    ExtractionError,
    OperationCancelled,
    PasswordProtectedArchiveError,
    SevenZipNotFoundError,
)
from anker_client.core.models import ArchiveEntry
from anker_client.core.tasks import CancelToken
from anker_client.services.install._fsutil import CREATE_NO_WINDOW, IS_WINDOWS, kill_process_tree

log = logging.getLogger(__name__)

_EXE_NAMES = ("7z.exe", "7za.exe", "7zz.exe") if IS_WINDOWS else ("7z", "7za", "7zz")
_PATH_COMMANDS = ("7z", "7za", "7zz")
_GUI_EXE_NAMES = frozenset({"7zfm.exe", "7zg.exe"})

_PROGRESS_RE = re.compile(r"^\s*(\d{1,3})%")
_SCAN_RE = re.compile(r"^\d+M Scan\b")
_SEPARATOR_RE = re.compile(r"[\b\r\n]")
_VERSION_RE = re.compile(r"7-Zip(?:\s*\([a-z]\))?(?:\s*\[\d+\])?\s+(\d+(?:\.\d+)*)", re.IGNORECASE)
_P7ZIP_VERSION_RE = re.compile(r"p7zip\s+Version\s+(\d+(?:\.\d+)*)", re.IGNORECASE)

_PASSWORD_MARKERS = ("wrong password", "cannot open encrypted archive", "can not open encrypted archive")
_DISK_SPACE_MARKERS = ("not enough space", "there is not enough space", "disk is full", "disk full")
_CORRUPT_MARKERS = (
    "data error",
    "crc failed",
    "unexpected end",
    "headers error",
    "is not archive",
    "cannot open the file as",
    "can not open the file as",
)
_WRITE_MARKERS = ("can not open output file", "cannot open output file", "access is denied", "cannot create")
_COMMAND_LINE_MARKERS = ("incorrect command line", "unsupported switch", "command line error")
_EXIT_COMMAND_LINE = 7

#: 7-Zip versions before this do not support the ``-bs*`` output switches used here.
MIN_VERSION = (15, 5)
TOO_OLD_MESSAGE = "This 7-Zip version is too old for AnkerClient. Update 7-Zip or set its location in Settings."

_MAX_MESSAGES = 200
_VERSION_TIMEOUT = 15.0
_LIST_TIMEOUT = 300.0

ProgressCallback = Callable[[float], None]


# ---------------------------------------------------------------------------
# output parsing
# ---------------------------------------------------------------------------


class _OutputParser:
    """Incremental parser for 7-Zip's ``-bsp1 -bse1`` stdout.

    ``feed`` accepts arbitrary byte chunks (a fragment, a UTF-8 sequence or a
    ``NN%`` token may be split across reads) and returns the new percentages
    (strictly increasing). Non-progress text is collected in ``messages``.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._percent = -1
        self.messages: deque[str] = deque(maxlen=_MAX_MESSAGES)

    @property
    def percent(self) -> int:
        return max(0, self._percent)

    def feed(self, data: bytes) -> list[int]:
        return self._consume(self._decoder.decode(data))

    def close(self) -> list[int]:
        updates = self._consume(self._decoder.decode(b"", final=True))
        tail, self._pending = self._pending, ""
        self._handle_fragment(tail, updates)
        return updates

    def _consume(self, text: str) -> list[int]:
        updates: list[int] = []
        if not text:
            return updates
        parts = _SEPARATOR_RE.split(self._pending + text)
        self._pending = parts.pop()
        for fragment in parts:
            self._handle_fragment(fragment, updates)
        # The token in an unterminated fragment is already complete once its "%"
        # arrived (digits precede it), so report it now instead of ~200 ms later.
        match = _PROGRESS_RE.match(self._pending)
        if match:
            self._advance(int(match.group(1)), updates)
        return updates

    def _handle_fragment(self, fragment: str, updates: list[int]) -> None:
        stripped = fragment.strip()
        if not stripped:
            return
        match = _PROGRESS_RE.match(fragment)
        if match:
            self._advance(int(match.group(1)), updates)
            return
        if _SCAN_RE.match(stripped):
            return
        self.messages.append(stripped)

    def _advance(self, value: int, updates: list[int]) -> None:
        value = min(100, value)
        if value > self._percent:
            self._percent = value
            updates.append(value)


class _ProgressReporter:
    """Forwards monotonic 0..1 fractions to an optional callback."""

    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._last = -1.0

    def __call__(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if self._callback is None or fraction <= self._last:
            return
        self._last = fraction
        self._callback(fraction)


def _classify_failure(returncode: int, lines: list[str]) -> ExtractionError:
    text = "\n".join(lines).casefold()
    detail = f"7-Zip exit code {returncode}: " + " | ".join(lines[-15:])
    if returncode == _EXIT_COMMAND_LINE or any(marker in text for marker in _COMMAND_LINE_MARKERS):
        # Versions before 15.x do not know -bsp1/-bse1/-scc: nothing was extracted.
        return SevenZipNotFoundError(TOO_OLD_MESSAGE, detail=detail)
    if any(marker in text for marker in _PASSWORD_MARKERS):
        return PasswordProtectedArchiveError(detail=detail)
    if any(marker in text for marker in _DISK_SPACE_MARKERS):
        return ExtractionError("There is not enough free disk space to extract the archive.", detail=detail)
    if "missing volume" in text:
        return ExtractionError("The archive is split into several parts and some parts are missing.", detail=detail)
    if any(marker in text for marker in _CORRUPT_MARKERS):
        return CorruptArchiveError(detail=detail)
    if "unsupported method" in text:
        return ExtractionError(
            "The archive uses a compression method this 7-Zip version does not support. Update 7-Zip.",
            detail=detail,
        )
    if returncode == 8:
        return ExtractionError("7-Zip ran out of memory while extracting the archive.", detail=detail)
    if any(marker in text for marker in _WRITE_MARKERS):
        return ExtractionError(
            "7-Zip could not write the extracted files. Check the folder permissions and your antivirus.",
            detail=detail,
        )
    return ExtractionError(detail=detail)


def _listing_entry_is_dir(fields: dict[str, str]) -> bool:
    if "Folder" in fields:  # zip/rar listings say it explicitly
        return fields["Folder"] == "+"
    # 7z listings: Windows attribute letters first, e.g. "D", "A", "RHA -rwxr-xr-x".
    return "D" in fields.get("Attributes", "").split(" ", 1)[0]


def _parse_listing(text: str) -> list[ArchiveEntry]:
    """Parse ``7z l -slt`` output (blocks of ``Key = Value`` after a ``----------`` line)."""
    entries: list[ArchiveEntry] = []
    _, separator, body = text.partition("\n----------")
    if not separator:
        return entries
    for block in re.split(r"\n\s*\n", body):
        fields: dict[str, str] = {}
        for raw in block.splitlines():
            key, sep, value = raw.partition(" = ")
            if not sep:
                key, sep, value = raw.partition(" =")
            if sep:
                fields[key.strip()] = value.strip()
        path = fields.get("Path")
        if not path:
            continue
        is_dir = _listing_entry_is_dir(fields)
        try:
            size = int(fields.get("Size") or 0)
        except ValueError:
            size = 0
        entries.append(ArchiveEntry(path=path, size=size, is_dir=is_dir))
    return entries


def _parse_version(text: str) -> str:
    for line in text.splitlines():
        match = _VERSION_RE.search(line)
        if match:
            return f"7-Zip {match.group(1)}"
        match = _P7ZIP_VERSION_RE.search(line)
        if match:
            return f"p7zip {match.group(1)}"
    return ""


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def _registry_install_dirs() -> list[str]:
    """``Path64``/``Path`` values of ``SOFTWARE\\7-Zip`` (HKLM and HKCU, both registry views)."""
    if not IS_WINDOWS:
        return []
    import winreg

    found: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\7-Zip", 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with key:
                for value_name in ("Path64", "Path"):
                    try:
                        value, _type = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip() and value not in found:
                        found.append(value.strip())
    return found


def _well_known_paths() -> list[str]:
    paths: list[str] = []
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            paths.append(os.path.join(base, "7-Zip", "7z.exe"))
    profile = os.environ.get("USERPROFILE")
    if profile:
        paths.append(os.path.join(profile, "scoop", "apps", "7zip", "current", "7z.exe"))
    return paths


def _candidate_paths() -> Iterator[str]:
    for directory in _registry_install_dirs():
        yield os.path.join(directory, "7z.exe")
    yield from _well_known_paths()
    for command in _PATH_COMMANDS:
        found = shutil.which(command)
        if found:
            yield found


def _console_exe_in(directory: str) -> str | None:
    for name in _EXE_NAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _resolve_configured(configured: str) -> str | None:
    path = os.path.expandvars(os.path.expanduser(configured.strip().strip('"')))
    if not path:
        return None
    if os.path.isfile(path):
        if os.path.basename(path).casefold() in _GUI_EXE_NAMES:
            # Users often browse to 7zFM.exe/7zG.exe: running those opens a window and never
            # returns, so use the console 7z.exe that ships next to them.
            return _console_exe_in(os.path.dirname(os.path.abspath(path)))
        return os.path.abspath(path)
    if os.path.isdir(path):
        return _console_exe_in(path)
    return None


def _redact(args: list[str]) -> list[str]:
    """Hide the archive password in logged command lines."""
    return ["-p***" if arg.startswith("-p") and len(arg) > 2 else arg for arg in args]


def _drain(stream: IO[bytes], sink: deque[str]) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    try:
        while chunk := stream.read1(65536):  # type: ignore[attr-defined]
            pending += decoder.decode(chunk)
            *lines, pending = re.split(r"[\r\n]+", pending)
            sink.extend(line.strip() for line in lines if line.strip())
    except (OSError, ValueError):
        pass
    tail = (pending + decoder.decode(b"", final=True)).strip()
    if tail:
        sink.append(tail)


# ---------------------------------------------------------------------------
# wrapper
# ---------------------------------------------------------------------------


class SevenZip:
    def __init__(self, exe_path: str) -> None:
        if not exe_path or not os.path.isfile(exe_path):
            raise SevenZipNotFoundError(detail=f"not a file: {exe_path!r}")
        if os.path.basename(exe_path).casefold() in _GUI_EXE_NAMES:
            # Never run the window programs (they would open a window and block): use 7z.exe.
            console = _console_exe_in(os.path.dirname(os.path.abspath(exe_path)))
            if console is None:
                raise SevenZipNotFoundError(
                    "7zFM.exe and 7zG.exe are 7-Zip's window programs. Choose 7z.exe in the same folder.",
                    detail=exe_path,
                )
            exe_path = console
        self._exe = os.path.abspath(exe_path)
        self._version: str | None = None
        self._version_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"SevenZip({self._exe!r})"

    @staticmethod
    def locate(configured: str | None = None) -> str | None:
        if configured:
            resolved = _resolve_configured(configured)
            if resolved:
                return resolved
            log.warning("Configured 7-Zip path %r does not exist; auto-detecting", configured)
        for candidate in _candidate_paths():
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    @property
    def exe_path(self) -> str:
        return self._exe

    def version(self) -> str:
        with self._version_lock:
            if self._version is None:
                self._version = self._read_version()
            return self._version

    def _read_version(self) -> str:
        try:
            completed = subprocess.run(
                [self._exe, "i"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_VERSION_TIMEOUT,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SevenZipNotFoundError(detail=f"{self._exe}: {exc}") from exc
        text = completed.stdout.decode("utf-8", errors="replace")
        version = _parse_version(text)
        if not version:
            log.warning("Unrecognised 7-Zip banner from %s: %r", self._exe, text[:200])
            return "7-Zip"
        return version

    # --- operations -----------------------------------------------------------------
    def extract(
        self,
        archive: str,
        dest_dir: str,
        *,
        token: CancelToken,
        on_progress: Callable[[float], None] | None = None,
        password: str = "",
    ) -> None:
        archive = self._existing_archive(archive)
        dest = os.path.abspath(dest_dir)
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            raise ExtractionError("The destination folder could not be created.", detail=f"{dest}: {exc}") from exc
        args = [self._exe, "x", f"-o{dest}", "-y", "-bsp1", "-bso0", "-bse1", f"-p{password}", "-sccUTF-8",
                "--", archive]
        self._run_with_progress(args, token=token, on_progress=on_progress)

    def test(
        self,
        archive: str,
        *,
        token: CancelToken,
        on_progress: Callable[[float], None] | None = None,
        password: str = "",
    ) -> None:
        archive = self._existing_archive(archive)
        args = [self._exe, "t", "-y", "-bsp1", "-bso0", "-bse1", f"-p{password}", "-sccUTF-8", "--", archive]
        self._run_with_progress(args, token=token, on_progress=on_progress)

    def list(self, archive: str, *, token: CancelToken | None = None) -> list[ArchiveEntry]:
        archive = self._existing_archive(archive)
        args = [self._exe, "l", "-slt", "-p", "-sccUTF-8", "--", archive]
        returncode, stdout, stderr = self._run_capture(args, token=token)
        text = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if returncode not in (0, 1):
            lines = [line.strip() for line in (text + "\n" + stderr.decode("utf-8", "replace")).splitlines()]
            raise _classify_failure(returncode, [line for line in lines if line])
        return _parse_listing(text)

    # --- process plumbing --------------------------------------------------------------
    @staticmethod
    def _existing_archive(archive: str) -> str:
        path = os.path.abspath(archive)
        if not os.path.isfile(path):
            raise ExtractionError("The archive file could not be found.", detail=path)
        return path

    def _spawn(self, args: list[str]) -> subprocess.Popen[bytes]:
        log.debug("Running 7-Zip: %s", " ".join(_redact(args[1:])))
        try:
            return subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise SevenZipNotFoundError(detail=f"{self._exe}: {exc}") from exc

    def _run_with_progress(
        self,
        args: list[str],
        *,
        token: CancelToken,
        on_progress: ProgressCallback | None,
    ) -> None:
        token.raise_if_cancelled()
        report = _ProgressReporter(on_progress)
        parser = _OutputParser()
        stderr_lines: deque[str] = deque(maxlen=_MAX_MESSAGES)
        proc = self._spawn(args)
        assert proc.stdout is not None and proc.stderr is not None
        drain = threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), name="7z-stderr", daemon=True)
        drain.start()
        unregister = token.on_cancel(lambda: kill_process_tree(proc))
        try:
            report(0.0)
            while chunk := proc.stdout.read1(65536):
                for percent in parser.feed(chunk):
                    report(percent / 100)
            for percent in parser.close():
                report(percent / 100)
            returncode = proc.wait()
        finally:
            unregister()
            self._reap(proc)
            drain.join(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        if token.cancelled:
            raise OperationCancelled()
        lines = [*parser.messages, *stderr_lines]
        if returncode == 0:
            report(1.0)
            return
        if returncode == 1:
            log.warning("7-Zip finished with warnings: %s", " | ".join(lines[-10:]) or "(no details)")
            report(1.0)
            return
        error = _classify_failure(returncode, lines)
        log.info("7-Zip failed (%s): %s", type(error).__name__, error.detail)
        raise error

    def _run_capture(self, args: list[str], *, token: CancelToken | None) -> tuple[int, bytes, bytes]:
        if token is not None:
            token.raise_if_cancelled()
        proc = self._spawn(args)
        unregister = token.on_cancel(lambda: kill_process_tree(proc)) if token is not None else (lambda: None)
        try:
            stdout, stderr = proc.communicate(timeout=_LIST_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            # Kill, then let communicate() finish so its reader threads exit and the pipes close.
            kill_process_tree(proc)
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise ExtractionError("7-Zip did not respond.", detail=" ".join(_redact(args[1:]))) from exc
        finally:
            unregister()
            self._reap(proc)
        if token is not None and token.cancelled:
            raise OperationCancelled()
        return proc.returncode, stdout, stderr

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            kill_process_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a killed process always exits
            log.error("7-Zip process %s did not exit after being killed", proc.pid)

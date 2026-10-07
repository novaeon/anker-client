"""Archive extraction facade: 7-Zip when available, Python ``zipfile`` fallback for .zip.

* Archive type is detected from magic bytes, not the extension:
  ``PK\\x03\\x04`` (also ``PK\\x05\\x06`` empty / ``PK\\x07\\x08`` spanned) zip,
  ``7z\\xBC\\xAF\\x27\\x1C`` 7z, ``Rar!\\x1A\\x07`` rar (v4 and v5).
  Anything that starts with ``<!DOCTYPE``/``<html``/``<head`` (after an optional
  BOM and whitespace) is an HTML error page that was saved by mistake →
  ``CorruptArchiveError("The server sent a web page instead of the game archive.")``.
* ``seven_zip_path`` is resolved lazily on each call via the provided callable
  (so a settings change applies without restart) using ``SevenZip.locate``.
* 7-Zip handles every type it can open (zip included); when its executable
  cannot be started (``SevenZipNotFoundError``), zips fall back to ``zipfile``.
  Unknown types are handed to 7-Zip too (tar, iso…); without 7-Zip they are
  rejected as ``CorruptArchiveError`` unless ``zipfile`` recognises them (e.g. a
  zip with a prefix).
* Zip fallback: validates every member first (zip-slip: members escaping
  ``dest_dir``, absolute/drive/UNC paths, ``..``, ``:`` alternate streams →
  ``ExtractionError``, nothing written), refuses encrypted members
  (``PasswordProtectedArchiveError``), then streams members with progress by
  uncompressed bytes, honouring the cancel token between members and every
  1 MiB. Unsupported compression methods (e.g. Deflate64) →
  ``SevenZipNotFoundError`` asking for 7-Zip. Partially written output is left
  for the caller to clean up (the installer extracts into a staging folder).
* 7z/rar without 7-Zip → ``SevenZipNotFoundError``. The standalone console
  builds cannot open everything: ``7za`` has no RAR support, ``7zr`` reads
  only 7z, 7-Zip before 15.05 does not understand the command line used and
  7-Zip before 15.06 cannot read RAR5, so those archives are routed to the
  zip fallback or rejected with
  ``SevenZipNotFoundError`` (never ``CorruptArchiveError``, which would make
  the download manager delete and re-download a perfectly good archive).
* Zip member names are mapped to names Windows can store, the way 7-Zip does:
  ``<>"|?*`` become ``_``, trailing dots/spaces become ``_`` and reserved device
  names (``CON``, ``NUL.txt``, ``COM1``…) get a ``_`` prefix.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import threading
import time
import zipfile
import zlib
from collections.abc import Callable

from anker_client.core.errors import (
    AnkerError,
    CorruptArchiveError,
    ExtractionError,
    PasswordProtectedArchiveError,
    SevenZipNotFoundError,
)
from anker_client.core.tasks import CancelToken
from anker_client.services.install._fsutil import long_path
from anker_client.services.install.sevenzip import MIN_VERSION as SEVEN_ZIP_MIN_VERSION
from anker_client.services.install.sevenzip import TOO_OLD_MESSAGE as SEVEN_ZIP_TOO_OLD
from anker_client.services.install.sevenzip import SevenZip

log = logging.getLogger(__name__)

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
)
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<head")
_HTML_MESSAGE = "The server sent a web page instead of the game archive."
_BUILTIN_BACKEND = "built-in (zip only)"
_CHUNK = 1024 * 1024
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_INVALID_NAME_CHARS_RE = re.compile(r'[<>"|?*]')
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_ENCRYPTED_FLAG = 0x1
# Archive types the standalone 7-Zip console builds can open (the full 7z.exe opens everything).
_LIMITED_BACKEND_TYPES = {
    "7za": frozenset({"zip", "7z", "unknown"}),
    "7zr": frozenset({"7z"}),
}
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"
_RAR5_MIN_VERSION = (15, 6)  # RAR5 support arrived in 7-Zip 15.06
_VERSION_NUMBER_RE = re.compile(r"(\d+)\.(\d+)")

ProgressCallback = Callable[[float], None]


def detect_archive_type(path: str) -> str:
    """``"zip" | "7z" | "rar" | "html" | "unknown"`` from the first bytes of ``path``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(512)
    except OSError:
        return "unknown"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if text.startswith(_HTML_PREFIXES):
        return "html"
    return "unknown"


class Extractor:
    def __init__(self, seven_zip_path: Callable[[], str]) -> None:
        """``seven_zip_path`` returns the configured path ("" = auto-detect)."""
        self._seven_zip_path = seven_zip_path
        self._lock = threading.Lock()
        self._cached: SevenZip | None = None

    def available_backend(self) -> str:
        """``"7-Zip <version>"`` or ``"built-in (zip only)"`` (also when 7-Zip is too old to be used)."""
        seven_zip = self._seven_zip()
        if seven_zip is None:
            return _BUILTIN_BACKEND
        try:
            version = seven_zip.version()
        except AnkerError:
            log.warning("7-Zip at %s does not run", seven_zip.exe_path)
            return _BUILTIN_BACKEND
        if _older_than(seven_zip, SEVEN_ZIP_MIN_VERSION):
            log.warning("%s at %s is too old to be used", version, seven_zip.exe_path)
            return _BUILTIN_BACKEND
        return version

    def extract(
        self,
        archive: str,
        dest_dir: str,
        *,
        token: CancelToken,
        on_progress: Callable[[float], None] | None = None,
    ) -> None:
        kind = self._check_archive(archive, token)
        seven_zip = self._seven_zip()
        problem = _backend_problem(seven_zip, kind, archive) if seven_zip is not None else ""
        if seven_zip is not None and not problem:
            log.info("Extracting %s (%s) with %s", archive, kind, seven_zip.exe_path)
            try:
                seven_zip.extract(archive, dest_dir, token=token, on_progress=on_progress)
                return
            except SevenZipNotFoundError:
                # 7z.exe exists but cannot be started (blocked, broken install): nothing was
                # written, so a zip can still be extracted by the built-in extractor.
                if not self._zip_fallback_possible(archive, kind):
                    raise
                log.warning("7-Zip at %s could not be started; using the built-in zip extractor",
                            seven_zip.exe_path)
        if self._zip_fallback_possible(archive, kind):
            log.info("Extracting %s with the built-in zip extractor", archive)
            _extract_zip(archive, dest_dir, token=token, on_progress=on_progress)
            return
        raise self._unsupported(kind, problem, seven_zip)

    def test(self, archive: str, *, token: CancelToken, on_progress: Callable[[float], None] | None = None) -> None:
        """Integrity check (7z ``t`` or a full CRC read through ``zipfile``)."""
        kind = self._check_archive(archive, token)
        seven_zip = self._seven_zip()
        problem = _backend_problem(seven_zip, kind, archive) if seven_zip is not None else ""
        if seven_zip is not None and not problem:
            try:
                seven_zip.test(archive, token=token, on_progress=on_progress)
                return
            except SevenZipNotFoundError:
                if not self._zip_fallback_possible(archive, kind):
                    raise
                log.warning("7-Zip at %s could not be started; testing with the built-in zip reader",
                            seven_zip.exe_path)
        if self._zip_fallback_possible(archive, kind):
            _test_zip(archive, token=token, on_progress=on_progress)
            return
        raise self._unsupported(kind, problem, seven_zip)

    # --- helpers --------------------------------------------------------------------------
    def _seven_zip(self) -> SevenZip | None:
        try:
            configured = self._seven_zip_path() or ""
        except Exception:
            log.exception("Could not read the configured 7-Zip path")
            configured = ""
        path = SevenZip.locate(configured or None)
        if not path:
            return None
        with self._lock:
            cached = self._cached
            if cached is None or os.path.normcase(cached.exe_path) != os.path.normcase(os.path.abspath(path)):
                try:
                    cached = SevenZip(path)
                except SevenZipNotFoundError:
                    return None
                self._cached = cached
            return cached

    @staticmethod
    def _check_archive(archive: str, token: CancelToken) -> str:
        token.raise_if_cancelled()
        if not os.path.isfile(archive):
            raise ExtractionError("The archive file could not be found.", detail=archive)
        kind = detect_archive_type(archive)
        if kind == "html":
            raise CorruptArchiveError(_HTML_MESSAGE, detail=archive)
        return kind

    @staticmethod
    def _zip_fallback_possible(archive: str, kind: str) -> bool:
        if kind == "zip":
            return True
        if kind != "unknown":
            return False
        try:
            return zipfile.is_zipfile(archive)
        except OSError:
            return False

    @staticmethod
    def _unsupported(kind: str, problem: str = "", seven_zip: SevenZip | None = None) -> ExtractionError:
        if problem and seven_zip is not None:
            return SevenZipNotFoundError(problem, detail=f"{kind} archive; {seven_zip.exe_path} cannot open it")
        if kind in ("7z", "rar"):
            return SevenZipNotFoundError(detail=f"{kind} archive")
        return CorruptArchiveError("The file is not a supported archive. Delete it and download again.")


def _backend_problem(seven_zip: SevenZip, kind: str, archive: str) -> str:
    """Why this 7-Zip build cannot open the archive (a user message), or "" when it can.

    Letting 7-Zip try would end in "Incorrect command line" (too old) or "Cannot
    open the file as archive" (missing codec); the latter is reported as a damaged
    download and makes the manager delete and fetch a good archive again.
    """
    stem = os.path.splitext(os.path.basename(seven_zip.exe_path))[0].casefold()
    supported = _LIMITED_BACKEND_TYPES.get(stem)
    if supported is not None and kind not in supported:
        return f"This {kind} archive needs the full 7-Zip program. Install 7-Zip or set its location in Settings."
    if _older_than(seven_zip, SEVEN_ZIP_MIN_VERSION):
        return SEVEN_ZIP_TOO_OLD
    if kind == "rar" and _is_rar5(archive) and _older_than(seven_zip, _RAR5_MIN_VERSION):
        return "This RAR archive needs 7-Zip 15.06 or newer. Update 7-Zip or set its location in Settings."
    return ""


def _is_rar5(archive: str) -> bool:
    try:
        with open(archive, "rb") as handle:
            return handle.read(len(_RAR5_MAGIC)) == _RAR5_MAGIC
    except OSError:
        return False


def _older_than(seven_zip: SevenZip, minimum: tuple[int, int]) -> bool:
    try:
        banner = seven_zip.version()
    except AnkerError:
        return False  # it does not run at all: extraction reports that itself
    match = _VERSION_NUMBER_RE.search(banner)
    return bool(match) and (int(match.group(1)), int(match.group(2))) < minimum


# ---------------------------------------------------------------------------
# zip fallback
# ---------------------------------------------------------------------------


class _Progress:
    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self._total = max(1, total)
        self._last = -1.0
        self.done = 0

    def add(self, count: int) -> None:
        self.done += count
        self.emit(self.done / self._total)

    def emit(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if self._callback is not None and fraction > self._last:
            self._last = fraction
            self._callback(fraction)


def _unsafe(name: str) -> ExtractionError:
    return ExtractionError("The archive contains unsafe file paths and was not extracted.", detail=name)


def _member_target(dest_root: str, name: str) -> str | None:
    """Absolute output path for a member, ``None`` for no-op entries, raises for zip-slip."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise _unsafe(name)
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        return None
    for part in parts:
        # Windows strips trailing dots/spaces, so "... " or ". ." would collapse to ".." or "".
        if part == ".." or ":" in part or not part.rstrip(" .") or any(ord(ch) < 32 for ch in part):
            raise _unsafe(name)
    target = os.path.normpath(os.path.join(dest_root, *(_windows_name(part) for part in parts)))
    try:
        common = os.path.commonpath([os.path.normcase(dest_root), os.path.normcase(target)])
    except ValueError as exc:  # different drives
        raise _unsafe(name) from exc
    if common != os.path.normcase(dest_root) or os.path.normcase(target) == os.path.normcase(dest_root):
        raise _unsafe(name)
    return target


def _windows_name(part: str) -> str:
    """A path component Windows can store, mapped like 7-Zip does (see the module doc).

    Written verbatim through extended-length paths, ``"Game "`` or ``"CON"`` would become
    folders that normal paths (and therefore Explorer, the game and the root
    detection) cannot reach.
    """
    fixed = _INVALID_NAME_CHARS_RE.sub("_", part)
    trimmed = fixed.rstrip(" .")
    fixed = trimmed + "_" * (len(fixed) - len(trimmed))
    if fixed.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_DEVICE_NAMES:
        fixed = "_" + fixed
    return fixed


def _open_zip(archive: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise CorruptArchiveError(detail=f"{archive}: {exc}") from exc
    except OSError as exc:
        raise ExtractionError("The archive could not be opened.", detail=f"{archive}: {exc}") from exc


def _read_error(exc: BaseException, name: str) -> ExtractionError:
    if isinstance(exc, NotImplementedError):
        return SevenZipNotFoundError(
            "This archive needs 7-Zip to be extracted. Install 7-Zip or set its location in Settings.",
            detail=f"{name}: {exc}",
        )
    if isinstance(exc, RuntimeError) and "encrypted" in str(exc).lower():
        return PasswordProtectedArchiveError(detail=name)
    return CorruptArchiveError(detail=f"{name}: {exc}")


_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ValueError)


def _extract_zip(archive: str, dest_dir: str, *, token: CancelToken, on_progress: ProgressCallback | None) -> None:
    dest_root = os.path.abspath(dest_dir)
    with _open_zip(archive) as zf:
        members = zf.infolist()
        plan = [(info, _member_target(dest_root, info.filename)) for info in members]
        if any(info.flag_bits & _ENCRYPTED_FLAG for info in members):
            raise PasswordProtectedArchiveError(detail=archive)
        progress = _Progress(on_progress, sum(info.file_size for info in members if not info.is_dir()))
        progress.emit(0.0)
        try:
            os.makedirs(long_path(dest_root), exist_ok=True)
            for info, target in plan:
                token.raise_if_cancelled()
                if target is None:
                    continue
                if info.is_dir():
                    os.makedirs(long_path(target), exist_ok=True)
                    continue
                os.makedirs(long_path(os.path.dirname(target)), exist_ok=True)
                _extract_member(zf, info, target, token=token, progress=progress)
        except OSError as exc:
            if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
                raise ExtractionError("There is not enough free disk space to extract the archive.",
                                      detail=str(exc)) from exc
            raise ExtractionError("The extracted files could not be written.", detail=str(exc)) from exc
        progress.emit(1.0)


def _extract_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: str,
    *,
    token: CancelToken,
    progress: _Progress,
) -> None:
    try:
        with zf.open(info) as source, open(long_path(target), "wb") as sink:
            while chunk := source.read(_CHUNK):
                sink.write(chunk)
                progress.add(len(chunk))
                token.raise_if_cancelled()
    except _READ_ERRORS as exc:
        raise _read_error(exc, info.filename) from exc
    try:
        timestamp = time.mktime((*info.date_time, 0, 0, -1))
        os.utime(long_path(target), (timestamp, timestamp))
    except (OverflowError, ValueError, OSError):
        pass


def _test_zip(archive: str, *, token: CancelToken, on_progress: ProgressCallback | None) -> None:
    with _open_zip(archive) as zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        if any(info.flag_bits & _ENCRYPTED_FLAG for info in members):
            raise PasswordProtectedArchiveError(detail=archive)
        progress = _Progress(on_progress, sum(info.file_size for info in members))
        progress.emit(0.0)
        for info in members:
            token.raise_if_cancelled()
            try:
                # Reading to EOF makes zipfile verify the CRC-32.
                with zf.open(info) as source:
                    while chunk := source.read(_CHUNK):
                        progress.add(len(chunk))
                        token.raise_if_cancelled()
            except _READ_ERRORS as exc:
                raise _read_error(exc, info.filename) from exc
            except OSError as exc:
                raise ExtractionError("The archive could not be read.", detail=str(exc)) from exc
        progress.emit(1.0)

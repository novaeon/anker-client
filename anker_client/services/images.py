"""Disk cache for remote images (covers, screenshots, artwork). Qt-free.

* Files live in ``paths.images_dir / <sha1(url)[:2]> / <sha1(url)><ext>``;
  ``ext`` (``.jpg``/``.png``/``.webp``/``.gif``) comes from the bytes' magic
  number, so lookups try the URL's own extension first, then the others.
* ``fetch`` downloads through ``HttpClient`` (no site pacing for CDN images is
  fine; ankergames.net/uploads images ARE on the site host so they are paced
  by ``HttpClient``), rejects bodies > 20 MiB, non-image content types
  (``text/html`` error pages…) and bodies whose magic number is not
  JPEG/PNG/WebP/GIF, streams into a temp file next to the target and renames
  it atomically, and de-duplicates concurrent requests for the same URL (the
  second caller waits for the first and shares its result or error; if the
  first caller was cancelled, a waiter takes over). Returns the local path.
  Failures raise ``AnkerError`` subclasses (``NetworkError``, ``NotFoundError``,
  plain ``AnkerError`` for invalid images).
* ``prune`` keeps total size under ``max_bytes`` by deleting least recently
  used files (recency = max(atime, mtime); mtime is touched on cache hits, at
  most once an hour per file) down to 90 % of ``max_bytes``, plus temp files
  abandoned for over an hour. Called at startup in the background.
* ``import_file(url, path)`` seeds the cache from an existing local file (used
  by the legacy migration for old ``covers/*.png``); returns ``None`` when the
  file is missing, too large or not an image. An existing cache entry wins.
* ``clear``/``prune`` only ever delete files inside ``<images_dir>/<2 hex>/``
  and skip temp files of downloads in progress.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from anker_client.core.errors import AnkerError, NetworkError, NotFoundError, OperationCancelled
from anker_client.core.paths import AppPaths
from anker_client.core.tasks import NEVER, CancelToken
from anker_client.site.http import HttpClient

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
PRUNE_TARGET_RATIO = 0.9

_CHUNK_SIZE = 64 * 1024
_SNIFF_BYTES = 12
_TOUCH_INTERVAL_SECONDS = 3600
_STALE_TEMP_SECONDS = 3600
_TEMP_SUFFIX = ".tmp"
_EXTENSIONS = (".jpg", ".png", ".webp", ".gif")
_EXTENSION_ALIASES = {".jpeg": ".jpg", ".jpe": ".jpg", ".jfif": ".jpg"}
_ACCEPT = "image/webp,image/png,image/jpeg,image/gif;q=0.9,image/*;q=0.8,*/*;q=0.5"
# Some CDNs label images generically; the magic-number check still applies to these.
_GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream", "application/binary"})
_HEX = frozenset("0123456789abcdef")


def _sniff_image_type(head: bytes) -> str | None:
    """File extension for JPEG/PNG/GIF/WebP magic numbers, ``None`` for anything else."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(head) >= _SNIFF_BYTES and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def _invalid_image(detail: str) -> AnkerError:
    return AnkerError("This image could not be loaded.", detail=detail)


def _is_shard_dir(entry: os.DirEntry[str]) -> bool:
    return len(entry.name) == 2 and set(entry.name) <= _HEX and entry.is_dir(follow_symlinks=False)


class _InFlight:
    """A download in progress that later callers for the same URL wait on."""

    __slots__ = ("done", "error", "path")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.path: Path | None = None
        self.error: BaseException | None = None


class ImageCache:
    def __init__(self, http: HttpClient, paths: AppPaths, *, max_bytes: int = 768 * 1024 * 1024) -> None:
        self._http = http
        self._root = Path(paths.images_dir)
        self._max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()  # guards _inflight
        self._inflight: dict[str, _InFlight] = {}
        # Serialises directory creation/renames against clear()/prune() so a shard
        # directory is never removed between "mkdir" and "create temp file".
        self._fs_lock = threading.RLock()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("Could not create the image cache folder %s", self._root, exc_info=True)

    # --- lookups ----------------------------------------------------------------------
    def cached_path(self, url: str) -> Path | None:
        normalized = self._normalize_url(url)
        return self._lookup(normalized, touch=True) if normalized else None

    def fetch(self, url: str, *, token: CancelToken | None = None) -> Path:
        token = token or NEVER
        normalized = self._normalize_url(url)
        if not normalized:
            raise NotFoundError("This image is not available.", detail=f"unsupported image URL {url!r}")
        hit = self._lookup(normalized, touch=True)
        if hit is not None:
            return hit
        while True:
            with self._lock:
                flight = self._inflight.get(normalized)
                leader = flight is None
                if flight is None:
                    flight = self._inflight[normalized] = _InFlight()
            if leader:
                return self._lead(normalized, flight, token)
            result = self._follow(normalized, flight, token)
            if result is not None:
                return result
            # The leader was cancelled: take over (loop) unless the file appeared meanwhile.

    def import_file(self, url: str, source: Path) -> Path | None:
        normalized = self._normalize_url(url)
        if not normalized:
            return None
        existing = self._lookup(normalized, touch=False)
        if existing is not None:
            return existing
        source = Path(source)
        if not source.is_file():
            return None
        try:
            size = source.stat().st_size
            if size == 0 or size > MAX_IMAGE_BYTES:
                return None
            with source.open("rb") as handle:
                ext = _sniff_image_type(handle.read(_SNIFF_BYTES))
            if ext is None:
                log.debug("Not importing %s: not a JPEG/PNG/WebP/GIF file", source)
                return None
            digest = self._digest(normalized)
            tmp = self._new_temp_file(digest)
            try:
                shutil.copyfile(source, tmp)
                return self._commit(tmp, digest, ext)
            except BaseException:
                _remove_quietly(tmp)
                raise
        except (OSError, AnkerError) as exc:
            log.warning("Could not import %s into the image cache: %s", source, exc)
            return None

    # --- maintenance ------------------------------------------------------------------
    def size_bytes(self) -> int:
        total = 0
        for _shard, entry in self._iter_files():
            try:
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return total

    def clear(self) -> None:
        removed = 0
        with self._fs_lock:
            for _shard, entry in self._iter_files():
                if entry.name.endswith(_TEMP_SUFFIX) and not self._is_stale_temp(entry):
                    continue  # a download in progress owns it
                if _unlink(entry.path):
                    removed += 1
            self._remove_empty_shards()
        log.info("Image cache cleared (%d files)", removed)

    def prune(self) -> int:
        """Returns bytes freed."""
        freed = 0
        files: list[tuple[float, int, str]] = []
        with self._fs_lock:
            for _shard, entry in self._iter_files():
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name.endswith(_TEMP_SUFFIX):
                    if self._is_stale_temp(entry) and _unlink(entry.path):
                        freed += stat.st_size
                    continue
                files.append((max(stat.st_atime, stat.st_mtime), stat.st_size, entry.path))
            total = sum(size for _recency, size, _path in files)
            if total > self._max_bytes:
                target = int(self._max_bytes * PRUNE_TARGET_RATIO)
                files.sort()  # least recently used first
                for _recency, size, path in files:
                    if total <= target:
                        break
                    if _unlink(path):
                        total -= size
                        freed += size
                self._remove_empty_shards()
        if freed:
            log.info("Image cache pruned: %d bytes freed", freed)
        return freed

    # ===================================================================================
    # internals
    # ===================================================================================

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = (url or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        return url if url.lower().startswith(("http://", "https://")) else ""

    @staticmethod
    def _digest(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _candidates(self, url: str, digest: str) -> list[Path]:
        guess = os.path.splitext(urlsplit(url).path)[1].lower()
        guess = _EXTENSION_ALIASES.get(guess, guess)
        order = [guess] if guess in _EXTENSIONS else []
        order += [ext for ext in _EXTENSIONS if ext != guess]
        shard = self._root / digest[:2]
        return [shard / f"{digest}{ext}" for ext in order]

    def _lookup(self, url: str, *, touch: bool) -> Path | None:
        for path in self._candidates(url, self._digest(url)):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size == 0:
                continue
            if touch and time.time() - stat.st_mtime > _TOUCH_INTERVAL_SECONDS:
                try:
                    os.utime(path)  # recency for LRU pruning (NTFS often skips atime updates)
                except OSError:
                    pass
            return path
        return None

    # --- download ---------------------------------------------------------------------
    def _lead(self, url: str, flight: _InFlight, token: CancelToken) -> Path:
        try:
            path = self._download(url, token)
        except BaseException as exc:
            flight.error = exc
            raise
        else:
            flight.path = path
            return path
        finally:
            with self._lock:
                if self._inflight.get(url) is flight:
                    del self._inflight[url]
            flight.done.set()

    def _follow(self, url: str, flight: _InFlight, token: CancelToken) -> Path | None:
        while not flight.done.wait(0.1):
            token.raise_if_cancelled()
        if flight.path is not None and flight.path.exists():
            return flight.path
        if flight.error is not None and not isinstance(flight.error, OperationCancelled):
            raise flight.error
        return self._lookup(url, touch=False)

    def _download(self, url: str, token: CancelToken) -> Path:
        digest = self._digest(url)
        response = self._http.get(url, stream=True, headers={"Accept": _ACCEPT}, token=token)
        try:
            self._check_headers(url, response)
            tmp = self._new_temp_file(digest)
            try:
                ext = self._stream_body(url, response, tmp, token)
                return self._commit(tmp, digest, ext)
            except BaseException:
                _remove_quietly(tmp)
                raise
        finally:
            response.close()

    @staticmethod
    def _check_headers(url: str, response: requests.Response) -> None:
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in _GENERIC_CONTENT_TYPES and not content_type.startswith("image/"):
            raise _invalid_image(f"{url}: unexpected content type {content_type!r}")
        length = response.headers.get("Content-Length")
        if length and length.strip().isdigit() and int(length) > MAX_IMAGE_BYTES:
            raise _invalid_image(f"{url}: {length} bytes exceeds the {MAX_IMAGE_BYTES} byte limit")

    @staticmethod
    def _stream_body(url: str, response: requests.Response, tmp: Path, token: CancelToken) -> str:
        """Write the body to ``tmp``; returns the extension detected from its magic number."""
        head = b""
        ext: str | None = None
        total = 0
        try:
            with open(tmp, "wb") as handle:
                for chunk in response.iter_content(_CHUNK_SIZE):
                    token.raise_if_cancelled()
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise _invalid_image(f"{url}: body exceeds the {MAX_IMAGE_BYTES} byte limit")
                    if ext is None:
                        head = (head + chunk)[:_SNIFF_BYTES]
                        if len(head) >= _SNIFF_BYTES:
                            ext = _sniff_image_type(head)
                            if ext is None:
                                raise _invalid_image(f"{url}: body is not a JPEG/PNG/WebP/GIF image")
                    handle.write(chunk)
        except requests.RequestException as exc:
            raise NetworkError(detail=f"{url}: {exc}") from exc
        except OSError as exc:
            raise AnkerError("Could not save the image to the cache.", detail=f"{tmp}: {exc}") from exc
        if ext is None:  # bodies shorter than the sniff window
            ext = _sniff_image_type(head)
        if ext is None or total == 0:
            raise _invalid_image(f"{url}: empty or unrecognised image body ({total} bytes)")
        return ext

    # --- filesystem -------------------------------------------------------------------
    def _new_temp_file(self, digest: str) -> Path:
        shard = self._root / digest[:2]
        with self._fs_lock:
            try:
                shard.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=f".{digest}.", suffix=_TEMP_SUFFIX, dir=shard)
            except OSError as exc:
                raise AnkerError("Could not write to the image cache.", detail=f"{shard}: {exc}") from exc
        os.close(fd)
        return Path(name)

    def _commit(self, tmp: Path, digest: str, ext: str) -> Path:
        final = self._root / digest[:2] / f"{digest}{ext}"
        for attempt in range(5):
            try:
                with self._fs_lock:
                    os.replace(tmp, final)
                return final
            except PermissionError as exc:
                # Windows: the target may be open in a reader for a moment (sharing violation).
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                if final.exists():  # another writer already stored this image
                    _remove_quietly(tmp)
                    return final
                raise AnkerError("Could not write to the image cache.", detail=f"{final}: {exc}") from exc
            except OSError as exc:
                raise AnkerError("Could not write to the image cache.", detail=f"{final}: {exc}") from exc
        return final

    def _iter_files(self) -> list[tuple[str, os.DirEntry[str]]]:
        found: list[tuple[str, os.DirEntry[str]]] = []
        try:
            with os.scandir(self._root) as shards:
                shard_dirs = [entry for entry in shards if _is_shard_dir(entry)]
        except OSError:
            return found
        for shard in shard_dirs:
            try:
                with os.scandir(shard.path) as entries:
                    found.extend((shard.path, e) for e in entries if e.is_file(follow_symlinks=False))
            except OSError:
                continue
        return found

    def _remove_empty_shards(self) -> None:
        try:
            with os.scandir(self._root) as shards:
                shard_dirs = [entry.path for entry in shards if _is_shard_dir(entry)]
        except OSError:
            return
        for path in shard_dirs:
            try:
                os.rmdir(path)  # fails (harmlessly) unless empty
            except OSError:
                pass

    @staticmethod
    def _is_stale_temp(entry: os.DirEntry[str]) -> bool:
        try:
            return time.time() - entry.stat(follow_symlinks=False).st_mtime > _STALE_TEMP_SECONDS
        except OSError:
            return False


def _unlink(path: str | os.PathLike[str]) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # e.g. open in an image reader on Windows
        log.debug("Could not delete cached image %s: %s", path, exc)
        return False


def _remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

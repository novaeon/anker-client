"""Transactional PATCH/ADDON overlay with an on-disk journal (private to the install package).

Update archives are copied over an existing game file by file. Every replaced
file is first moved to ``<session>\\b\\<relative path>``, and *before* each step
an intent record is appended to ``<session>\\overlay.journal`` (JSON lines,
flushed to the OS so a killed or crashed process loses nothing). A failure or
cancellation rolls back in process; after a crash (or when that rollback could
not finish, e.g. a locked file) the journal stays and is replayed
(:func:`recover`) before the staging folder is deleted — right away by the
failing install, or later by the next install into the same library root — so a
game is never left half-patched with the backups of its original files deleted.

Rollback, newest record first:

* a file that replaced an original: when the backup exists, delete the new file
  and move the backup back (re-applying read-only); without a backup the
  original was never moved and is still in place;
* a new file: delete it when present;
* a created folder: remove it when empty;
* the manifest: rewrite the text it had before the update.

A journal whose last record is ``{"done": true}`` belongs to a committed
update and is never replayed. Paths read from a journal are validated (relative,
no ``..``, target inside the library root) before anything is deleted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from anker_client.constants import MANIFEST_FILENAME
from anker_client.core.errors import InstallError
from anker_client.core.paths import is_within
from anker_client.core.settings import atomic_write_text
from anker_client.services.install import _fsutil as fs

log = logging.getLogger(__name__)

JOURNAL_FILENAME = "overlay.journal"
BACKUP_SUBDIR = "b"

_FILE = "file"
_DIR = "dir"
_MANIFEST = "manifest"


@dataclass(frozen=True, slots=True)
class _Record:
    kind: str  # "file" | "dir" | "manifest"
    relative: str = ""
    had_original: bool = False  # file: an existing file is (about to be) moved to the backup folder
    was_readonly: bool = False
    text: str | None = None  # manifest: original JSON text

    def to_json(self) -> dict[str, Any]:
        if self.kind == _MANIFEST:
            return {"kind": _MANIFEST, "text": self.text}
        if self.kind == _DIR:
            return {"kind": _DIR, "rel": self.relative}
        return {"kind": _FILE, "rel": self.relative, "orig": self.had_original, "ro": self.was_readonly}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _Record | None:
        kind = data.get("kind")
        if kind == _MANIFEST:
            text = data.get("text")
            return cls(_MANIFEST, text=text if isinstance(text, str) else None)
        relative = data.get("rel")
        if kind not in (_FILE, _DIR) or not isinstance(relative, str) or not _safe_relative(relative):
            return None
        return cls(kind, relative, bool(data.get("orig")), bool(data.get("ro")))


def _safe_relative(relative: str) -> bool:
    if not relative or os.path.isabs(relative) or os.path.splitdrive(relative)[0]:
        return False
    normalized = os.path.normpath(relative)
    return not (normalized in (".", "..") or normalized.startswith(".." + os.sep))


class OverlayTransaction:
    """Apply files to ``target`` so that :meth:`rollback` (or a later :func:`recover`) can undo them."""

    def __init__(self, session: str, target: str) -> None:
        self._target = fs.long_path(target)
        self._backup_root = fs.long_path(os.path.join(session, BACKUP_SUBDIR))
        self._journal_path = os.path.join(session, JOURNAL_FILENAME)
        self._records: list[_Record] = []
        self._handle: IO[str] | None = open(self._journal_path, "w", encoding="utf-8", newline="\n")  # noqa: SIM115
        try:
            self._append({"target": os.path.abspath(target)})
            os.fsync(self._handle.fileno())
        except BaseException:
            self.close()
            raise

    # --- steps ------------------------------------------------------------------------------
    def add_dir(self, relative: str) -> None:
        dest = os.path.join(self._target, relative)
        if os.path.isdir(dest):
            return
        if os.path.lexists(dest):
            raise InstallError("The update conflicts with a file of the game.", detail=relative)
        self._record(_Record(_DIR, relative))
        os.mkdir(dest)

    def add_file(self, source: str, relative: str) -> None:
        dest = os.path.join(self._target, relative)
        if os.path.isdir(dest) and not os.path.islink(dest):
            raise InstallError("The update conflicts with a folder of the game.", detail=relative)
        had_original = os.path.lexists(dest)
        was_readonly = had_original and fs.is_readonly(dest)
        self._record(_Record(_FILE, relative, had_original, was_readonly))
        if had_original:
            backup = os.path.join(self._backup_root, relative)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            fs.clear_readonly(dest)
            try:
                fs.rename_with_retry(dest, backup)
            except BaseException:
                if was_readonly:
                    fs.set_readonly(dest)
                raise
        fs.move_file(source, dest)

    def protect_manifest(self) -> None:
        """Record the manifest's current text; call right before rewriting it."""
        try:
            with open(os.path.join(self._target, MANIFEST_FILENAME), encoding="utf-8-sig") as handle:
                text: str | None = handle.read()
        except OSError:
            text = None
        self._record(_Record(_MANIFEST, text=text))

    # --- outcome ----------------------------------------------------------------------------
    def complete(self) -> None:
        """The update is final: mark the journal so it is never replayed, then delete it."""
        if self._handle is not None:
            self._append({"done": True})
            os.fsync(self._handle.fileno())
        self.close()
        try:
            fs.remove_file(self._journal_path)
        except OSError as exc:  # harmless: the "done" record keeps it from being replayed
            log.debug("Could not delete %s: %s", self._journal_path, exc)

    def rollback(self) -> bool:
        """Undo every recorded step; returns False when something could not be restored.

        The journal is deleted only after a complete rollback; otherwise it stays so
        that :func:`recover` can finish the job (the session must then be kept).
        """
        self.close()
        log.warning("Rolling back %d update step(s) in %s", len(self._records), self._target)
        ok = _undo(self._records, self._target, self._backup_root)
        if ok:
            try:
                fs.remove_file(self._journal_path)
            except OSError as exc:  # harmless: replaying a finished rollback changes nothing
                log.debug("Could not delete %s: %s", self._journal_path, exc)
        return ok

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                log.debug("Could not close %s", self._journal_path, exc_info=True)

    # --- journal ------------------------------------------------------------------------------
    def _record(self, record: _Record) -> None:
        self._append(record.to_json())
        self._records.append(record)

    def _append(self, data: dict[str, Any]) -> None:
        if self._handle is None:
            raise InstallError("The update journal is closed.", detail=self._journal_path)
        self._handle.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._handle.flush()


def _undo(records: list[_Record], target: str, backup_root: str) -> bool:
    ok = True
    for record in reversed(records):
        try:
            if record.kind == _FILE:
                _undo_file(record, target, backup_root)
            elif record.kind == _DIR:
                _remove_empty_dir(os.path.join(target, record.relative))
            elif record.kind == _MANIFEST and record.text is not None:
                _restore_manifest(target, record.text)
        except OSError:
            ok = False
            log.error("Could not roll back %s in %s", record.relative or record.kind, target, exc_info=True)
    return ok


def _undo_file(record: _Record, target: str, backup_root: str) -> None:
    dest = os.path.join(target, record.relative)
    if not record.had_original:
        if os.path.lexists(dest):
            fs.remove_file(dest)
        return
    backup = os.path.join(backup_root, record.relative)
    if not os.path.lexists(backup):
        return  # the original was never moved away
    if os.path.lexists(dest):
        fs.remove_file(dest)
    fs.rename_with_retry(backup, dest)
    if record.was_readonly:
        fs.set_readonly(dest)


def _remove_empty_dir(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass  # missing (never created) or not empty (not ours to delete)


def _restore_manifest(target: str, text: str) -> None:
    path = os.path.join(target, MANIFEST_FILENAME)
    if os.path.lexists(path):
        fs.clear_readonly(path)
    atomic_write_text(Path(path), text)
    fs.set_hidden(path)


def _read_journal(path: str) -> tuple[str | None, list[_Record], bool]:
    """``(target, records, done)``; a torn last line (crash mid-write) ends the journal."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    target: str | None = None
    records: list[_Record] = []
    done = False
    for index, line in enumerate(lines):
        try:
            data = json.loads(line)
        except ValueError:
            break
        if not isinstance(data, dict):
            break
        if index == 0:
            value = data.get("target")
            target = value if isinstance(value, str) and value else None
            continue
        if data.get("done") is True:
            done = True
            continue
        record = _Record.from_json(data)
        if record is None:
            log.warning("Ignoring an invalid record in %s: %r", path, data)
            continue
        records.append(record)
    return target, records, done


def recover(session: str, *, within: str) -> bool:
    """Roll back an update that a crash interrupted in staging folder ``session``.

    ``within`` is the library root the update's target must lie in. Returns
    True when the session may now be deleted, False when files could not be
    restored yet (the session, holding their backups, must be kept).
    """
    journal = os.path.join(session, JOURNAL_FILENAME)
    try:
        target, records, done = _read_journal(journal)
    except FileNotFoundError:
        return True
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Could not read %s: %s", journal, exc)
        return False
    if done or not records:
        return True
    if target is None or not is_within(target, within):
        log.error("Refusing to replay %s: its target %r is outside %s", journal, target, within)
        return True
    if not os.path.isdir(target):
        log.warning("Cannot roll back an interrupted update of %s: the game folder is gone", target)
        return True
    log.warning("Rolling back an interrupted update of %s (%d step(s))", target, len(records))
    ok = _undo(records, fs.long_path(target), fs.long_path(os.path.join(session, BACKUP_SUBDIR)))
    if ok:
        try:
            fs.remove_file(journal)
        except OSError:
            pass
    return ok

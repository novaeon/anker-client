"""Windows ``.lnk`` shortcuts for installed games.

* Desktop: ``core.paths.desktop_dir() / "<Safe Title>.lnk"``.
* Start menu: ``start_menu_programs_dir() / START_MENU_FOLDER / "<Safe Title>.lnk"``.
  The legacy client wrote ``start_menu_programs_dir() / "<Safe Title>.lnk"``;
  ``remove`` deletes both locations (and the ``START_MENU_FOLDER`` once empty).
  ``<Safe Title>`` is ``sanitize_windows_name(title)``.
* The folder functions are looked up at call time, so tests may monkeypatch
  either ``anker_client.core.paths.<fn>`` or this module's re-exported names.
* Implementation: ``win32com.client.Dispatch("WScript.Shell").CreateShortcut``
  (COM initialised for the calling thread with ``pythoncom.CoInitializeEx`` and
  balanced with ``CoUninitialize``); fallback to PowerShell's ``WScript.Shell``
  via ``subprocess`` with ``CREATE_NO_WINDOW`` when pywin32 is missing (values
  are passed through environment variables, never interpolated into the
  script). Sets TargetPath, Arguments, WorkingDirectory (exe dir),
  IconLocation (exe,0), Description.
* Never raises on removal (missing files are fine, other errors are logged);
  raises ``InstallError`` on creation failure (callers treat shortcut failures
  as warnings).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from anker_client.constants import APP_NAME, START_MENU_FOLDER
from anker_client.core import paths as _paths
from anker_client.core.errors import InstallError
from anker_client.core.paths import desktop_dir, sanitize_windows_name, start_menu_programs_dir
from anker_client.services.install._fsutil import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

_ORIGINAL_DESKTOP_DIR = desktop_dir
_ORIGINAL_START_MENU_DIR = start_menu_programs_dir
_POWERSHELL_TIMEOUT = 60.0
_RPC_E_CHANGED_MODE = -2147417850

_POWERSHELL_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ANKER_LNK_PATH);"
    "$s.TargetPath=$env:ANKER_LNK_TARGET;"
    "$s.Arguments=$env:ANKER_LNK_ARGS;"
    "$s.WorkingDirectory=$env:ANKER_LNK_WORKDIR;"
    "$s.IconLocation=$env:ANKER_LNK_ICON;"
    "$s.Description=$env:ANKER_LNK_DESC;"
    "$s.Save()"
)


def _desktop_dir() -> Path:
    patched = globals()["desktop_dir"]
    return Path(patched() if patched is not _ORIGINAL_DESKTOP_DIR else _paths.desktop_dir())


def _start_menu_dir() -> Path:
    patched = globals()["start_menu_programs_dir"]
    return Path(patched() if patched is not _ORIGINAL_START_MENU_DIR else _paths.start_menu_programs_dir())


def _link_name(title: str) -> str:
    return f"{sanitize_windows_name(title, fallback='Game')}.lnk"


def _powershell_exe() -> str:
    found = shutil.which("powershell") or shutil.which("pwsh")
    if found:
        return found
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _write_with_pywin32(link: Path, fields: dict[str, str]) -> None:
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    initialized = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
    except pythoncom.com_error as exc:
        # The thread already lives in another apartment (e.g. MTA); COM is usable as is.
        if exc.args and exc.args[0] != _RPC_E_CHANGED_MODE:
            raise
    shell: Any = None
    shortcut: Any = None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(link))
        shortcut.TargetPath = fields["target"]
        shortcut.Arguments = fields["arguments"]
        shortcut.WorkingDirectory = fields["workdir"]
        shortcut.IconLocation = fields["icon"]
        shortcut.Description = fields["description"]
        shortcut.Save()
    except pythoncom.com_error as exc:
        raise OSError(_describe_com_error(exc)) from None
    finally:
        # Release the COM objects before CoUninitialize, or pywin32 faults on teardown.
        shortcut = None
        shell = None
        if initialized:
            pythoncom.CoUninitialize()


def _describe_com_error(exc: BaseException) -> str:
    """``com_error(hr, msg, (code, source, description, …), arg)`` → readable text."""
    args = getattr(exc, "args", ())
    excepinfo = args[2] if len(args) > 2 else None
    if isinstance(excepinfo, tuple) and len(excepinfo) > 2 and excepinfo[2]:
        return str(excepinfo[2])
    return str(exc)


def _write_with_powershell(link: Path, fields: dict[str, str]) -> None:
    env = dict(os.environ)
    env.update(
        ANKER_LNK_PATH=str(link),
        ANKER_LNK_TARGET=fields["target"],
        ANKER_LNK_ARGS=fields["arguments"],
        ANKER_LNK_WORKDIR=fields["workdir"],
        ANKER_LNK_ICON=fields["icon"],
        ANKER_LNK_DESC=fields["description"],
    )
    command = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-Command", _POWERSHELL_SCRIPT]
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_POWERSHELL_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"PowerShell could not be started: {exc}") from exc
    if completed.returncode != 0 or not link.exists():
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"PowerShell exited with {completed.returncode}: {stderr[:500]}")


def _pywin32_available() -> bool:
    try:
        import pythoncom  # type: ignore[import-not-found]  # noqa: F401
        import win32com.client  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def _write_shortcut(link: Path, fields: dict[str, str]) -> None:
    if _pywin32_available():
        _write_with_pywin32(link, fields)
    else:
        log.debug("pywin32 unavailable; creating %s through PowerShell", link)
        _write_with_powershell(link, fields)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
        log.info("Removed shortcut %s", path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove shortcut %s: %s", path, exc)


class ShortcutService:
    # --- locations ------------------------------------------------------------------------
    @staticmethod
    def _desktop_link(title: str) -> Path:
        return _desktop_dir() / _link_name(title)

    @staticmethod
    def _start_menu_folder() -> Path:
        return _start_menu_dir() / START_MENU_FOLDER

    def _start_menu_link(self, title: str) -> Path:
        return self._start_menu_folder() / _link_name(title)

    @staticmethod
    def _legacy_start_menu_link(title: str) -> Path:
        return _start_menu_dir() / _link_name(title)

    # --- API --------------------------------------------------------------------------------
    def create(self, title: str, target_exe: str, *, arguments: str = "", desktop: bool = True,
               start_menu: bool = True) -> list[str]:
        """Create the requested shortcuts; returns the paths written."""
        target = os.path.abspath(target_exe)
        if not os.path.isfile(target):
            raise InstallError("The game's program file was not found, so no shortcut was created.", detail=target)
        fields = {
            "target": target,
            "arguments": arguments or "",
            "workdir": os.path.dirname(target),
            "icon": f"{target},0",
            "description": f"Play {title} ({APP_NAME})",
        }
        requested: list[Path] = []
        if desktop:
            requested.append(self._desktop_link(title))
        if start_menu:
            requested.append(self._start_menu_link(title))
        written: list[str] = []
        failures: list[str] = []
        for link in requested:
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                _write_shortcut(link, fields)
            except Exception as exc:
                log.warning("Could not create shortcut %s: %s", link, exc)
                failures.append(f"{link}: {exc}")
                continue
            written.append(str(link))
            log.info("Created shortcut %s → %s", link, target)
        if failures:
            raise InstallError("Some shortcuts could not be created.", detail="; ".join(failures))
        return written

    def remove(self, title: str) -> None:
        for link in (self._desktop_link(title), self._start_menu_link(title), self._legacy_start_menu_link(title)):
            _remove_quietly(link)
        folder = self._start_menu_folder()
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass

    def exists(self, title: str) -> dict[str, bool]:
        """``{"desktop": bool, "start_menu": bool}`` (legacy start-menu location counts)."""
        return {
            "desktop": self._desktop_link(title).is_file(),
            "start_menu": self._start_menu_link(title).is_file() or self._legacy_start_menu_link(title).is_file(),
        }

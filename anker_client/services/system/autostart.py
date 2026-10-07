"""Start-with-Windows registration (``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``).

Uses ``winreg``; no-ops (returning False) on other platforms. The command is
the frozen executable (``sys.executable`` when ``getattr(sys, "frozen", False)``)
or ``"<pythonw.exe>" -m anker_client`` from source (``pythonw.exe`` next to the
running interpreter when it exists, so no console window flashes at logon),
always followed by ``--minimized``.

Windows also keeps a per-entry switch that Task Manager / Settings › Startup
apps write when the user turns an entry off
(``...\\Explorer\\StartupApproved\\Run``, binary value whose first byte is odd
when disabled; no value = enabled). ``is_enabled`` reports whether a Run value
is registered *and* not switched off there; ``set_enabled(True)`` always
rewrites the Run value (so a moved installation repairs itself) and removes a
"disabled" switch; ``set_enabled(False)`` removes both values.
"""

from __future__ import annotations

import logging
import os
import sys
from types import ModuleType

try:
    import winreg as _winreg_module
except ImportError:  # not Windows
    _winreg_module = None

log = logging.getLogger(__name__)

VALUE_NAME = "AnkerClient"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

# Replaced by tests with a fake module; never write the real registry from tests.
winreg: ModuleType | None = _winreg_module


def _interpreter() -> str:
    executable = sys.executable or "python"
    folder, name = os.path.split(executable)
    if name.casefold() == "python.exe":
        windowed = os.path.join(folder, "pythonw.exe")
        if os.path.isfile(windowed):
            return windowed
    return executable


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{_interpreter()}" -m anker_client --minimized'


def _read_value(registry: ModuleType, key_path: str) -> object | None:
    """``VALUE_NAME`` under ``HKCU\\<key_path>`` (``None`` when absent or unreadable)."""
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_READ) as key:
            value, _kind = registry.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("Could not read %s\\%s", key_path, VALUE_NAME, exc_info=True)
        return None
    return value


def _delete_value(registry: ModuleType, key_path: str) -> None:
    """Remove ``VALUE_NAME`` under ``HKCU\\<key_path>``; absent key/value is fine, other errors raise."""
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_SET_VALUE) as key:
            registry.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def _registered_command() -> str | None:
    """The command currently registered under the Run key (``None`` when absent/unsupported)."""
    registry = winreg
    if registry is None:
        return None
    value = _read_value(registry, RUN_KEY)
    return str(value) if value else None


def _switched_off() -> bool:
    """True when the user disabled the entry in Task Manager / Settings › Startup apps."""
    registry = winreg
    if registry is None:
        return False
    value = _read_value(registry, APPROVED_KEY)
    data = bytes(value) if isinstance(value, bytes | bytearray) else b""
    return bool(data) and data[0] & 1 == 1


def is_enabled() -> bool:
    return bool(_registered_command()) and not _switched_off()


def set_enabled(enabled: bool) -> bool:
    """Returns True on success."""
    registry = winreg
    if registry is None:
        return False
    try:
        if enabled:
            with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
                registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, launch_command())
            # Without this a switch turned off in Task Manager keeps Windows from starting us.
            _delete_value(registry, APPROVED_KEY)
        else:
            _delete_value(registry, RUN_KEY)
            try:
                _delete_value(registry, APPROVED_KEY)
            except OSError:
                log.debug("Could not remove the startup-approval value", exc_info=True)
    except OSError:
        log.warning("Could not %s start with Windows", "enable" if enabled else "disable", exc_info=True)
        return False
    log.info("Start with Windows %s", "enabled" if enabled else "disabled")
    return True

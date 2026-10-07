"""Thin ctypes wrappers around ``ShellExecuteW``/``ShellExecuteExW`` (elevated launches).

Private to ``services.launcher``. Both functions raise :class:`OSError` with a
``winerror`` on failure so the caller can turn it into a user-facing error,
and :class:`OSError` when called on a platform without the Windows shell.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from anker_client.core.tasks import NEVER, CancelToken

log = logging.getLogger(__name__)

SW_HIDE = 0
SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223  # the user declined the UAC prompt
ERROR_ELEVATION_REQUIRED = 740

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_SEE_MASK_FLAG_NO_UI = 0x00000400
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_SE_ERR_MESSAGES = {
    0: "out of memory",
    2: "file not found",
    3: "path not found",
    5: "access denied",
    8: "out of memory",
    11: "invalid executable",
    26: "sharing violation",
    31: "no application associated",
    32: "DLL not found",
}


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("Elevated launches are only supported on Windows.")


@contextmanager
def _com_initialized() -> Iterator[None]:
    """ShellExecute may delegate to shell extensions that need COM on the calling thread."""
    import ctypes

    ole32 = ctypes.WinDLL("ole32")
    result = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE)
    try:
        yield
    finally:
        if result in (0, 1):  # S_OK / S_FALSE → balanced CoUninitialize required
            ole32.CoUninitialize()


def shell_execute(verb: str, file: str, params: str, directory: str, show: int = SW_SHOWNORMAL) -> None:
    """Fire-and-forget ``ShellExecuteW`` (no process handle is returned)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    # A private WinDLL instance so setting argtypes never affects other users of ctypes.windll.
    func = ctypes.WinDLL("shell32").ShellExecuteW
    func.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                     ctypes.c_int]
    func.restype = ctypes.c_void_p
    with _com_initialized():
        code = int(func(None, verb, file, params or None, directory or None, show) or 0)
    if code <= 32:
        # SE_ERR_ACCESSDENIED (5) is also what a declined UAC prompt reports.
        error = OSError(f"ShellExecuteW failed ({_SE_ERR_MESSAGES.get(code, code)})")
        error.winerror = code  # type: ignore[attr-defined]
        raise error


def run_elevated_and_wait(
    file: str,
    params: str,
    directory: str,
    *,
    token: CancelToken | None = None,
    poll_seconds: float = 0.25,
    verb: str = "runas",
    show: int = SW_SHOWNORMAL,
) -> int:
    """Run ``file`` via the ``runas`` verb (elevated), wait for it and return its exit code.

    Cancellation stops *waiting* (the installer keeps running — killing a
    half-done installer is worse) and raises ``OperationCancelled``.
    """
    _require_windows()
    import ctypes
    from ctypes import wintypes

    token = token or NEVER

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes structure layout
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI
    info.lpVerb = verb
    info.lpFile = file
    info.lpParameters = params or None
    info.lpDirectory = directory or None
    info.nShow = show

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    with _com_initialized():
        ok = shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        winerror = ctypes.get_last_error()
        raise ctypes.WinError(winerror)
    if not info.hProcess:
        return 0  # handed off to an already-running process; nothing to wait for
    try:
        while True:
            state = kernel32.WaitForSingleObject(info.hProcess, int(poll_seconds * 1000))
            if state == _WAIT_OBJECT_0:
                break
            if state != _WAIT_TIMEOUT:
                raise ctypes.WinError(ctypes.get_last_error())
            token.raise_if_cancelled()
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)

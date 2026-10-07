"""The ctypes ShellExecute helpers, exercised with the non-elevating "open" verb (no UAC prompt)."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import psutil
import pytest

from anker_client.core.errors import OperationCancelled
from anker_client.core.tasks import CancelToken
from anker_client.services import _library_win as win

SYSTEM32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
CMD = SYSTEM32 / "cmd.exe"
PING = SYSTEM32 / "PING.EXE"

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(os.name != "nt" or not CMD.is_file() or not PING.is_file(), reason="needs Windows"),
]


def test_run_and_wait_returns_exit_code(tmp_path: Path) -> None:
    code = win.run_elevated_and_wait(str(CMD), "/c exit 7", str(tmp_path), verb="open", show=win.SW_HIDE)
    assert code == 7


def test_run_and_wait_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError) as info:
        win.run_elevated_and_wait(str(tmp_path / "missing.exe"), "", str(tmp_path), verb="open", show=win.SW_HIDE)
    assert getattr(info.value, "winerror", None) in (2, 3)


def test_run_and_wait_is_cancellable(tmp_path: Path) -> None:
    ping = tmp_path / "PING.EXE"
    shutil.copy2(PING, ping)
    token = CancelToken()
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(OperationCancelled):
            win.run_elevated_and_wait(str(ping), "-n 30 127.0.0.1", str(tmp_path), verb="open", show=win.SW_HIDE,
                                      token=token, poll_seconds=0.05)
        assert time.monotonic() - started < 5
    finally:
        timer.cancel()
        target = os.path.normcase(str(ping))
        for proc in psutil.process_iter(["exe"]):
            if proc.info.get("exe") and os.path.normcase(proc.info["exe"]) == target:
                proc.kill()
                proc.wait(5)


def test_shell_execute(tmp_path: Path) -> None:
    win.shell_execute("open", str(CMD), "/c exit 0", str(tmp_path), win.SW_HIDE)
    with pytest.raises(OSError) as info:
        win.shell_execute("open", str(tmp_path / "missing.exe"), "", str(tmp_path), win.SW_HIDE)
    assert getattr(info.value, "winerror", None) in (2, 3)

"""Single-instance lock + local-socket messaging (real QLockFile/QLocalServer, unique names per test)."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtNetwork import QLocalSocket

from anker_client.ui.single_instance import MSG_SHOW, SingleInstance, server_name


@pytest.fixture
def name() -> str:
    return f"AnkerClientTest-{uuid.uuid4().hex[:12]}"


def test_server_name_is_per_user_and_per_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANKERCLIENT_HOME", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "Jane Doe")
    plain = server_name()
    assert plain == "AnkerClient-Jane_Doe"
    monkeypatch.setenv("ANKERCLIENT_HOME", str(tmp_path / "a"))
    home_a = server_name()
    monkeypatch.setenv("ANKERCLIENT_HOME", str(tmp_path / "b"))
    assert home_a.startswith(plain + "-") and home_a != server_name()


def test_server_name_without_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_user() -> str:
        raise OSError("no user")

    monkeypatch.setattr("getpass.getuser", no_user)
    assert server_name().startswith("AnkerClient-user")


def test_second_instance_sends_show(qtbot: Any, tmp_path: Path, name: str) -> None:
    lock = tmp_path / "instance.lock"
    primary = SingleInstance(name, lock)
    secondary = SingleInstance(name, lock)
    try:
        assert primary.acquire() and primary.is_primary and primary.is_listening
        assert not secondary.acquire() and not secondary.is_primary
        results: list[bool] = []
        # A real second launch runs concurrently with the primary's event loop: send from a thread.
        sender = threading.Thread(target=lambda: results.append(secondary.send(MSG_SHOW, attempts=3,
                                                                                timeout_ms=2000)))
        with qtbot.waitSignal(primary.message_received, timeout=3000) as blocker:
            sender.start()
        qtbot.waitUntil(lambda: bool(results), timeout=3000)
        sender.join(2)
        assert blocker.args == ["show"]
        assert results == [True]
    finally:
        secondary.close()
        primary.close()


def test_lock_released_on_close(tmp_path: Path, name: str) -> None:
    lock = tmp_path / "instance.lock"
    first = SingleInstance(name, lock)
    assert first.acquire()
    first.close()
    second = SingleInstance(name, lock)
    try:
        assert second.acquire()
    finally:
        second.close()


def test_send_without_primary_gives_up(tmp_path: Path, name: str) -> None:
    lonely = SingleInstance(name, tmp_path / "instance.lock")
    try:
        assert lonely.send("show", attempts=2, timeout_ms=100, retry_delay=0.01) is False
    finally:
        lonely.close()


def test_multiple_lines_and_partial_messages(qtbot: Any, tmp_path: Path, name: str) -> None:
    primary = SingleInstance(name, tmp_path / "instance.lock")
    received: list[str] = []
    primary.message_received.connect(received.append)
    try:
        assert primary.acquire()
        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        socket.write(b"show\nping\npar")
        socket.flush()
        qtbot.waitUntil(lambda: received == ["show", "ping"], timeout=3000)
        socket.write(b"tial")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        qtbot.waitUntil(lambda: received == ["show", "ping", "partial"], timeout=3000)
    finally:
        primary.close()


def test_oversized_message_is_dropped(qtbot: Any, tmp_path: Path, name: str) -> None:
    primary = SingleInstance(name, tmp_path / "instance.lock")
    received: list[str] = []
    primary.message_received.connect(received.append)
    try:
        assert primary.acquire()
        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        socket.write(b"x" * 10_000)
        socket.flush()
        qtbot.wait(300)
        assert received == []
        socket.abort()
    finally:
        primary.close()


def test_unwritable_lock_does_not_block_startup(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QLockFile

    instance = SingleInstance(name, tmp_path / "instance.lock")
    monkeypatch.setattr(instance._lock, "tryLock", lambda _timeout=0: False)
    monkeypatch.setattr(instance._lock, "error", lambda: QLockFile.LockError.PermissionError)
    try:
        assert instance.acquire()
    finally:
        instance.close()

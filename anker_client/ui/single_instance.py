"""Single-instance guard and "show yourself" messaging.

The first instance takes a ``QLockFile`` in the config directory (stale locks
from crashed processes are detected by PID) and listens on a ``QLocalServer``
named ``AnkerClient-<user>`` (plus a short hash of ``ANKERCLIENT_HOME`` when
that override is set, so portable copies and tests never collide). A second
launch fails to take the lock, connects to the server, sends ``show`` and
exits with code 0; the first instance raises its window.

The lock — not the server name — decides who is first: on Windows several
processes can listen on the same pipe name, so ``listen`` alone cannot.

Protocol: UTF-8 lines, one message per line, at most 4 KiB per connection; the
primary answers ``ok`` per line so the sender knows it may disconnect.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import logging
import os
import re
import sys
import time
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from anker_client.constants import APP_ID

log = logging.getLogger(__name__)

MSG_SHOW = "show"
ACK = b"ok\n"
_MAX_BUFFER = 4096


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no USERNAME in the environment (service accounts, odd shells)
        user = "user"
    safe_user = re.sub(r"[^A-Za-z0-9_.-]", "_", user) or "user"
    name = f"{APP_ID}-{safe_user}"
    home = os.environ.get("ANKERCLIENT_HOME")
    if home:
        digest = hashlib.sha1(os.path.normcase(os.path.abspath(home)).encode("utf-8")).hexdigest()[:10]
        name = f"{name}-{digest}"
    return name


def allow_foreground_switch() -> None:
    """Let the primary instance bring its window to the front (Windows foreground-lock rules)."""
    if sys.platform != "win32":
        return
    try:
        asfw_any = -1
        ctypes.windll.user32.AllowSetForegroundWindow(asfw_any)
    except (AttributeError, OSError):
        log.debug("AllowSetForegroundWindow unavailable", exc_info=True)


class SingleInstance(QObject):
    message_received = pyqtSignal(str)

    def __init__(self, name: str, lock_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(lock_path))
        self._lock.setStaleLockTime(0)  # only a dead PID makes the lock stale, never its age
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytes] = {}
        self._primary = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_primary(self) -> bool:
        return self._primary

    @property
    def is_listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    # --- primary ---------------------------------------------------------------------------
    def acquire(self) -> bool:
        """True when this process is the first instance (and now listens for messages)."""
        if not self._lock.tryLock(0):
            error = self._lock.error()
            if error == QLockFile.LockError.LockFailedError:
                return False
            # Permission problems etc. must never stop the app from starting.
            log.warning("Single-instance lock unavailable (%s); continuing without it", error)
        self._primary = True
        self._listen()
        return True

    def _listen(self) -> None:
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._name):
            # A crashed instance can leave a stale socket (non-Windows); we hold the lock, so remove it.
            QLocalServer.removeServer(self._name)
            if not server.listen(self._name):
                log.warning("Single-instance server could not listen on %s: %s", self._name, server.errorString())
                server.deleteLater()
                return
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        log.debug("Single-instance server listening on %s", self._name)

    def _on_new_connection(self) -> None:
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                break
            self._buffers[socket] = b""
            socket.readyRead.connect(partial(self._read, socket))
            socket.disconnected.connect(partial(self._finish, socket))
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        data = self._buffers.get(socket, b"") + bytes(socket.readAll().data())
        if len(data) > _MAX_BUFFER:
            log.warning("Single-instance message too long; dropping connection")
            self._buffers.pop(socket, None)
            socket.abort()
            socket.deleteLater()
            return
        *lines, rest = data.split(b"\n")
        self._buffers[socket] = rest
        for line in lines:
            self._deliver(line)
            if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
                socket.write(ACK)  # lets the sender disconnect knowing the line was read
                socket.flush()

    def _finish(self, socket: QLocalSocket) -> None:
        if socket.bytesAvailable():
            self._read(socket)
        rest = self._buffers.pop(socket, b"")
        if rest.strip():
            self._deliver(rest)
        socket.deleteLater()

    def _deliver(self, raw: bytes) -> None:
        message = raw.decode("utf-8", errors="replace").strip()
        if message:
            log.info("Message from another AnkerClient launch: %s", message)
            self.message_received.emit(message)

    # --- secondary --------------------------------------------------------------------------
    def send(self, message: str = MSG_SHOW, *, attempts: int = 10, timeout_ms: int = 1000,
             retry_delay: float = 0.3) -> bool:
        """Deliver ``message`` to the primary instance. Retries while it is still starting up.

        Blocking — call before the event loop runs (or from a worker thread). The connection stays
        open until the primary acknowledges the line: closing a Windows pipe right after writing
        can drop data the server has not read yet.
        """
        payload = (message.strip() + "\n").encode("utf-8")
        for attempt in range(max(1, attempts)):
            socket = QLocalSocket()  # no parent, no deleteLater: may run in a thread without an event loop
            try:
                socket.connectToServer(self._name)
                if socket.waitForConnected(timeout_ms):
                    socket.write(payload)
                    socket.flush()
                    socket.waitForBytesWritten(timeout_ms)
                    if not self._wait_for_ack(socket, timeout_ms):
                        log.warning("The running instance did not acknowledge %r", message)
                    socket.disconnectFromServer()
                    return True
            finally:
                socket.abort()
            if attempt + 1 < attempts:
                time.sleep(retry_delay)  # before the event loop exists; nothing else to block
        log.warning("Could not reach the running AnkerClient instance on %s", self._name)
        return False

    @staticmethod
    def _wait_for_ack(socket: QLocalSocket, timeout_ms: int) -> bool:
        received = b""
        deadline = time.monotonic() + timeout_ms / 1000
        while ACK not in received:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0 or not socket.waitForReadyRead(remaining):
                return False
            received += bytes(socket.readAll().data())
        return True

    # --- teardown ---------------------------------------------------------------------------
    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        for socket in list(self._buffers):
            socket.abort()
            socket.deleteLater()
        self._buffers.clear()
        if self._primary and self._lock.isLocked():
            self._lock.unlock()
        self._primary = False

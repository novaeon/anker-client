"""GameLauncher against real processes: a copy of PING.EXE plays the part of a game."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from anker_client.core.events import GameExited, GameLaunched
from anker_client.core.models import InstallManifest
from anker_client.services.launcher import GameLauncher
from tests.unit.test_library_support import LibraryEnv, make_env, make_game_dir

PING = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "PING.EXE"

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(os.name != "nt" or not PING.is_file(), reason="needs Windows and PING.EXE"),
]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryEnv]:
    environment = make_env(tmp_path, monkeypatch)
    folder = make_game_dir(environment.root, "Ping Game", files={"readme.txt": b"a game"})
    shutil.copy2(PING, folder / "PING.EXE")
    environment.extra["folder"] = folder
    yield environment
    environment.db.close()


def _manifest(args: str) -> InstallManifest:
    return InstallManifest(slug="ping-game", title="Ping Game", executable="PING.EXE", launch_args=args)


def _setup(env: LibraryEnv, args: str) -> GameLauncher:
    from tests.unit.test_library_support import fake_write_manifest

    fake_write_manifest(str(env.extra["folder"]), _manifest(args))
    env.library.scan()
    return GameLauncher(env.library, env.events, env.settings, poll_interval=0.1, min_session_seconds=1,
                        kill_timeout=3, exit_confirm_polls=2)


def _wait_until(condition, timeout: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _ping_processes(folder: Path) -> list[psutil.Process]:
    target = os.path.normcase(str(folder / "PING.EXE"))
    found = []
    for proc in psutil.process_iter(["exe"]):
        exe = proc.info.get("exe")
        if exe and os.path.normcase(exe) == target:
            found.append(proc)
    return found


def test_launch_track_and_record_session(env: LibraryEnv) -> None:
    launcher = _setup(env, "-n 4 127.0.0.1")  # ~3 seconds
    launcher.start()
    try:
        launcher.launch("ping-game")
        assert launcher.is_running("ping-game")
        assert env.recorder.of(GameLaunched) == [GameLaunched("ping-game", "Ping Game")]
        assert _wait_until(lambda: bool(_ping_processes(env.extra["folder"])), timeout=5)

        exited = env.recorder.wait_for(GameExited, timeout=20)
        assert exited, "the game never exited"
        assert exited[0].install_id == "ping-game"
        assert exited[0].session_seconds >= 2
        assert not launcher.is_running("ping-game")
    finally:
        launcher.shutdown()

    sessions = env.sessions()
    assert len(sessions) == 1 and sessions[0]["install_id"] == "ping-game" and sessions[0]["seconds"] >= 2
    game = env.library.get("ping-game")
    assert game is not None and game.playtime_seconds == sessions[0]["seconds"] and game.last_played


def test_game_started_outside_the_client_is_detected(env: LibraryEnv) -> None:
    launcher = _setup(env, "")
    folder = env.extra["folder"]
    launcher.start()
    process = subprocess.Popen([str(folder / "PING.EXE"), "-n", "3", "127.0.0.1"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert env.recorder.wait_for(GameLaunched, timeout=10), "running game was not detected"
        assert launcher.is_running("ping-game")
        launcher.launch("ping-game")  # refused: already running
        assert _wait_until(lambda: process.poll() is not None, timeout=15)
        assert env.recorder.wait_for(GameExited, timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(5)
        launcher.shutdown()
    assert len(_ping_processes(folder)) == 0
    assert env.sessions() and env.sessions()[0]["seconds"] >= 1


def test_stop_terminates_the_game(env: LibraryEnv) -> None:
    launcher = _setup(env, "-n 120 127.0.0.1")
    launcher.start()
    try:
        launcher.launch("ping-game")
        folder = env.extra["folder"]
        assert _wait_until(lambda: bool(_ping_processes(folder)), timeout=5)
        time.sleep(1.2)  # long enough to be a recorded session
        started = time.monotonic()
        launcher.stop("ping-game")
        assert time.monotonic() - started < 10
        assert not _ping_processes(folder)
        assert not launcher.is_running("ping-game")
        exited = env.recorder.of(GameExited)
        assert len(exited) == 1 and exited[0].session_seconds >= 1
        time.sleep(0.4)  # the monitor must not report a second exit
        assert len(env.recorder.of(GameExited)) == 1
    finally:
        for proc in _ping_processes(env.extra["folder"]):
            proc.kill()
        launcher.shutdown()

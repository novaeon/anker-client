"""GameLauncher with fakes: launching, double-launch refusal, monitoring logic, redist, folders."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
import pytest

from anker_client.core.errors import ExecutableNotSetError, LaunchError, OperationCancelled
from anker_client.core.events import EventBus, GameExited, GameLaunched, LibraryChanged, Notification
from anker_client.core.models import InstalledGame
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services import _library_fs as fs
from anker_client.services import launcher as launcher_module
from anker_client.services.install import installer as installer_module
from anker_client.services.launcher import GameLauncher
from tests.unit.test_library_support import EventRecorder

# --- fakes -----------------------------------------------------------------------------------------


class FakeLibrary:
    def __init__(self, games: list[InstalledGame]) -> None:
        self.items = {g.install_id: g for g in games}
        self.sessions: list[tuple[str, str, str, int]] = []
        self.redist_marked: list[str] = []
        self.games_calls = 0

    def get(self, install_id: str) -> InstalledGame | None:
        game = self.items.get(install_id)
        return game.copy() if game else None

    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]:
        self.games_calls += 1
        return [g.copy() for g in self.items.values()]

    def record_play_session(self, install_id: str, started_at: str, ended_at: str, seconds: int) -> None:
        self.sessions.append((install_id, started_at, ended_at, seconds))

    def mark_redist_installed(self, install_id: str) -> None:
        self.redist_marked.append(install_id)


class FakePopen:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **launcher_kwargs: Any) -> None:
        self.root = tmp_path / "Games"
        self.game_dir = self.root / "Portal 2"
        (self.game_dir / "bin").mkdir(parents=True)
        (self.game_dir / "bin" / "portal2.exe").write_bytes(b"MZ")
        self.game = InstalledGame(install_id="portal-2", title="Portal 2", path=str(self.game_dir),
                                  library_root=str(self.root), slug="portal-2",
                                  executable=os.path.join("bin", "portal2.exe"), launch_args="-novid -w 1920")
        self.library = FakeLibrary([self.game])
        self.events = EventBus()
        self.recorder = EventRecorder(self.events)
        self.settings = SettingsStore(tmp_path / "config.json", self.events)
        self.processes: list[tuple[int, str, float]] = []
        self.clock = Clock()
        self.spawned: list[tuple[Any, dict[str, Any]]] = []
        self.shell_calls: list[tuple[str, str, str, str]] = []
        self.next_popen: FakePopen | None = FakePopen()
        self.spawn_error: OSError | None = None
        self.shell_error: OSError | None = None
        monkeypatch.setattr(launcher_module, "_spawn", self._spawn)
        monkeypatch.setattr(launcher_module, "_shell_execute", self._shell_execute)
        options: dict[str, Any] = {"poll_interval": 0.01, "min_session_seconds": 5, "launch_grace_seconds": 15,
                                   "exit_confirm_polls": 2, "process_lister": lambda: list(self.processes),
                                   "clock": self.clock}
        options.update(launcher_kwargs)
        self.launcher = GameLauncher(self.library, self.events, self.settings, **options)  # type: ignore[arg-type]

    def _spawn(self, command: Any, **kwargs: Any) -> FakePopen:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawned.append((command, kwargs))
        assert self.next_popen is not None
        return self.next_popen

    def _shell_execute(self, verb: str, file: str, params: str, directory: str, show: int = 1) -> None:
        if self.shell_error is not None:
            raise self.shell_error
        self.shell_calls.append((verb, file, params, directory))

    def exe_key(self, relative: str = os.path.join("bin", "portal2.exe")) -> str:
        return fs.norm_key(self.game_dir / relative)

    def set_game(self, **changes: Any) -> None:
        self.game = replace(self.game, **changes)
        self.library.items[self.game.install_id] = self.game


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.launcher.shutdown()


# --- launch --------------------------------------------------------------------------------------------


def test_launch_starts_detached_process(h: Harness) -> None:
    h.launcher.launch("portal-2")

    assert len(h.spawned) == 1
    command, kwargs = h.spawned[0]
    exe = str(h.game_dir / "bin" / "portal2.exe")
    assert kwargs["cwd"] == str(h.game_dir / "bin")
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    if os.name == "nt":
        assert command == f"{subprocess.list2cmdline([exe])} -novid -w 1920"
        assert kwargs["executable"] == exe
        assert kwargs["creationflags"] == subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert command == [exe, "-novid", "-w", "1920"]
    assert h.launcher.is_running("portal-2")
    assert h.launcher.running() == {"portal-2"}
    assert h.recorder.of(GameLaunched) == [GameLaunched("portal-2", "Portal 2")]


def test_launch_keeps_user_quoting_verbatim(h: Harness) -> None:
    h.set_game(launch_args='-path="C:\\My Saves" +connect 1.2.3.4')
    h.launcher.launch("portal-2")
    command = h.spawned[0][0]
    if os.name == "nt":
        assert command.endswith(' -path="C:\\My Saves" +connect 1.2.3.4')


def test_launch_errors(h: Harness) -> None:
    with pytest.raises(LaunchError):
        h.launcher.launch("unknown")
    h.set_game(executable="")
    with pytest.raises(ExecutableNotSetError):
        h.launcher.launch("portal-2")
    h.set_game(executable="missing.exe")
    with pytest.raises(LaunchError) as info:
        h.launcher.launch("portal-2")
    assert "missing.exe" in info.value.user_message
    assert not isinstance(info.value, ExecutableNotSetError)
    assert h.spawned == [] and not h.launcher.is_running("portal-2")


def test_spawn_failure_is_wrapped(h: Harness) -> None:
    h.spawn_error = OSError(2, "boom")
    with pytest.raises(LaunchError):
        h.launcher.launch("portal-2")
    assert not h.launcher.is_running("portal-2")
    assert h.recorder.of(GameLaunched) == []


def test_elevation_required_falls_back_to_runas(h: Harness) -> None:
    error = OSError(22, "The requested operation requires elevation")
    error.winerror = 740  # type: ignore[attr-defined]
    h.spawn_error = error
    h.launcher.launch("portal-2")
    assert h.shell_calls == [("runas", str(h.game_dir / "bin" / "portal2.exe"), "-novid -w 1920",
                              str(h.game_dir / "bin"))]
    assert h.launcher.is_running("portal-2")


def test_run_as_admin_uses_shell_execute(h: Harness) -> None:
    h.set_game(run_as_admin=True)
    h.launcher.launch("portal-2")
    assert h.spawned == []
    assert h.shell_calls[0][0] == "runas"
    assert h.launcher.is_running("portal-2")


def test_run_as_admin_declined(h: Harness) -> None:
    h.set_game(run_as_admin=True)
    error = OSError("declined")
    error.winerror = 5  # type: ignore[attr-defined]
    h.shell_error = error
    with pytest.raises(LaunchError) as info:
        h.launcher.launch("portal-2")
    assert "administrator" in info.value.user_message
    assert not h.launcher.is_running("portal-2")


def test_second_launch_is_refused_with_notification(h: Harness) -> None:
    h.launcher.launch("portal-2")
    h.launcher.launch("portal-2")
    assert len(h.spawned) == 1
    assert h.recorder.of(Notification) == [Notification("Portal 2", "Portal 2 is already running.")]
    assert len(h.recorder.of(GameLaunched)) == 1


def test_monitor_poll_during_spawn_does_not_report_a_second_launch(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    original_spawn = h._spawn

    def spawn_while_monitor_polls(command: Any, **kwargs: Any) -> FakePopen:
        popen = original_spawn(command, **kwargs)
        h.processes = [(popen.pid, h.exe_key(), h.clock.now)]
        h.launcher._poll()  # the monitor thread sees the process before launch() returns
        return popen

    monkeypatch.setattr(launcher_module, "_spawn", spawn_while_monitor_polls)
    h.launcher.launch("portal-2")
    h.launcher._poll()
    assert h.recorder.of(GameLaunched) == [GameLaunched("portal-2", "Portal 2")]
    assert h.launcher.is_running("portal-2")


def test_slow_uac_prompt_does_not_end_the_session(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.set_game(run_as_admin=True)

    def slow_consent(verb: str, file: str, params: str, directory: str, show: int = 1) -> None:
        for _ in range(3):  # the user takes 60 s to answer the prompt while the monitor keeps polling
            h.clock.now += 20
            h.launcher._poll()

    monkeypatch.setattr(launcher_module, "_shell_execute", slow_consent)
    h.launcher.launch("portal-2")
    assert h.launcher.is_running("portal-2")
    assert h.recorder.of(GameExited) == []
    launched_at = h.clock.now
    h.processes = [(9, h.exe_key(), launched_at)]
    h.clock.now += 50
    h.launcher._poll()
    h.processes = []
    h.launcher._poll()
    h.launcher._poll()
    assert h.library.sessions[0][3] == 50  # counted from when the game actually started


def test_failed_spawn_leaves_no_session(h: Harness) -> None:
    h.spawn_error = OSError(2, "boom")
    with pytest.raises(LaunchError):
        h.launcher.launch("portal-2")
    h.spawn_error = None
    h.launcher.launch("portal-2")  # a retry is not refused as "already running"
    assert len(h.spawned) == 1


def test_launch_racing_the_monitor_reports_the_game_once(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.processes = [(777, h.exe_key(), h.clock.now - 100)]  # started from a desktop shortcut
    real_live_processes = h.launcher._live_processes

    def monitor_polls_meanwhile(dir_keys: frozenset[str]) -> dict[int, float]:
        found = real_live_processes(dir_keys)
        h.launcher._poll()  # the monitor thread picks the game up while launch() looks
        return found

    monkeypatch.setattr(h.launcher, "_live_processes", monitor_polls_meanwhile)
    h.launcher.launch("portal-2")

    assert h.spawned == []
    assert h.recorder.of(GameLaunched) == [GameLaunched("portal-2", "Portal 2")]
    assert h.recorder.of(Notification) == [Notification("Portal 2", "Portal 2 is already running.")]


def test_launch_does_not_start_a_second_copy_when_the_monitor_got_there_first(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def monitor_sees_it_now(dir_keys: frozenset[str]) -> dict[int, float]:
        h.processes = [(777, h.exe_key(), h.clock.now)]  # started a moment after the first listing
        h.launcher._poll()
        return {}

    monkeypatch.setattr(h.launcher, "_live_processes", monitor_sees_it_now)
    h.launcher.launch("portal-2")
    assert h.spawned == []
    assert len(h.recorder.of(GameLaunched)) == 1


def test_launch_detects_copy_started_outside_the_client(h: Harness) -> None:
    h.processes = [(777, h.exe_key(), h.clock.now - 100)]
    h.launcher.launch("portal-2")
    assert h.spawned == []
    assert h.launcher.is_running("portal-2")
    assert [type(e) for e in h.recorder.events] == [GameLaunched, Notification]


# --- monitoring ---------------------------------------------------------------------------------------------


def test_external_game_detected_and_session_recorded(h: Harness) -> None:
    h.processes = [(100, h.exe_key(), h.clock.now - 30), (101, h.exe_key("helper.exe"), h.clock.now - 10)]
    h.launcher._poll()
    assert h.launcher.is_running("portal-2")
    assert h.recorder.of(GameLaunched) == [GameLaunched("portal-2", "Portal 2")]

    h.clock.now += 60
    h.launcher._poll()
    h.processes = []
    h.clock.now += 2
    h.launcher._poll()
    assert h.launcher.is_running("portal-2")  # one empty poll is not an exit (hand-off gap)
    h.clock.now += 2
    h.launcher._poll()
    assert not h.launcher.is_running("portal-2")

    assert len(h.library.sessions) == 1
    install_id, started_at, ended_at, seconds = h.library.sessions[0]
    assert install_id == "portal-2" and seconds == 90  # from process start to the last time it was seen alive
    assert started_at < ended_at
    assert h.recorder.of(GameExited) == [GameExited("portal-2", "Portal 2", 90)]


def test_short_sessions_are_not_recorded(h: Harness) -> None:
    h.processes = [(100, h.exe_key(), h.clock.now - 1)]
    h.launcher._poll()
    h.processes = []
    h.launcher._poll()
    h.launcher._poll()
    assert h.library.sessions == []
    assert h.recorder.of(GameExited) == [GameExited("portal-2", "Portal 2", 1)]


def test_detection_does_not_double_count_recorded_time(h: Harness) -> None:
    from datetime import UTC, datetime

    h.set_game(last_played="2027-01-15T08:00:00+00:00")  # recorded at the previous client shutdown
    recorded_until = datetime(2027, 1, 15, 8, 0, tzinfo=UTC).timestamp()
    h.clock.now = recorded_until + 20
    h.processes = [(100, h.exe_key(), recorded_until - 3600)]  # running since before the last recording
    h.launcher._current_targets()
    h.launcher._poll()
    h.processes = []
    h.launcher._poll()
    h.launcher._poll()
    assert h.library.sessions[0][3] == 20


def test_unrelated_and_prefix_folders_are_ignored(h: Harness) -> None:
    sibling = h.root / "Portal 2 Mods" / "mod.exe"
    h.processes = [(1, fs.norm_key(sibling), 0.0), (2, fs.norm_key(h.root / "loose.exe"), 0.0),
                   (os.getpid(), h.exe_key(), 0.0)]
    h.launcher._poll()
    assert h.launcher.running() == set()


def test_launched_game_stays_running_while_child_alive(h: Harness) -> None:
    popen = FakePopen()
    h.next_popen = popen
    h.launcher.launch("portal-2")
    h.clock.now += 100
    for _ in range(5):  # path not visible to the lister, but our child is alive
        h.launcher._poll()
    assert h.launcher.is_running("portal-2")
    popen.returncode = 0
    h.launcher._poll()
    h.launcher._poll()
    assert not h.launcher.is_running("portal-2")
    assert h.library.sessions[0][3] == 100


def test_handoff_from_launcher_to_game(h: Harness) -> None:
    popen = FakePopen()
    h.next_popen = popen
    h.launcher.launch("portal-2")
    popen.returncode = 0  # the launcher stub exits…
    h.processes = [(555, h.exe_key("game-x64.exe"), h.clock.now)]  # …after starting the real game
    h.clock.now += 30
    h.launcher._poll()
    assert h.launcher.is_running("portal-2")
    h.processes = []
    h.launcher._poll()
    h.launcher._poll()
    assert not h.launcher.is_running("portal-2")
    assert h.library.sessions[0][3] == 30
    assert len(h.recorder.of(GameLaunched)) == 1


def test_elevated_launch_grace_period(h: Harness) -> None:
    h.set_game(run_as_admin=True)
    h.launcher.launch("portal-2")
    h.clock.now += 10
    h.launcher._poll()
    h.launcher._poll()
    assert h.launcher.is_running("portal-2")  # not visible yet, still within the grace period
    h.clock.now += 10
    h.launcher._poll()
    h.launcher._poll()
    assert not h.launcher.is_running("portal-2")
    assert h.library.sessions == []  # never seen → nothing to record
    assert h.recorder.of(GameExited)[0].session_seconds == 0


def test_targets_refresh_on_library_changed(h: Harness) -> None:
    h.launcher._poll()
    h.launcher._poll()
    assert h.library.games_calls == 1
    h.events.publish(LibraryChanged())
    h.launcher._poll()
    assert h.library.games_calls == 2


def test_shutdown_records_running_sessions(h: Harness) -> None:
    h.processes = [(100, h.exe_key(), h.clock.now)]
    h.launcher._poll()
    h.clock.now += 42
    h.launcher.shutdown()
    assert h.library.sessions[0][3] == 42
    assert h.launcher.running() == set()
    assert h.recorder.of(GameExited) == []


def test_shutdown_ignores_a_launch_still_waiting_for_uac(h: Harness) -> None:
    h.set_game(run_as_admin=True)
    h.launcher.launch("portal-2")  # elevated: no process handle, not visible yet
    h.clock.now += 10
    h.launcher.shutdown()
    assert h.library.sessions == []


def test_shutdown_records_a_launched_child_that_is_alive(h: Harness) -> None:
    h.launcher.launch("portal-2")  # FakePopen stays alive; its path is never listed
    h.clock.now += 30
    h.launcher.shutdown()
    assert [s[3] for s in h.library.sessions] == [30]


def test_shutdown_keeps_the_last_seen_time_of_a_game_that_already_looked_gone(h: Harness) -> None:
    h.processes = [(100, h.exe_key(), h.clock.now)]
    h.launcher._poll()
    h.clock.now += 30
    h.launcher._poll()
    h.processes = []
    h.clock.now += 2
    h.launcher._poll()  # first empty poll: probably exited
    h.clock.now += 1
    h.launcher.shutdown()
    assert [s[3] for s in h.library.sessions] == [30]


def test_monitor_thread_start_and_shutdown(h: Harness) -> None:
    h.processes = [(100, h.exe_key(), h.clock.now)]
    h.launcher.start()
    h.launcher.start()  # idempotent
    h.recorder.wait_for(GameLaunched, timeout=5)
    assert h.launcher.is_running("portal-2")
    h.launcher.shutdown()
    h.launcher.shutdown()  # safe twice


# --- redist ---------------------------------------------------------------------------------------------------


def _make_redist(game_dir: Path) -> None:
    for relative in (
        "_CommonRedist/vcredist/2015/VC_redist.x64.exe",
        "_CommonRedist/vcredist/2010/vcredist_x86.exe",
        "_CommonRedist/vcredist/2008/vcredist_x86.exe",
        "_CommonRedist/DirectX/Jun2010/DXSETUP.exe",
        "_CommonRedist/DirectX/Jun2010/dxdllreg_x86.exe",
        "_CommonRedist/DotNet/dotNetFx40_Full_x86_x64.exe",
        "_CommonRedist/PhysX/PhysX_Setup.exe",
        "_CommonRedist/readme.txt",
    ):
        path = game_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")


def test_run_redist_runs_installers_with_silent_flags(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_redist(h.game_dir)
    monkeypatch.setattr(installer_module, "find_redist_dirs", lambda d: ["_CommonRedist", "_CommonRedist\\DirectX"])
    calls: list[tuple[str, str, str]] = []

    def fake_run(file: str, params: str, directory: str, *, token: CancelToken | None = None) -> int:
        calls.append((os.path.relpath(file, h.game_dir).replace("\\", "/"), params, directory))
        return 3010 if "2015" in file else 0

    monkeypatch.setattr(launcher_module, "_run_elevated_and_wait", fake_run)

    count = h.launcher.run_redist("portal-2")

    assert count == 6
    assert [(c[0], c[1]) for c in calls] == [
        ("_CommonRedist/DirectX/Jun2010/DXSETUP.exe", "/silent"),
        ("_CommonRedist/DotNet/dotNetFx40_Full_x86_x64.exe", "/q /norestart"),
        ("_CommonRedist/PhysX/PhysX_Setup.exe", ""),
        ("_CommonRedist/vcredist/2008/vcredist_x86.exe", "/q"),
        ("_CommonRedist/vcredist/2010/vcredist_x86.exe", "/q /norestart"),
        ("_CommonRedist/vcredist/2015/VC_redist.x64.exe", "/install /quiet /norestart"),
    ]
    assert calls[0][2] == str(h.game_dir / "_CommonRedist" / "DirectX" / "Jun2010")
    assert h.library.redist_marked == ["portal-2"]


def test_run_redist_without_installers(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_module, "find_redist_dirs", lambda d: [])
    assert h.launcher.run_redist("portal-2") == 0
    assert h.library.redist_marked == ["portal-2"]


def test_run_redist_declined(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_redist(h.game_dir)
    monkeypatch.setattr(installer_module, "find_redist_dirs", lambda d: ["_CommonRedist"])

    def declined(*args: Any, **kwargs: Any) -> int:
        error = OSError("The operation was canceled by the user")
        error.winerror = 1223  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(launcher_module, "_run_elevated_and_wait", declined)
    with pytest.raises(LaunchError):
        h.launcher.run_redist("portal-2")
    assert h.library.redist_marked == []


def test_run_redist_cancelled(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_redist(h.game_dir)
    monkeypatch.setattr(installer_module, "find_redist_dirs", lambda d: ["_CommonRedist"])
    token = CancelToken()
    ran: list[str] = []

    def run_then_cancel(file: str, params: str, directory: str, *, token: CancelToken | None = None) -> int:
        ran.append(file)
        assert token is not None
        token.cancel()
        return 0

    monkeypatch.setattr(launcher_module, "_run_elevated_and_wait", run_then_cancel)
    with pytest.raises(OperationCancelled):
        h.launcher.run_redist("portal-2", token=token)
    assert len(ran) == 1
    assert h.library.redist_marked == []


def test_redist_dirs_outside_the_game_are_ignored(h: Harness, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "evil.exe").write_bytes(b"MZ")
    monkeypatch.setattr(installer_module, "find_redist_dirs", lambda d: ["..\\..\\evil", "../../evil"])
    monkeypatch.setattr(launcher_module, "_run_elevated_and_wait", lambda *a, **k: pytest.fail("must not run"))
    assert h.launcher.run_redist("portal-2") == 0


# --- misc --------------------------------------------------------------------------------------------------------


def test_open_folder(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher_module, "_open_path", opened.append)
    h.launcher.open_folder("portal-2")
    assert opened == [str(h.game_dir)]
    h.set_game(path=str(h.root / "gone"))
    with pytest.raises(LaunchError):
        h.launcher.open_folder("portal-2")
    with pytest.raises(LaunchError):
        h.launcher.open_folder("unknown")


def test_open_folder_wraps_os_errors(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(path: str) -> None:
        raise OSError("no association")

    monkeypatch.setattr(launcher_module, "_open_path", broken)
    with pytest.raises(LaunchError):
        h.launcher.open_folder("portal-2")


class FakeProc:
    """Stands in for ``psutil.Process`` in stop() tests."""

    def __init__(self, pid: int, *, denied: bool = False, stubborn: bool = False, waitable: bool = True) -> None:
        self.pid = pid
        self.alive = True
        self.denied = denied  # e.g. an elevated game: terminate/kill/wait → AccessDenied
        self.stubborn = stubborn  # ignores terminate and kill
        self.waitable = waitable

    def _signal(self) -> None:
        if self.denied:
            raise psutil.AccessDenied(self.pid)
        if not self.stubborn:
            self.alive = False

    def terminate(self) -> None:
        self._signal()

    def kill(self) -> None:
        self._signal()

    def wait(self, timeout: float | None = None) -> int:
        if self.denied or not self.waitable:
            raise psutil.AccessDenied(self.pid)  # psutil needs PROCESS_QUERY_INFORMATION to wait
        if self.alive:
            raise psutil.TimeoutExpired(timeout or 0, self.pid)
        return 0

    def is_running(self) -> bool:
        return self.alive


@pytest.fixture
def fast_stop(h: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(launcher_module, "_KILL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(launcher_module, "_WAIT_POLL_SECONDS", 0.01)
    h.launcher._kill_timeout = 0.05
    return h


def _use_fake_processes(h: Harness, monkeypatch: pytest.MonkeyPatch, procs: list[FakeProc]) -> None:
    monkeypatch.setattr(h.launcher, "_process_lister",
                        lambda: [(p.pid, h.exe_key(), h.clock.now - 60) for p in procs if p.alive])
    monkeypatch.setattr(h.launcher, "_process_tree", lambda pids, popen: [p for p in procs if p.pid in pids])


def test_stop_of_an_elevated_game_raises_launch_error(fast_stop: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = fast_stop
    procs = [FakeProc(10, denied=True)]
    _use_fake_processes(h, monkeypatch, procs)
    h.launcher._poll()
    assert h.launcher.is_running("portal-2")

    with pytest.raises(LaunchError) as info:
        h.launcher.stop("portal-2")

    assert "administrator" in info.value.user_message
    assert h.launcher.is_running("portal-2")  # still running, still tracked
    assert h.recorder.of(GameExited) == []


def test_stop_reports_a_game_that_does_not_close(fast_stop: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = fast_stop
    procs = [FakeProc(10), FakeProc(11, stubborn=True)]
    _use_fake_processes(h, monkeypatch, procs)
    h.launcher._poll()
    with pytest.raises(LaunchError) as info:
        h.launcher.stop("portal-2")
    assert "did not close" in info.value.user_message
    assert not procs[0].alive and h.launcher.is_running("portal-2")


def test_stop_works_without_the_right_to_wait(fast_stop: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = fast_stop
    procs = [FakeProc(10, waitable=False), FakeProc(11)]
    _use_fake_processes(h, monkeypatch, procs)
    h.launcher._poll()
    h.clock.now += 40
    h.launcher.stop("portal-2")
    assert not any(p.alive for p in procs)
    assert not h.launcher.is_running("portal-2")
    assert h.recorder.of(GameExited) == [GameExited("portal-2", "Portal 2", 100)]


def test_stop_without_processes_is_a_no_op(h: Harness) -> None:
    h.launcher.stop("portal-2")
    h.launcher.stop("unknown")
    assert h.recorder.of(GameExited) == []


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("_CommonRedist\\DirectX\\Jun2010\\DXSETUP.exe", "/silent"),
        ("redist\\vc_redist.x86.exe", "/install /quiet /norestart"),
        ("redist\\2005\\vcredist_x64.exe", "/q"),
        ("redist\\NDP472-KB4054530-x86-x64-AllOS-ENU.exe", "/q /norestart"),
        ("redist\\oalinst.exe", ""),
    ],
)
def test_redist_silent_args(relative: str, expected: str) -> None:
    assert launcher_module._redist_silent_args(relative) == expected

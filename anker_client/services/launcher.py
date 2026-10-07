"""Launch games, track running processes and playtime, run prerequisite installers.

* ``launch``: resolve ``game.executable_path`` (``ExecutableNotSetError`` when
  empty, ``LaunchError`` when missing on disk). Normal launch:
  ``subprocess.Popen`` with ``cwd=exe_dir``,
  ``creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``, ``close_fds=True``
  and null stdio. On Windows the user's launch arguments are appended to the
  quoted executable *verbatim* (so their own quoting reaches the game exactly as
  typed); elsewhere they are split with ``shlex``. A program that requires
  elevation (``ERROR_ELEVATION_REQUIRED``) is relaunched elevated.
  ``run_as_admin``: ``ShellExecuteW(None, "runas", exe, args, cwd, SW_SHOWNORMAL)``
  via ctypes (returns no handle → tracked by exe path with psutil).
  Refuses to launch the same game twice while it is running — including a copy
  started outside AnkerClient that the monitor has not seen yet (returns
  quietly after publishing a Notification).
* Monitoring: one daemon thread polls every 2 s with ``psutil``. A game is
  "running" while any process whose executable lives under the install
  directory is alive (covers launcher → game hand-offs, admin launches and
  games started from shortcuts; also detects games already running at
  startup). A game launched by AnkerClient stays running while its own child
  is alive. It is considered exited after two consecutive polls without a
  process (hand-off gaps). When it exits: ``library.record_play_session`` and
  ``GameExited``. Sessions shorter than 5 s are not recorded. A session that
  is still running at ``shutdown`` is recorded up to that moment (one that
  was never seen running — e.g. a UAC prompt still open — is not); when it is
  re-detected on the next start, the new session starts at ``last_played`` so
  nothing is counted twice.
* ``stop``: terminate the game's process tree (``psutil``; kill after 5 s);
  ``LaunchError`` when a process survives (e.g. an elevated game).
* ``run_redist``: run each installer found by ``find_redist_dirs`` (``*.exe``
  in those folders, depth ≤ 3; inside a DirectX folder only ``DXSETUP.exe``)
  with known silent flags — vcredist 2015+ ``/install /quiet /norestart``
  (2010 ``/q /norestart``, 2005/2008 ``/q``), DirectX ``DXSETUP.exe /silent``,
  .NET ``/q /norestart``, others interactive — sequentially, elevated via
  ``runas`` and waited for; then ``library.mark_redist_installed``.
* ``open_folder``: ``os.startfile(path)``.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psutil

from anker_client.core.errors import AnkerError, ExecutableNotSetError, LaunchError
from anker_client.core.events import Event, EventBus, GameExited, GameLaunched, LibraryChanged, Notification
from anker_client.core.models import InstalledGame
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import NEVER, CancelToken
from anker_client.services import _library_fs as fs
from anker_client.services import _library_win as win
from anker_client.services.install import installer as _installer
from anker_client.services.library import LibraryService

log = logging.getLogger(__name__)

#: ``(pid, normalised executable path, create_time)`` of a live process.
ProcessInfo = tuple[int, str, float]

# Exit codes of prerequisite installers that mean "fine": success, newer version
# already installed (1638), reboot initiated (1641) / required (3010).
_REDIST_OK_CODES = frozenset({0, 1638, 1641, 3010})
_REDIST_MAX_DEPTH = 3
_ACCESS_DENIED_ERRORS = frozenset({5, win.ERROR_CANCELLED})
_KILL_WAIT_SECONDS = 2.0
_WAIT_POLL_SECONDS = 0.1


# --- indirections patched by tests ---------------------------------------------------


def _spawn(command: str | list[str], **kwargs: object) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, **kwargs)  # type: ignore[call-overload,no-any-return]


_shell_execute = win.shell_execute
_run_elevated_and_wait = win.run_elevated_and_wait


def _open_path(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- pure helpers --------------------------------------------------------------------


def _build_command(exe: str, args: str) -> str | list[str]:
    """Command for ``Popen``: a raw Windows command line, or an argv list elsewhere."""
    args = (args or "").strip()
    if os.name == "nt":
        quoted = subprocess.list2cmdline([exe])
        return f"{quoted} {args}" if args else quoted
    try:
        return [exe, *shlex.split(args)]
    except ValueError as exc:
        raise LaunchError("The launch options contain an unmatched quote.") from exc


def _redist_silent_args(relative_path: str) -> str:
    """Known unattended switches for common prerequisite installers ("" = run interactively)."""
    name = os.path.basename(relative_path).casefold()
    lowered = relative_path.casefold()
    if name == "dxsetup.exe":
        return "/silent"
    if name.startswith(("vcredist", "vc_redist")):
        if "2005" in lowered or "2008" in lowered:
            return "/q"
        if "2010" in lowered:
            return "/q /norestart"
        return "/install /quiet /norestart"
    if name.startswith(("dotnetfx", "ndp")):
        return "/q /norestart"
    return ""


def _executables_under(folder: str, max_depth: int) -> list[str]:
    found: list[str] = []
    stack: list[tuple[str, int]] = [(folder, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if fs.entry_is_link(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth < max_depth:
                    stack.append((entry.path, depth + 1))
            elif entry.name.casefold().endswith(".exe"):
                found.append(entry.path)
    return sorted(found, key=str.casefold)


def _find_redist_installers(install_dir: str) -> list[tuple[str, str]]:
    """``[(absolute exe path, silent args)]`` for the prerequisite installers of a game."""
    try:
        relative_dirs = _installer.find_redist_dirs(install_dir)
    except Exception:
        log.warning("Could not look for prerequisites in %s", install_dir, exc_info=True)
        return []
    seen: set[str] = set()
    executables: list[str] = []
    for relative in relative_dirs:
        folder = os.path.join(install_dir, relative)
        if not os.path.isdir(folder) or not fs.is_safely_inside(folder, install_dir):
            continue
        for exe in _executables_under(folder, _REDIST_MAX_DEPTH):
            key = fs.norm_key(exe)
            if key not in seen:
                seen.add(key)
                executables.append(exe)
    # A DirectX redist folder holds DXSETUP.exe plus helpers that must not run on their own.
    dx_dirs = {fs.norm_key(os.path.dirname(e)) for e in executables if os.path.basename(e).casefold() == "dxsetup.exe"}
    installers: list[tuple[str, str]] = []
    for exe in executables:
        if fs.norm_key(os.path.dirname(exe)) in dx_dirs and os.path.basename(exe).casefold() != "dxsetup.exe":
            continue
        installers.append((exe, _redist_silent_args(os.path.relpath(exe, install_dir))))
    return installers


def _is_alive(proc: psutil.Process) -> bool:
    try:
        return proc.is_running()
    except psutil.AccessDenied:
        return True  # cannot tell → assume it still runs
    except psutil.Error:
        return False


def _wait_gone(processes: list[psutil.Process], timeout: float) -> list[psutil.Process]:
    """The processes still alive after waiting up to ``timeout`` seconds.

    ``psutil.wait_procs`` needs ``PROCESS_QUERY_INFORMATION``, which a non-elevated
    client does not get for an elevated game (``AccessDenied``); fall back to polling.
    """
    try:
        _gone, alive = psutil.wait_procs(processes, timeout=timeout)
        return list(alive)
    except psutil.Error:
        log.debug("Waiting for processes failed; polling instead", exc_info=True)
    deadline = time.monotonic() + timeout
    while True:
        alive = [proc for proc in processes if _is_alive(proc)]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(_WAIT_POLL_SECONDS)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).replace(microsecond=0).isoformat()


def _epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _dir_keys(path: str) -> frozenset[str]:
    """Every spelling under which Windows may report executables inside ``path``."""
    keys = {fs.norm_key(path), fs.norm_key(fs.long_path(path))}
    try:
        keys.add(fs.norm_key(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    return frozenset(keys)


def _owner(exe_key: str, targets: dict[str, _Target]) -> _Target | None:
    """The game whose directory contains ``exe_key`` (walks up the parent chain)."""
    parent = os.path.dirname(exe_key)
    while True:
        target = targets.get(parent)
        if target is not None:
            return target
        up = os.path.dirname(parent)
        if up == parent:
            return None
        parent = up


# --- state ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    install_id: str
    title: str
    last_played: str


@dataclass(slots=True)
class _Session:
    install_id: str
    title: str
    started: float  # epoch seconds
    launched_by_us: bool
    dir_keys: frozenset[str]
    popen: subprocess.Popen[bytes] | None = None
    pids: dict[int, float] = field(default_factory=dict)  # pid → create_time
    seen: bool = False  # a process was observed at least once
    missing_polls: int = 0
    last_alive: float = 0.0
    starting: bool = False  # launch() is still starting the process (e.g. UAC prompt open)


class GameLauncher:
    def __init__(
        self,
        library: LibraryService,
        events: EventBus,
        settings: SettingsStore,
        *,
        poll_interval: float = 2.0,
        min_session_seconds: float = 5.0,
        kill_timeout: float = 5.0,
        launch_grace_seconds: float = 15.0,
        exit_confirm_polls: int = 2,
        process_lister: Callable[[], list[ProcessInfo]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._library = library
        self._events = events
        self._settings = settings
        self._poll_interval = max(0.01, float(poll_interval))
        self._min_session = float(min_session_seconds)
        self._kill_timeout = float(kill_timeout)
        self._launch_grace = float(launch_grace_seconds)
        self._exit_polls = max(1, int(exit_confirm_polls))
        self._process_lister = process_lister or self._list_processes
        self._clock = clock
        self._own_pid = os.getpid()

        self._lock = threading.RLock()  # sessions + targets
        self._launch_lock = threading.Lock()  # check-then-start must be atomic per launcher
        self._scan_lock = threading.Lock()  # exe cache
        self._sessions: dict[str, _Session] = {}
        self._targets: dict[str, _Target] = {}
        self._targets_dirty = True
        self._exe_cache: dict[tuple[int, float], str] = {}
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._unsubscribe: Callable[[], None] | None = events.subscribe(LibraryChanged, self._on_library_changed)

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        """Start the monitor thread (also detects games already running at startup)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._unsubscribe is None:  # restarted after shutdown()
                self._unsubscribe = self._events.subscribe(LibraryChanged, self._on_library_changed)
                self._targets_dirty = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="anker-game-monitor", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(5.0, self._poll_interval * 2))
        self._thread = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        now = self._clock()
        for session in sessions:
            # The game keeps running; record what was played so far (the next start resumes from here).
            child_alive = session.popen is not None and session.popen.poll() is None
            session.popen = None
            if not (session.seen or child_alive):
                continue  # never observed running (e.g. UAC prompt still open): nothing was played
            if child_alive or session.missing_polls == 0:
                session.last_alive = now  # else it already looked gone: keep the last time it was seen
            self._record(session, now)

    # --- queries -------------------------------------------------------------------------------
    def is_running(self, install_id: str) -> bool:
        with self._lock:
            return install_id in self._sessions

    def running(self) -> set[str]:
        with self._lock:
            return set(self._sessions)

    # --- actions -------------------------------------------------------------------------------
    def launch(self, install_id: str) -> None:
        game = self._library.get(install_id)
        if game is None:
            raise LaunchError("This game is no longer in your library.")
        if not game.executable:
            raise ExecutableNotSetError()
        exe = game.executable_path
        if not os.path.isfile(exe):
            raise LaunchError(
                f'{game.title} could not be started because "{game.executable}" is missing. '
                "Choose its program again in Properties.",
                detail=exe,
            )
        cwd = os.path.dirname(exe)
        dir_keys = _dir_keys(game.path)
        launched = GameLaunched(install_id=install_id, title=game.title)
        already_running = Notification(title=game.title, message=f"{game.title} is already running.")
        pending: list[Event]
        session: _Session | None
        with self._launch_lock:
            running_pids = {} if self.is_running(install_id) else self._live_processes(dir_keys)
            # Check and register atomically: the monitor may have picked the game up while the
            # processes were listed, and its session (and GameLaunched) must not be duplicated.
            with self._lock:
                now = self._clock()
                if install_id in self._sessions:
                    session, pending = None, [already_running]
                elif running_pids:
                    # Started outside AnkerClient (shortcut, Explorer) and not seen by the monitor yet.
                    start = self._detected_start(min(running_pids.values()), game.last_played, now)
                    self._sessions[install_id] = _Session(install_id, game.title, start, False, dir_keys,
                                                          pids=running_pids, seen=True, last_alive=now)
                    session, pending = None, [launched, already_running]
                else:
                    # Registered *before* the process exists so the monitor attaches what it sees to
                    # this session instead of reporting a second, "external" launch.
                    session = _Session(install_id, game.title, now, True, dir_keys, last_alive=now, starting=True)
                    self._sessions[install_id] = session
                    pending = [launched]
                self._targets_dirty = True  # a freshly installed game may not be in the map yet
            if session is not None:
                self._spawn_session(game, exe, cwd, session)
        for event in pending:
            self._events.publish(event)

    def _spawn_session(self, game: InstalledGame, exe: str, cwd: str, session: _Session) -> None:
        """Start the process of an already registered (``starting``) session."""
        start = self._start_elevated if game.run_as_admin else self._start_normal
        try:
            popen = start(game, exe, cwd)
        except BaseException:
            with self._lock:
                if self._sessions.get(game.install_id) is session:
                    del self._sessions[game.install_id]
            raise
        with self._lock:
            now = self._clock()  # the UAC prompt may have taken a while
            if not session.seen:
                session.started = now
            session.last_alive = max(session.last_alive, now)
            session.popen = popen
            session.starting = False
        log.info("Launched %s (%s%s)", game.title, exe, ", elevated" if popen is None else "")

    def stop(self, install_id: str) -> None:
        game = self._library.get(install_id)
        with self._lock:
            session = self._sessions.get(install_id)
            dir_keys = session.dir_keys if session else (_dir_keys(game.path) if game else frozenset())
            popen = session.popen if session else None
        title = game.title if game else (session.title if session else install_id)
        processes = self._process_tree(self._live_processes(dir_keys), popen)
        if processes:
            self._terminate(processes, title)
        if not self._live_processes(dir_keys):
            with self._lock:
                ended = self._sessions.pop(install_id, None)
            if ended is not None:
                if ended.popen is not None:
                    ended.popen.poll()
                    ended.popen = None
                now = self._clock()
                ended.last_alive = now
                self._finish(ended, now)

    def run_redist(self, install_id: str, *, token: CancelToken | None = None) -> int:
        """Returns the number of installers run."""
        token = token or NEVER
        game = self._require(install_id)
        installers = _find_redist_installers(game.path)
        count = 0
        for exe, args in installers:
            token.raise_if_cancelled()
            log.info("Running prerequisite installer %s %s", exe, args)
            try:
                code = _run_elevated_and_wait(exe, args, os.path.dirname(exe), token=token)
            except AnkerError:
                raise
            except OSError as exc:
                if getattr(exc, "winerror", None) in _ACCESS_DENIED_ERRORS:
                    raise LaunchError(
                        "Administrator permission is needed to install the game's prerequisites.", detail=str(exc)
                    ) from exc
                raise LaunchError(
                    f'Could not run "{os.path.basename(exe)}".', detail=f"{exe}: {exc}"
                ) from exc
            count += 1
            if code not in _REDIST_OK_CODES:
                log.warning("Prerequisite installer %s exited with code %s", exe, code)
        self._library.mark_redist_installed(install_id)
        return count

    def open_folder(self, install_id: str) -> None:
        game = self._require(install_id)
        if not os.path.isdir(game.path):
            raise LaunchError(f"The folder of {game.title} no longer exists.", detail=game.path)
        try:
            _open_path(game.path)
        except OSError as exc:
            raise LaunchError("Could not open the game's folder.", detail=str(exc)) from exc

    # --- launching helpers ----------------------------------------------------------------------
    def _require(self, install_id: str) -> InstalledGame:
        game = self._library.get(install_id)
        if game is None:
            raise LaunchError("This game is no longer in your library.")
        return game

    def _start_normal(self, game: InstalledGame, exe: str, cwd: str) -> subprocess.Popen[bytes] | None:
        command = _build_command(exe, game.launch_args)
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "close_fds": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["executable"] = exe
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return _spawn(command, **kwargs)
        except OSError as exc:
            if getattr(exc, "winerror", None) == win.ERROR_ELEVATION_REQUIRED:
                log.info("%s requires administrator rights; relaunching elevated", exe)
                return self._start_elevated(game, exe, cwd)
            raise LaunchError(f"{game.title} could not be started.", detail=f"{exe}: {exc}") from exc

    @staticmethod
    def _start_elevated(game: InstalledGame, exe: str, cwd: str) -> None:
        try:
            _shell_execute("runas", exe, game.launch_args.strip(), cwd)
        except OSError as exc:
            if getattr(exc, "winerror", None) in _ACCESS_DENIED_ERRORS:
                raise LaunchError(
                    f"{game.title} was not started because administrator permission was declined.",
                    detail=str(exc),
                ) from exc
            raise LaunchError(f"{game.title} could not be started as administrator.", detail=str(exc)) from exc

    # --- process helpers ---------------------------------------------------------------------------
    def _list_processes(self) -> list[ProcessInfo]:
        """All live processes with a readable executable path (exe lookups cached per pid+create_time)."""
        result: list[ProcessInfo] = []
        with self._scan_lock:
            fresh: dict[tuple[int, float], str] = {}
            for proc in psutil.process_iter():
                try:
                    pid = proc.pid
                    created = proc.create_time()
                except (psutil.Error, OSError):
                    continue
                key = (pid, created)
                exe = self._exe_cache.get(key)
                if exe is None:
                    try:
                        path = proc.exe()
                    except psutil.AccessDenied:
                        path = ""  # protected/system process: never a game, remember that
                    except (psutil.Error, OSError):
                        continue
                    exe = fs.norm_key(path) if path else ""
                fresh[key] = exe
                if exe:
                    result.append((pid, exe, created))
            self._exe_cache = fresh
        return result

    def _live_processes(self, dir_keys: frozenset[str]) -> dict[int, float]:
        """``{pid: create_time}`` of processes whose executable lives under one of ``dir_keys``."""
        if not dir_keys:
            return {}
        probe = {key: _Target("", "", "") for key in dir_keys}
        return {
            pid: created
            for pid, exe, created in self._process_lister()
            if pid != self._own_pid and _owner(exe, probe) is not None
        }

    def _process_tree(self, pids: dict[int, float], popen: subprocess.Popen[bytes] | None) -> list[psutil.Process]:
        processes: dict[int, psutil.Process] = {}
        for pid, created in pids.items():
            try:
                proc = psutil.Process(pid)
                if created and abs(proc.create_time() - created) > 1.0:
                    continue  # the pid was reused by an unrelated process
                processes[pid] = proc
            except (psutil.Error, OSError):
                continue
        if popen is not None and popen.poll() is None:
            try:
                processes.setdefault(popen.pid, psutil.Process(popen.pid))
            except (psutil.Error, OSError):
                pass
        for proc in list(processes.values()):
            try:
                for child in proc.children(recursive=True):
                    processes.setdefault(child.pid, child)
            except (psutil.Error, OSError):
                continue
        processes.pop(self._own_pid, None)
        return list(processes.values())

    def _terminate(self, processes: list[psutil.Process], title: str) -> None:
        """Terminate, then kill what is left; ``LaunchError`` when something survives."""
        denied = False
        for proc in processes:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                denied = True
        alive = _wait_gone(processes, self._kill_timeout)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                denied = True
        if alive:
            alive = _wait_gone(alive, _KILL_WAIT_SECONDS)
        if not alive:
            return
        log.warning("%d process(es) of %s survived stop()", len(alive), title)
        if denied:
            raise LaunchError(
                f"AnkerClient cannot stop {title} because it runs as administrator. Close it from the game."
            )
        raise LaunchError(f"{title} did not close. Close it from the game, then try again.")

    # --- monitor ---------------------------------------------------------------------------------
    def _run(self) -> None:
        log.debug("Game monitor started")
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception:
                log.exception("Game monitor poll failed")
            self._wake.wait(self._poll_interval)
            self._wake.clear()
        log.debug("Game monitor stopped")

    def _on_library_changed(self, _event: LibraryChanged) -> None:
        with self._lock:
            self._targets_dirty = True

    def _current_targets(self) -> dict[str, _Target]:
        with self._lock:
            if not self._targets_dirty:
                return self._targets
            self._targets_dirty = False
        try:
            games = self._library.games()
        except Exception:
            log.warning("Could not read the library for process monitoring", exc_info=True)
            with self._lock:
                self._targets_dirty = True
                return self._targets
        targets: dict[str, _Target] = {}
        for game in games:
            if not game.path:
                continue
            target = _Target(game.install_id, game.title, game.last_played)
            for key in _dir_keys(game.path):
                targets.setdefault(key, target)
        with self._lock:
            self._targets = targets
        return targets

    def _poll(self) -> None:
        targets = self._current_targets()
        found: dict[str, dict[int, float]] = {}
        owners: dict[str, _Target] = {}
        if targets:
            for pid, exe, created in self._process_lister():
                if pid == self._own_pid:
                    continue
                target = _owner(exe, targets)
                if target is not None:
                    found.setdefault(target.install_id, {})[pid] = created
                    owners[target.install_id] = target
        now = self._clock()
        started: list[_Session] = []
        ended: list[_Session] = []
        with self._lock:
            for install_id, pids in found.items():
                session = self._sessions.get(install_id)
                if session is None:
                    target = owners[install_id]
                    start = self._detected_start(min(pids.values()), target.last_played, now)
                    dir_keys = frozenset(key for key, t in targets.items() if t.install_id == install_id)
                    session = _Session(install_id, target.title, start, False, dir_keys)
                    self._sessions[install_id] = session
                    started.append(session)
                session.pids = dict(pids)
                session.seen = True
                session.missing_polls = 0
                session.last_alive = now
            for install_id, session in list(self._sessions.items()):
                if install_id in found or session.starting:
                    continue
                if session.popen is not None:
                    if session.popen.poll() is None:
                        session.last_alive = now  # our child is alive even if its path did not match
                        continue
                    session.popen = None
                if not session.seen and now - session.started < self._launch_grace:
                    continue  # an elevated/slow start may not be visible yet
                session.missing_polls += 1
                if session.missing_polls >= self._exit_polls:
                    del self._sessions[install_id]
                    ended.append(session)
        for session in started:
            log.info("Detected running game %s", session.title)
            self._events.publish(GameLaunched(install_id=session.install_id, title=session.title))
        for session in ended:
            self._finish(session, now)

    def _detected_start(self, first_created: float, last_played: str, now: float) -> float:
        """Session start for a game found already running: never before what was already recorded."""
        start = first_created or now
        recorded_until = _epoch(last_played)
        if recorded_until is not None and recorded_until > start:
            start = recorded_until
        return min(start, now)

    def _record(self, session: _Session, end: float) -> int:
        end = max(session.started, session.last_alive or end)
        seconds = round(end - session.started)
        if seconds >= self._min_session:
            try:
                self._library.record_play_session(session.install_id, _iso(session.started), _iso(end), seconds)
            except Exception:
                log.exception("Could not record the play session of %s", session.title)
        return seconds

    def _finish(self, session: _Session, now: float) -> None:
        seconds = self._record(session, now)
        log.info("%s exited after %ss", session.title, seconds)
        self._events.publish(GameExited(install_id=session.install_id, title=session.title, session_seconds=seconds))

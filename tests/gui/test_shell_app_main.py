"""``app.main`` end to end on FakeContext (event loop, logging setup and dialogs are patched)."""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QWidget

from anker_client import app
from anker_client.core.paths import AppPaths
from anker_client.ui.single_instance import SingleInstance, server_name
from tests.fakes import FakeContext

PAGES = {
    "anker_client.ui.pages.store": "StorePage",
    "anker_client.ui.pages.game": "GamePage",
    "anker_client.ui.pages.library": "LibraryPage",
    "anker_client.ui.pages.downloads": "DownloadsPage",
    "anker_client.ui.pages.settings": "SettingsPage",
}


class Page(QWidget):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.setProperty("role", "page")


class Recorder:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.windows: list[Any] = []
        self.fatal: list[tuple[str, str]] = []
        self.started = 0
        self.shutdowns = 0


@pytest.fixture
def harness(qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    from anker_client.services import container
    from anker_client.ui import main_window

    rec = Recorder()
    for module_name, class_name in PAGES.items():
        monkeypatch.setattr(importlib.import_module(module_name), class_name, Page)

    def build_context(paths: AppPaths | None = None) -> FakeContext:
        ctx = FakeContext(tmp_path / f"ctx{len(rec.contexts)}")
        original_start, original_shutdown = ctx.start, ctx.shutdown

        def start() -> None:
            rec.started += 1
            original_start()

        def shutdown() -> None:
            rec.shutdowns += 1
            original_shutdown()

        ctx.start = start  # type: ignore[method-assign]
        ctx.shutdown = shutdown  # type: ignore[method-assign]
        rec.contexts.append(ctx)
        return ctx

    original_window = main_window.MainWindow

    def window_factory(*args: Any, **kwargs: Any) -> Any:
        kwargs["tray_available"] = False
        window = original_window(*args, **kwargs)
        rec.windows.append(window)
        return window

    monkeypatch.setattr(container, "build_context", build_context)
    monkeypatch.setattr(main_window, "MainWindow", window_factory)
    monkeypatch.setattr(app, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(app, "import_verification", lambda: (None, False))
    monkeypatch.setattr(app, "exec_application", lambda _app: 0)
    monkeypatch.setattr(app, "show_fatal_error", lambda title, message, details="": rec.fatal.append((title, message)))
    monkeypatch.setattr(app, "set_app_user_model_id", lambda *a: True)
    return rec


def _cleanup(rec: Recorder) -> None:
    for window in rec.windows:
        if not sip.isdeleted(window):
            window.hide()
            window.deleteLater()


def test_main_runs_and_shuts_down_cleanly(harness: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run_loop(_app: Any) -> int:
        window = harness.windows[0]
        seen.update(page=window.current_page_key(), visible=window.isVisible())
        return 0

    monkeypatch.setattr(app, "exec_application", run_loop)
    try:
        assert app.main(["--reset-window"]) == 0
        assert harness.started == 1 and harness.shutdowns == 1
        assert seen == {"page": "store", "visible": True}
        assert QApplication.instance().applicationName() == "AnkerClient"
        assert QApplication.instance().quitOnLastWindowClosed() is False
        # Destroyed during shutdown, while the QApplication still exists (not at interpreter exit).
        assert sip.isdeleted(harness.windows[0])
    finally:
        _cleanup(harness)


def test_main_minimized_without_tray_minimizes(harness: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    states: list[bool] = []
    monkeypatch.setattr(app, "exec_application",
                        lambda _app: states.append(harness.windows[0].isMinimized()) or 0)
    try:
        assert app.main(["--minimized"]) == 0
        assert states == [True]
    finally:
        _cleanup(harness)


def test_shutdown_releases_embedded_browsers_after_the_window(harness: Recorder,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    order: list[str] = []
    fake_browser = types.ModuleType("anker_client.ui.dialogs.browser")
    fake_browser.release_browsers = lambda: order.append(  # type: ignore[attr-defined]
        "browsers:" + ("window gone" if sip.isdeleted(harness.windows[0]) else "window alive"))
    monkeypatch.setitem(sys.modules, "anker_client.ui.dialogs.browser", fake_browser)

    class Verification:
        class WebEngineVerifier:
            def __init__(self, http: Any, paths: Any, parent_window: Any = None) -> None:
                pass

            def shutdown(self) -> None:
                order.append("verifier")

        @staticmethod
        def browser_user_agent(paths: Any) -> str:
            return ""

    monkeypatch.setattr(app, "import_verification", lambda: (Verification, True))
    try:
        assert app.main([]) == 0
        assert order == ["verifier", "browsers:window gone"]
    finally:
        _cleanup(harness)


def test_main_runs_first_run_wizard_when_needed(harness: Recorder, monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    from anker_client.services import container
    from anker_client.ui.dialogs import first_run

    shown: list[bool] = []

    class Wizard(QWidget):
        def __init__(self, ctx: Any, theme: Any, parent: Any = None) -> None:
            super().__init__()
            shown.append(True)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(first_run, "FirstRunWizard", Wizard)
    build = container.build_context

    def fresh(paths: Any = None) -> FakeContext:
        ctx = build(paths)
        ctx.settings.update(first_run_completed=False)
        return ctx

    monkeypatch.setattr(container, "build_context", fresh)
    try:
        assert app.main([]) == 0
        assert shown == [True]
    finally:
        _cleanup(harness)


def test_broken_wizard_does_not_stop_startup(harness: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services import container
    from anker_client.ui.dialogs import first_run

    def broken(*_a: Any, **_k: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(first_run, "FirstRunWizard", broken)
    build = container.build_context

    def fresh(paths: Any = None) -> FakeContext:
        ctx = build(paths)
        ctx.settings.update(first_run_completed=False)
        return ctx

    monkeypatch.setattr(container, "build_context", fresh)
    try:
        assert app.main([]) == 0
        assert harness.started == 1
    finally:
        _cleanup(harness)


def test_context_failure_is_fatal_with_message(harness: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.services import container

    def broken(paths: Any = None) -> None:
        raise OSError("database is locked")

    monkeypatch.setattr(container, "build_context", broken)
    assert app.main([]) == 1
    assert harness.fatal and "database is locked" in harness.fatal[0][1]


def test_verifier_installed_and_shut_down(harness: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Verifier:
        def __init__(self, http: Any, paths: Any, parent_window: Any = None) -> None:
            events.append("created")
            self.parent_window = parent_window

        def shutdown(self) -> None:
            events.append("shutdown")

    class Verification:
        WebEngineVerifier = Verifier

        @staticmethod
        def browser_user_agent(paths: Any) -> str:
            return "Mozilla/5.0 Chrome/134"

    monkeypatch.setattr(app, "import_verification", lambda: (Verification, True))
    verifiers: list[Any] = []

    from anker_client.services import container

    build = container.build_context

    def ctx_with_resolver(paths: Any = None) -> FakeContext:
        ctx = build(paths)
        ctx.resolver = type("R", (), {"set_verifier": lambda self, v: verifiers.append(v)})()
        return ctx

    monkeypatch.setattr(container, "build_context", ctx_with_resolver)
    try:
        assert app.main([]) == 0
        assert events == ["created", "shutdown"]
        assert verifiers and verifiers[0].parent_window is harness.windows[0]
        assert harness.contexts[0].http.user_agent == "Mozilla/5.0 Chrome/134"
    finally:
        _cleanup(harness)


def test_primary_raises_its_window_when_a_second_launch_says_show(qtbot: Any, harness: Recorder,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bool] = []

    def run_loop(_app: Any) -> int:
        window = harness.windows[0]
        window.hide()  # e.g. closed to the tray
        paths = AppPaths.default()
        second = SingleInstance(server_name(), Path(paths.config_dir) / "instance.lock")
        assert not second.acquire()  # the running main() holds the lock
        # A real second launch is another process: send from a thread while this "event loop" runs.
        sender = threading.Thread(target=lambda: sent.append(second.send("show", attempts=3, timeout_ms=2000)))
        sender.start()
        qtbot.waitUntil(window.isVisible, timeout=5000)
        sender.join(5)
        second.close()
        return 0

    monkeypatch.setattr(app, "exec_application", run_loop)
    try:
        assert app.main(["--minimized"]) == 0
        assert sent == [True]
    finally:
        _cleanup(harness)


def test_update_flags_loaded_by_the_startup_scan_are_not_announced(qtbot: Any, harness: Recorder,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.core import events as ev
    from anker_client.core.models import GameUpdate
    from anker_client.services import container

    build = container.build_context

    def unscanned(paths: Any = None) -> FakeContext:
        # Like the real LibraryService: nothing is known until the startup scan ran.
        ctx = build(paths)
        library = ctx.library
        real_games, real_scan, scanned = library.games, library.scan, []
        library.games = lambda *, include_hidden=True: real_games(include_hidden=include_hidden) if scanned else []

        def scan(*, token: Any = None) -> Any:
            scanned.append(True)
            return real_scan(token=token)

        library.scan = scan
        return ctx

    monkeypatch.setattr(container, "build_context", unscanned)

    def update_toasts(window: Any) -> list[str]:
        return [t.message for t in window.toasts.toasts() if t.title == "Updates available"]

    def run_loop(_app: Any) -> int:
        from anker_client.ui.shell_startup import StartupTasks

        window = harness.windows[0]
        ctx = harness.contexts[0]
        tasks = window.findChild(StartupTasks)
        assert tasks is not None
        qtbot.waitUntil(lambda: tasks.chain_done, timeout=5000)
        qtbot.waitUntil(lambda: window.header.updates_chip.text() == "2 updates", timeout=5000)
        ctx.events.publish(ev.UpdatesFound(tuple(ctx.updates.pending())))  # flags saved by an earlier run
        qtbot.wait(200)
        assert update_toasts(window) == []
        fresh = GameUpdate("celeste", "celeste", "Celeste", "v1.0", "v1.1")
        ctx.events.publish(ev.UpdatesFound((*ctx.updates.pending(), fresh)))
        qtbot.waitUntil(lambda: any("Celeste" in m for m in update_toasts(window)), timeout=3000)
        return 0

    monkeypatch.setattr(app, "exec_application", run_loop)
    try:
        assert app.main([]) == 0
    finally:
        _cleanup(harness)


def test_failed_catalog_sync_shows_in_the_header(qtbot: Any, harness: Recorder,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from anker_client.core import events as ev
    from anker_client.core.errors import NetworkError
    from anker_client.services import container

    build = container.build_context

    def failing_sync(paths: Any = None) -> FakeContext:
        ctx = build(paths)
        ctx.catalog.needs_sync = lambda max_age: True

        def sync(*, full: bool = False, token: Any, on_progress: Any = None) -> int:
            ctx.events.publish(ev.CatalogSyncProgress(1, 37))
            try:
                raise NetworkError("The site did not answer")
            finally:  # like CatalogService: CatalogUpdated is published even when the crawl fails
                ctx.events.publish(ev.CatalogUpdated(total_games=10, new_games=0))

        ctx.catalog.sync = sync
        return ctx

    monkeypatch.setattr(container, "build_context", failing_sync)

    def run_loop(_app: Any) -> int:
        header = harness.windows[0].header
        qtbot.waitUntil(lambda: header.sync_text() == "Catalog sync failed", timeout=5000)
        assert header.sync_indicator.toolTip()
        return 0

    monkeypatch.setattr(app, "exec_application", run_loop)
    try:
        assert app.main([]) == 0
    finally:
        _cleanup(harness)


def test_second_launch_asks_first_instance_to_show(qtbot: Any, harness: Recorder,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    # main() runs on a worker thread here (a real second launch is another process): keep it off the QApplication.
    monkeypatch.setattr(app, "create_application", lambda _args: QApplication.instance())
    paths = AppPaths.default().ensure()
    primary = SingleInstance(server_name(), Path(paths.config_dir) / "instance.lock")
    assert primary.acquire()
    codes: list[int] = []
    try:
        with qtbot.waitSignal(primary.message_received, timeout=5000) as blocker:
            thread = threading.Thread(target=lambda: codes.append(app.main([])))
            thread.start()
        qtbot.waitUntil(lambda: bool(codes), timeout=5000)
        thread.join(5)
        assert codes == [0]
        assert blocker.args == ["show"]
        assert harness.contexts == []  # the second launch never builds services
    finally:
        primary.close()

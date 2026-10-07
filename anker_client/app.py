"""Application entry point (docs/ARCHITECTURE.md §Startup sequence).

``main(argv)``:

1. Parse ``--minimized``, ``--debug``, ``--reset-window``, ``--version``
   (unknown arguments are passed on to Qt).
2. ``AppPaths.default().ensure()``; logging (level from the settings file, or
   DEBUG with ``--debug``).
3. Import ``ui.dialogs.verification`` (guards the QtWebEngine import, which must
   happen before the QApplication exists); ``AA_ShareOpenGLContexts``; high-DPI
   rounding PassThrough; Windows AppUserModelID; QApplication (name, org,
   version, icon, ``setQuitOnLastWindowClosed(False)``).
4. Single instance: a second launch sends ``show`` to the first and exits 0;
   the first instance raises its window (also from the tray).
5. Crash handlers (``sys``/``threading`` excepthooks, faulthandler, Qt messages).
6. ``build_context()``; theme; ``QtEventBridge``; ``ImageLoader``; HTTP
   User-Agent from the embedded browser; ``WebEngineVerifier`` →
   ``ctx.resolver.set_verifier``. (The verifier is created right after the
   window so it can parent its dialogs to it.)
7. ``MainWindow``; first-run wizard when needed; show — or stay in the tray
   with ``--minimized`` / ``start_minimized``.
8. ``ctx.start()``; ``StartupTasks`` (migration → scan → auth → catalog →
   image prune; periodic game/app update checks). Once the chain is done the
   window treats the update flags loaded by the scan as already announced; a
   failed library scan becomes a toast, a failed catalog sync shows in the
   header's sync indicator.
9. ``app.exec()``; then shut down in order: startup tasks, window (saves
   geometry), verifier, bridge, ``ctx.shutdown()``; the window and the embedded
   browser profiles are deleted while the QApplication still exists; finally
   the crash hooks and the single-instance lock are released.

Every optional step is guarded: a failure is logged and the app continues.
Only a failure to build the service container is fatal (error dialog, exit 1).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import platform
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from anker_client import __version__
from anker_client.constants import APP_ID, APP_NAME, ORG_NAME
from anker_client.core.logging_setup import setup_logging
from anker_client.core.paths import AppPaths, resource_path

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1


@dataclass(frozen=True, slots=True)
class Options:
    minimized: bool = False
    debug: bool = False
    reset_window: bool = False
    version: bool = False
    qt_args: tuple[str, ...] = ()


def parse_args(argv: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(
        prog="ankerclient",
        description=f"{APP_NAME} — unofficial desktop client and download manager for AnkerGames.",
    )
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--reset-window", action="store_true", help="forget the saved window size and position")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    namespace, unknown = parser.parse_known_args(list(argv))
    return Options(
        minimized=namespace.minimized,
        debug=namespace.debug,
        reset_window=namespace.reset_window,
        version=namespace.version,
        qt_args=tuple(unknown),
    )


def configured_log_level(paths: AppPaths) -> str:
    """``log_level`` from config.json without building the settings store (it does not exist yet)."""
    try:
        data = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "INFO"
    level = data.get("log_level") if isinstance(data, dict) else None
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"


def set_app_user_model_id(app_id: str = APP_ID) -> bool:
    """Group taskbar buttons/notifications under our own identity instead of python.exe."""
    if sys.platform != "win32":
        return False
    try:
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        log.debug("SetCurrentProcessExplicitAppUserModelID unavailable", exc_info=True)
        return False
    return result == 0


def _write_stdout(text: str) -> None:
    stream = sys.stdout
    if stream is None:  # pythonw / windowed builds have no console
        return
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        pass


def _guarded(name: str, fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        log.exception("Startup: %s failed", name)
        return default


# --- QtWebEngine ----------------------------------------------------------------------------------


def import_verification() -> tuple[ModuleType | None, bool]:
    """Import the verification module (and with it QtWebEngine) BEFORE the QApplication exists."""
    try:
        from anker_client.ui.dialogs import verification
    except Exception:
        log.exception("Verification module could not be imported; browser verification disabled")
        return None, _import_webengine_directly()
    try:
        return verification, bool(verification.webengine_available())
    except NotImplementedError:
        return verification, _import_webengine_directly()
    except Exception:
        log.exception("webengine_available() failed")
        return verification, False


def _import_webengine_directly() -> bool:
    try:
        import PyQt6.QtWebEngineWidgets  # noqa: F401
    except Exception:
        log.warning("QtWebEngine is not available", exc_info=True)
        return False
    return True


# --- QApplication -----------------------------------------------------------------------------------


def create_application(qt_args: Sequence[str]) -> Any:
    from PyQt6.QtCore import QCoreApplication, Qt
    from PyQt6.QtGui import QGuiApplication, QIcon
    from PyQt6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication([sys.argv[0] if sys.argv else APP_NAME, *qt_args])
    else:
        app = existing
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)
    icon_path = resource_path("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setQuitOnLastWindowClosed(False)
    return app


def exec_application(app: Any) -> int:
    return int(app.exec())


def show_fatal_error(title: str, message: str, details: str = "") -> None:
    from PyQt6.QtWidgets import QMessageBox

    box = QMessageBox(QMessageBox.Icon.Critical, title, message)
    if details:
        box.setDetailedText(details)
    box.exec()


# --- main --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:  # --help or argparse errors
        if exc.code is None:
            return EXIT_OK
        return exc.code if isinstance(exc.code, int) else EXIT_STARTUP_FAILED
    if options.version:
        _write_stdout(f"{APP_NAME} {__version__}\n")
        return EXIT_OK

    paths = AppPaths.default().ensure()
    level = "DEBUG" if options.debug else configured_log_level(paths)
    setup_logging(paths.log_file, level, console=sys.stderr is not None)
    log.info("%s %s starting (Python %s, %s)", APP_NAME, __version__, platform.python_version(), platform.platform())

    verification, webengine = import_verification()
    set_app_user_model_id()
    app = create_application(options.qt_args)
    return _run(app, paths, options, verification, webengine)


def _run(app: Any, paths: AppPaths, options: Options, verification: ModuleType | None, webengine: bool) -> int:
    from anker_client.ui.crash import CrashHandler, install_qt_message_handler
    from anker_client.ui.single_instance import SingleInstance, allow_foreground_switch, server_name

    instance = SingleInstance(server_name(), Path(paths.config_dir) / "instance.lock")
    if not instance.acquire():
        allow_foreground_switch()
        delivered = instance.send("show")
        log.info("%s is already running; %s", APP_NAME,
                 "asked it to show its window" if delivered else "it did not answer")
        instance.close()
        return EXIT_OK

    crash = CrashHandler(paths.logs_dir).install()
    install_qt_message_handler()
    try:
        return _run_primary(app, paths, options, verification, webengine, crash, instance)
    finally:
        crash.uninstall()
        instance.close()
        log.info("%s exited", APP_NAME)


def _run_primary(app: Any, paths: AppPaths, options: Options, verification: ModuleType | None, webengine: bool,
                 crash: Any, instance: Any = None) -> int:
    from anker_client.services.container import build_context

    try:
        ctx = build_context(paths)
    except Exception as exc:
        log.exception("Could not initialise AnkerClient")
        import traceback

        show_fatal_error(
            f"{APP_NAME} could not start",
            f"{APP_NAME} could not open its data files.\n\n{exc}\n\nLogs: {paths.logs_dir}",
            "".join(traceback.format_exception(exc)),
        )
        return EXIT_STARTUP_FAILED

    from anker_client.ui.bridge import QtEventBridge
    from anker_client.ui.image_loader import ImageLoader
    from anker_client.ui.main_window import MainWindow
    from anker_client.ui.shell_startup import StartupTasks
    from anker_client.ui.theme.manager import ThemeManager

    theme = ThemeManager(app)
    _guarded("theme", lambda: theme.apply(ctx.settings.get().theme))
    bridge = QtEventBridge(ctx.events)
    loader = ImageLoader(ctx.images, ctx.runner)
    if verification is not None and webengine:
        _guarded("browser user agent", lambda: _apply_browser_user_agent(verification, ctx, paths))

    window: Any = None
    verifier: Any = None
    startup: Any = None
    try:
        try:
            window = MainWindow(ctx, bridge, theme, loader=loader, reset_window=options.reset_window)
        except Exception as exc:
            log.exception("Could not create the main window")
            show_fatal_error(f"{APP_NAME} could not start", f"The main window could not be created.\n\n{exc}")
            return EXIT_STARTUP_FAILED
        crash.set_parent_window(window)
        if instance is not None:
            # A second launch sends "show": raise this window (it may be hidden in the tray).
            instance.message_received.connect(window.handle_instance_message)
        if verification is not None:
            verifier = _guarded("verification browser", lambda: _install_verifier(verification, ctx, paths, window))
        _guarded("first-run wizard", lambda: _run_first_run_wizard(ctx, theme, window))
        _show_window(window, options, ctx)
        _guarded("services", ctx.start)
        _guarded("window refresh", window.on_services_started)
        startup = StartupTasks(ctx, window)
        startup.step_failed.connect(lambda name, exc: _on_startup_step_failed(window, name, exc))
        # The scan has loaded the update flags saved by earlier checks: those are not news.
        startup.chain_finished.connect(lambda _results: window.mark_updates_known())
        startup.start()
        return exec_application(app)
    finally:
        crash.set_parent_window(None)
        _shutdown(ctx, bridge, window, verifier, startup, release_browsers=webengine)


def _apply_browser_user_agent(verification: ModuleType, ctx: Any, paths: AppPaths) -> None:
    user_agent = verification.browser_user_agent(paths)
    if user_agent:
        ctx.http.set_user_agent(user_agent)
        log.info("Using the embedded browser's User-Agent for HTTP requests")


def _install_verifier(verification: ModuleType, ctx: Any, paths: AppPaths, window: Any) -> Any:
    verifier = verification.WebEngineVerifier(ctx.http, paths, parent_window=window)
    ctx.resolver.set_verifier(verifier)
    return verifier


def _run_first_run_wizard(ctx: Any, theme: Any, window: Any) -> None:
    if ctx.settings.get().first_run_completed:
        return
    from anker_client.ui.dialogs.first_run import FirstRunWizard

    wizard = FirstRunWizard(ctx, theme, window)
    try:
        wizard.exec()
    finally:
        wizard.deleteLater()


def _show_window(window: Any, options: Options, ctx: Any) -> None:
    start_minimized = options.minimized or ctx.settings.get().start_minimized
    if not start_minimized:
        window.show_and_raise()
    elif window.tray.available:
        log.info("Starting in the system tray")
    else:
        window.showMinimized()


def _on_startup_step_failed(window: Any, name: str, exc: BaseException) -> None:
    from anker_client.ui.async_ import error_text
    from anker_client.ui.shell_startup import STEP_CATALOG, STEP_LIBRARY

    if isinstance(exc, NotImplementedError):
        return
    if name == STEP_LIBRARY:
        window.toast(error_text(exc), "error", title="Your library could not be scanned")
    elif name == STEP_CATALOG:
        # CatalogUpdated is published even when the crawl fails; don't leave "Catalog up to date" showing.
        window.header.set_sync_failed(error_text(exc))


def _shutdown(ctx: Any, bridge: Any, window: Any, verifier: Any, startup: Any, *,
              release_browsers: bool = False) -> None:
    log.info("Shutting down")
    if startup is not None:
        _guarded("stop startup tasks", startup.stop)
    if window is not None:
        _guarded("window shutdown", window.shutdown)
        _guarded("hide window", window.hide)
    if verifier is not None:
        _guarded("verifier shutdown", verifier.shutdown)
    _guarded("event bridge", bridge.close)
    _guarded("services shutdown", ctx.shutdown)
    # Destroy the widgets (and the browser profiles they use) now, while the QApplication still
    # exists: signal connections keep the window referenced, so Python would otherwise only free it
    # during interpreter teardown, after the QApplication — a classic crash-on-exit.
    if window is not None:
        _guarded("delete window", window.deleteLater)
        _guarded("flush deletions", _flush_deferred_deletes)
    if release_browsers:
        _guarded("release embedded browsers", _release_embedded_browsers)
        _guarded("flush deletions", _flush_deferred_deletes)


def _flush_deferred_deletes() -> None:
    from PyQt6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _release_embedded_browsers() -> None:
    """``ui.dialogs.browser.release_browsers()`` — only once every browser window is gone."""
    browser = sys.modules.get("anker_client.ui.dialogs.browser")
    release = getattr(browser, "release_browsers", None)
    if callable(release):
        release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Last-resort error handling.

``CrashHandler.install()`` replaces ``sys.excepthook`` and
``threading.excepthook`` (and enables ``faulthandler`` into the logs folder for
native crashes). Every unhandled exception is logged with its traceback and
reported on the GUI thread in a dialog with "Copy details" / "Open logs
folder" / "Close". The application keeps running: PyQt6 would otherwise
abort the process on an exception escaping a slot.

Dialog storms are prevented: while one report is open further errors are
appended to it ("+2 more errors"; the details of at most
``MAX_APPENDED_REPORTS``), and after ``MAX_DIALOGS_PER_MINUTE`` reports only
the log receives them.

``install_qt_message_handler()`` routes Qt's own warnings into ``logging``.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from PyQt6.QtCore import QObject, Qt, QtMsgType, QUrl, pyqtSignal, qInstallMessageHandler
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from anker_client import __version__
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, label

log = logging.getLogger(__name__)

MAX_DIALOGS_PER_MINUTE = 3
MAX_APPENDED_REPORTS = 10  # reports appended to an open dialog's details; later ones only bump the counter

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def format_report(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None,
                  *, thread_name: str = "") -> str:
    where = f" in thread {thread_name}" if thread_name else ""
    header = f"AnkerClient {__version__} · Python {sys.version.split()[0]} · {sys.platform}{where}"
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    return f"{header}\n\n{body}".rstrip()


def summary_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


class CrashDialog(QDialog):
    """Modeless error report with copyable details."""

    def __init__(self, summary: str, details: str, logs_dir: Path | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AnkerClient — unexpected error")
        self.setModal(False)
        self.setMinimumWidth(560)
        self._logs_dir = logs_dir
        self._extra = 0
        pal = palette.current()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(14)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("error", 30, pal.danger))
        head.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(4)
        column.addWidget(label("Something went wrong", "title"))
        column.addWidget(label("AnkerClient hit an unexpected error but is still running. If something stops "
                               "working, restart the app. The details below were saved to the log.",
                               "muted", wrap=True))
        head.addLayout(column, 1)
        layout.addLayout(head)

        self.summary_label = label(summary, "error", wrap=True, selectable=True)
        layout.addWidget(self.summary_label)
        self.more_label = label("", "caption")
        self.more_label.setVisible(False)
        layout.addWidget(self.more_label)

        self.details = QPlainTextEdit(details)
        self.details.setProperty("role", "mono")
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setMinimumHeight(180)
        layout.addWidget(self.details, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.copy_button = button("Copy details", variant="secondary", on_click=self.copy_details)
        buttons.addWidget(self.copy_button)
        self.logs_button = button("Open logs folder", "folder", variant="ghost", on_click=self.open_logs)
        self.logs_button.setVisible(logs_dir is not None)
        buttons.addWidget(self.logs_button)
        buttons.addStretch(1)
        close = button("Close", variant="primary", on_click=self.accept)
        close.setDefault(True)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def add_report(self, summary: str, details: str) -> None:
        self._extra += 1
        self.more_label.setText(f"+{self._extra} more error{'s' if self._extra != 1 else ''} since this one "
                                "(see details)")
        self.more_label.setVisible(True)
        # An error in a paint handler repeats many times a second: keep the text bounded (all are logged).
        if self._extra <= MAX_APPENDED_REPORTS:
            self.details.appendPlainText(f"\n{'-' * 72}\n{summary}\n\n{details}")
        elif self._extra == MAX_APPENDED_REPORTS + 1:
            self.details.appendPlainText(f"\n{'-' * 72}\nFurther errors are only written to the log.")

    @property
    def extra_reports(self) -> int:
        return self._extra

    def copy_details(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.details.toPlainText())
        self.copy_button.setText("Copied")

    def open_logs(self) -> None:
        if self._logs_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._logs_dir)))


DialogFactory = Callable[[str, str, "Path | None", "QWidget | None"], QDialog]


class CrashHandler(QObject):
    """Installs the global exception hooks; reports on the GUI thread."""

    _report = pyqtSignal(str, str)

    def __init__(
        self,
        logs_dir: Path | None = None,
        parent_window: QWidget | None = None,
        *,
        show_dialogs: bool = True,
        dialog_factory: DialogFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._logs_dir = Path(logs_dir) if logs_dir else None
        self._parent_window = parent_window
        self._show_dialogs = show_dialogs
        self._dialog_factory: DialogFactory = dialog_factory or CrashDialog
        self._clock = clock
        self._dialog: QDialog | None = None
        self._shown_at: list[float] = []
        self._installed = False
        self._previous_hook: Callable[..., Any] | None = None
        self._previous_thread_hook: Callable[..., Any] | None = None
        self._fault_file: IO[str] | None = None
        self.reports = 0
        # AutoConnection: emitted from worker threads, delivered on the thread owning this object (GUI).
        self._report.connect(self._present)

    def set_parent_window(self, window: QWidget | None) -> None:
        self._parent_window = window

    # --- installation -------------------------------------------------------------------------
    def install(self) -> CrashHandler:
        if self._installed:
            return self
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook
        sys.excepthook = self.handle_exception
        threading.excepthook = self._handle_thread_exception
        self._enable_faulthandler()
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        if sys.excepthook == self.handle_exception:
            sys.excepthook = self._previous_hook or sys.__excepthook__
        if threading.excepthook == self._handle_thread_exception:
            threading.excepthook = self._previous_thread_hook or threading.__excepthook__
        if self._fault_file is not None:
            try:
                faulthandler.disable()
                self._fault_file.close()
            except (OSError, ValueError):
                pass
            self._fault_file = None
        self._installed = False

    def _enable_faulthandler(self) -> None:
        if self._logs_dir is None or faulthandler.is_enabled():
            return  # keep an existing setup (python -X faulthandler, pytest)
        try:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            # Kept open for the process lifetime: faulthandler writes to the raw fd during a native crash.
            self._fault_file = open(self._logs_dir / "native-crash.log", "a", encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(self._fault_file)
        except (OSError, RuntimeError, ValueError):
            log.debug("faulthandler unavailable", exc_info=True)

    # --- hooks ------------------------------------------------------------------------------------
    def handle_exception(self, exc_type: type[BaseException], exc: BaseException,
                         tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            (self._previous_hook or sys.__excepthook__)(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        self._emit(summary_line(exc), format_report(exc_type, exc, tb))

    def _handle_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical("Unhandled exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        self._emit(summary_line(args.exc_value),
                   format_report(args.exc_type, args.exc_value, args.exc_traceback, thread_name=name))

    def _emit(self, summary: str, details: str) -> None:
        self.reports += 1
        try:
            self._report.emit(summary, details)
        except RuntimeError:  # handler already destroyed during interpreter shutdown
            pass

    # --- GUI thread ----------------------------------------------------------------------------------
    def _present(self, summary: str, details: str) -> None:
        if not self._show_dialogs:
            return
        try:
            if self._dialog is not None and self._dialog.isVisible():
                add = getattr(self._dialog, "add_report", None)
                if callable(add):
                    add(summary, details)
                return
            now = self._clock()
            self._shown_at = [t for t in self._shown_at if now - t < 60.0]
            if len(self._shown_at) >= MAX_DIALOGS_PER_MINUTE:
                log.warning("Suppressing error dialog (too many errors in the last minute)")
                return
            self._shown_at.append(now)
            parent = self._parent_window if self._parent_window is not None and self._parent_window.isVisible() \
                else None
            dialog = self._dialog_factory(summary, details, self._logs_dir, parent)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.destroyed.connect(self._forget_dialog)
            self._dialog = dialog
            dialog.show()
            dialog.raise_()
        except Exception:  # the reporter itself must never raise into the excepthook
            log.exception("Could not show the error dialog")

    def _forget_dialog(self, *_args: object) -> None:
        self._dialog = None

    @property
    def dialog(self) -> QDialog | None:
        return self._dialog


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}

# Harmless noise Qt prints on Windows / offscreen; kept at DEBUG.
_QT_NOISE = (
    "QWindowsWindow::setGeometry",
    "This plugin does not support",
    "propagateSizeHints",
    "Unknown property",
)


_qt_handler: Callable[[QtMsgType, Any, str | None], None] | None = None


def install_qt_message_handler() -> None:
    global _qt_handler
    qt_log = logging.getLogger("qt")

    def handler(msg_type: QtMsgType, context: Any, message: str | None) -> None:
        text = message or ""
        level = _QT_LEVELS.get(msg_type, logging.INFO)
        if level >= logging.WARNING and any(noise in text for noise in _QT_NOISE):
            level = logging.DEBUG
        qt_log.log(level, "%s", text)

    _qt_handler = handler  # Qt calls it for the rest of the process: keep it referenced
    qInstallMessageHandler(handler)

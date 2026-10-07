"""Crash handler: logging, GUI-thread reporting, dialog coalescing/rate limiting, hook installation."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from anker_client.ui.crash import (
    MAX_DIALOGS_PER_MINUTE,
    CrashDialog,
    CrashHandler,
    format_report,
    install_qt_message_handler,
    summary_line,
)


class FakeDialog(QDialog):
    created: list[FakeDialog] = []

    def __init__(self, summary: str, details: str, logs_dir: Path | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.details = details
        self.extra: list[str] = []
        FakeDialog.created.append(self)

    def add_report(self, summary: str, details: str) -> None:
        self.extra.append(summary)


@pytest.fixture
def handler(qapp: QApplication, tmp_path: Path) -> CrashHandler:
    FakeDialog.created = []
    now = [1000.0]
    crash = CrashHandler(tmp_path / "logs", dialog_factory=FakeDialog, clock=lambda: now[0])
    crash.now = now  # type: ignore[attr-defined]
    yield crash
    crash.uninstall()
    for dialog in FakeDialog.created:
        if not sip.isdeleted(dialog):
            dialog.close()


def _raise(exc: BaseException) -> tuple[type[BaseException], BaseException, Any]:
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001
        return type(caught), caught, caught.__traceback__


def test_report_formatting() -> None:
    exc_type, exc, tb = _raise(ValueError("bad value\nsecond line"))
    report = format_report(exc_type, exc, tb, thread_name="worker-1")
    assert "AnkerClient" in report and "in thread worker-1" in report and "ValueError: bad value" in report
    assert summary_line(exc) == "ValueError: bad value"
    assert summary_line(RuntimeError()) == "RuntimeError"


def test_exception_is_logged_and_shown(qtbot: Any, handler: CrashHandler, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="anker_client.ui.crash"):
        handler.handle_exception(*_raise(KeyError("missing")))
    assert any(r.exc_info and r.levelno == logging.CRITICAL for r in caplog.records)
    qtbot.waitUntil(lambda: len(FakeDialog.created) == 1, timeout=2000)
    assert FakeDialog.created[0].summary == "KeyError: 'missing'"
    assert "Traceback" in FakeDialog.created[0].details


def test_errors_while_dialog_open_are_appended(qtbot: Any, handler: CrashHandler) -> None:
    handler.handle_exception(*_raise(ValueError("one")))
    qtbot.waitUntil(lambda: len(FakeDialog.created) == 1, timeout=2000)
    handler.handle_exception(*_raise(ValueError("two")))
    qtbot.waitUntil(lambda: FakeDialog.created[0].extra == ["ValueError: two"], timeout=2000)
    assert len(FakeDialog.created) == 1


def test_dialogs_are_rate_limited(qtbot: Any, handler: CrashHandler) -> None:
    for i in range(MAX_DIALOGS_PER_MINUTE + 2):
        handler.handle_exception(*_raise(ValueError(str(i))))
        qtbot.wait(20)
        if handler.dialog is not None:
            handler.dialog.close()
            qtbot.wait(20)
    assert len(FakeDialog.created) == MAX_DIALOGS_PER_MINUTE
    handler.now[0] += 61  # type: ignore[attr-defined]
    handler.handle_exception(*_raise(ValueError("later")))
    qtbot.waitUntil(lambda: len(FakeDialog.created) == MAX_DIALOGS_PER_MINUTE + 1, timeout=2000)


def test_thread_exceptions_reach_the_gui_thread(qtbot: Any, handler: CrashHandler,
                                                caplog: pytest.LogCaptureFixture) -> None:
    handler.install()

    def worker() -> None:
        raise RuntimeError("from a worker")

    with caplog.at_level(logging.CRITICAL, logger="anker_client.ui.crash"):
        thread = threading.Thread(target=worker, name="test-worker")
        thread.start()
        thread.join()
    qtbot.waitUntil(lambda: len(FakeDialog.created) == 1, timeout=2000)
    assert "in thread test-worker" in FakeDialog.created[0].details
    assert any("test-worker" in r.getMessage() for r in caplog.records)


def test_install_and_uninstall_restore_hooks(handler: CrashHandler) -> None:
    previous_sys, previous_thread = sys.excepthook, threading.excepthook
    handler.install()
    assert sys.excepthook == handler.handle_exception
    handler.install()  # idempotent
    handler.uninstall()
    assert sys.excepthook is previous_sys and threading.excepthook is previous_thread


def test_keyboard_interrupt_goes_to_previous_hook(handler: CrashHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[type[BaseException]] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: seen.append(t))
    handler.install()
    handler.handle_exception(*_raise(KeyboardInterrupt()))
    assert seen == [KeyboardInterrupt]
    assert handler.reports == 0


def test_dialogs_can_be_disabled(qtbot: Any, tmp_path: Path) -> None:
    FakeDialog.created = []
    crash = CrashHandler(tmp_path, show_dialogs=False, dialog_factory=FakeDialog)
    crash.handle_exception(*_raise(ValueError("quiet")))
    qtbot.wait(50)
    assert FakeDialog.created == [] and crash.reports == 1


def test_real_crash_dialog(qtbot: Any, tmp_path: Path) -> None:
    dialog = CrashDialog("ValueError: x", "Traceback...\nValueError: x", tmp_path)
    qtbot.addWidget(dialog)
    dialog.add_report("KeyError: y", "Traceback...\nKeyError: y")
    assert dialog.extra_reports == 1 and "KeyError: y" in dialog.details.toPlainText()
    assert not dialog.more_label.isHidden()
    dialog.copy_details()
    assert "KeyError: y" in QApplication.clipboard().text()
    assert dialog.copy_button.text() == "Copied"


def test_repeated_errors_keep_the_dialog_text_bounded(qtbot: Any, tmp_path: Path) -> None:
    from anker_client.ui.crash import MAX_APPENDED_REPORTS

    dialog = CrashDialog("ValueError: paint", "Traceback...", tmp_path)
    qtbot.addWidget(dialog)
    for i in range(MAX_APPENDED_REPORTS + 50):  # e.g. an exception in a paintEvent
        dialog.add_report(f"ValueError: paint {i}", "Traceback...\n" + "x" * 200)
    text = dialog.details.toPlainText()
    assert dialog.extra_reports == MAX_APPENDED_REPORTS + 50
    assert f"paint {MAX_APPENDED_REPORTS - 1}" in text and f"paint {MAX_APPENDED_REPORTS + 1}" not in text
    assert "only written to the log" in text
    assert str(MAX_APPENDED_REPORTS + 50) in dialog.more_label.text()


def test_qt_message_handler_stays_referenced(qapp: QApplication) -> None:
    import gc

    from PyQt6.QtCore import qInstallMessageHandler

    from anker_client.ui import crash

    install_qt_message_handler()
    try:
        gc.collect()
        assert crash._qt_handler is not None
    finally:
        qInstallMessageHandler(None)


def test_qt_messages_are_logged(qapp: QApplication, caplog: pytest.LogCaptureFixture) -> None:
    from PyQt6.QtCore import qInstallMessageHandler, qWarning

    install_qt_message_handler()
    try:
        with caplog.at_level(logging.DEBUG, logger="qt"):
            qWarning(b"shell test warning")
            qWarning(b"QWindowsWindow::setGeometry: noise")
        records = {r.getMessage(): r.levelno for r in caplog.records if r.name == "qt"}
        assert records.get("shell test warning") == logging.WARNING
        assert records.get("QWindowsWindow::setGeometry: noise") == logging.DEBUG
    finally:
        qInstallMessageHandler(None)

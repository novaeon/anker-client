"""Renders the Library and Downloads pages and the library dialogs (build/screens/library_ui_*.png).

The default run is a painting smoke test of both pages in the two reference
themes. ``ANKER_SCREENS=1`` also renders the full visual-QA matrix — every
page state and dialog in midnight, daylight and vaporwave — for review by eye.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from anker_client.core import events as ev
from anker_client.core.models import DownloadKind, ErrorKind, JobState
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.game_dialogs import (
    ExecutablePickerDialog,
    GamePropertiesDialog,
    ImportArchiveDialog,
    ImportFoldersDialog,
    _MatchDialog,
)
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.pages.downloads import DownloadsPage
from anker_client.ui.pages.library import LibraryPage
from anker_client.ui.theme.manager import ThemeManager
from tests.fakes import screenshot

pytestmark = pytest.mark.gui

FULL_MATRIX = os.environ.get("ANKER_SCREENS") == "1"
full_matrix = pytest.mark.skipif(not FULL_MATRIX, reason="full screenshot matrix (set ANKER_SCREENS=1)")
QA_THEMES = ["midnight", "daylight", "vaporwave"]

#: Kept alive for the session so late worker emissions never hit a destroyed QObject.
_KEEP_ALIVE: list[Any] = []


class _Nav:
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


@pytest.fixture
def themed(qapp) -> Iterator[Callable[[str], None]]:
    yield lambda key: ThemeManager(QApplication.instance()).apply(key)
    ThemeManager(QApplication.instance()).apply("midnight")


@pytest.fixture
def ui(qtbot, fake_ctx) -> Iterator[Any]:
    """Builds pages on ``fake_ctx`` (real generated covers) and tears everything down safely."""
    bridge = QtEventBridge(fake_ctx.events)
    loader = ImageLoader(fake_ctx.images, fake_ctx.runner)
    pages: list[Any] = []

    class Builder:
        ctx = fake_ctx

        def page(self, cls: type) -> Any:
            page = cls(fake_ctx, bridge, _Nav(), loader)
            qtbot.addWidget(page)
            pages.append(page)
            return page

        def dialog(self, dialog: QWidget) -> QWidget:
            qtbot.addWidget(dialog)
            return dialog

    yield Builder()
    for page in pages:
        page.shutdown()
        page.close()
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and widget.isWindow():
            widget.close()
    fake_ctx.runner.shutdown(wait=True)
    QApplication.processEvents()
    bridge.close()
    _KEEP_ALIVE.extend((bridge, loader))


def shoot(widget: QWidget, name: str, size: tuple[int, int] = (1280, 820)) -> None:
    path = screenshot(widget, f"library_ui_{name}", size=size)
    image = QImage(str(path))
    assert not image.isNull() and image.width() == size[0]


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, "timed out"
        QApplication.processEvents()
        time.sleep(0.01)


# --- default smoke renders ------------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
@pytest.mark.parametrize("page_name", ["library", "downloads"])
def test_render_page(ui, themed, theme, page_name):
    themed(theme)
    page = ui.page(LibraryPage if page_name == "library" else DownloadsPage)
    shoot(page, f"{page_name}_{theme}")
    assert page._loaded


# --- full visual-QA matrix (ANKER_SCREENS=1) -------------------------------------------------------------


@full_matrix
@pytest.mark.parametrize("theme", QA_THEMES)
def test_library_states(ui, themed, theme, monkeypatch):
    themed(theme)
    ctx = ui.ctx
    page = ui.page(LibraryPage)
    shoot(page, f"library_grid_{theme}")
    page._list_toggle.click()
    shoot(page, f"library_list_{theme}")
    page._grid_toggle.click()
    page.select("hades-ii")
    shoot(page, f"library_needs_setup_{theme}")
    ctx.events.publish(ev.GameLaunched("minecraft", "Minecraft"))
    page.select("minecraft")
    wait_for(lambda: "minecraft" in page._running)
    shoot(page, f"library_running_{theme}")
    page.select("hollow-knight")
    page._panel.set_busy("uninstall", True, "Uninstalling…")
    shoot(page, f"library_uninstalling_{theme}")
    page._panel.set_busy("uninstall", False)
    shoot(page, f"library_narrow_{theme}", size=(880, 640))
    page._search.setText("zzz")
    shoot(page, f"library_noresults_{theme}")
    page._search.clear()
    page.set_filter("hidden")
    shoot(page, f"library_nohidden_{theme}")

    def broken(**_kwargs):
        raise OSError("The library database is locked by another program.")

    monkeypatch.setattr(ctx.library, "games", broken)
    failing = ui.page(LibraryPage)
    wait_for(lambda: failing._error_message != "")
    shoot(failing, f"library_error_{theme}")
    monkeypatch.undo()
    ctx.library._games.clear()
    empty = ui.page(LibraryPage)
    shoot(empty, f"library_empty_{theme}")


@full_matrix
@pytest.mark.parametrize("theme", QA_THEMES)
def test_downloads_states(ui, themed, theme, monkeypatch):
    themed(theme)
    ctx = ui.ctx
    downloads = ctx.downloads
    for job in downloads._jobs.values():
        if job.state is JobState.WAITING:
            job.retry_at = time.time() + 42
            job.error_kind = ErrorKind.RATE_LIMITED
    broken = downloads._add(ctx.client.games[31], JobState.FAILED)
    broken.error = "The archive is damaged. Delete it and download again."
    broken.error_kind = ErrorKind.EXTRACTION
    broken.archive_path = r"C:\Games\.ankerclient\downloads\job\game.zip"
    broken.completed_at = "2026-10-05T18:00:00+00:00"
    downloads._add(ctx.client.games[30], JobState.COMPLETED)  # downloaded, waiting for Install
    page = ui.page(DownloadsPage)
    shoot(page, f"downloads_{theme}", size=(1280, 900))
    page._scroll.verticalScrollBar().setValue(page._scroll.verticalScrollBar().maximum())
    shoot(page, f"downloads_history_{theme}", size=(1280, 900))
    page.history_toggle.click()
    shoot(page, f"downloads_narrow_{theme}", size=(900, 700))

    seen: dict[str, Any] = {}

    def capture(box: QMessageBox) -> int:
        shoot(box, f"confirm_cancel_{theme}", size=(box.sizeHint().width(), box.sizeHint().height()))
        seen["done"] = True
        return 0

    monkeypatch.setattr(QMessageBox, "exec", capture)
    gears = next(j for j in downloads.jobs() if j.state is JobState.DOWNLOADING)
    page.row(gears.id).button_for("cancel").click()
    assert seen
    monkeypatch.undo()

    downloads._jobs.clear()
    empty = ui.page(DownloadsPage)
    shoot(empty, f"downloads_empty_{theme}")


@full_matrix
@pytest.mark.parametrize("theme", QA_THEMES)
def test_dialogs(ui, themed, theme, tmp_path, monkeypatch):
    themed(theme)
    ctx = ui.ctx
    game_dir = ctx.library.get("hollow-knight").path
    for name, size in (("Hollow Knight.exe", 18 * 1024**2), ("Launcher.exe", 2 * 1024**2)):
        with open(os.path.join(game_dir, name), "wb") as handle:
            handle.truncate(size)

    picker = ui.dialog(ExecutablePickerDialog(ctx, "hollow-knight"))
    picker.show()
    wait_for(lambda: picker.list.count() == 3)
    shoot(picker, f"exe_picker_{theme}", size=(600, 500))
    picker.close()

    props = ui.dialog(GamePropertiesDialog(ctx, "hollow-knight"))
    props.show()
    wait_for(lambda: props._stack.currentIndex() == 1)
    shoot(props, f"properties_{theme}", size=(640, 720))
    props.close()

    archive = tmp_path / "Hollow-Knight_v1.5.78.zip"
    archive.write_bytes(b"PK" + b"\0" * 4094)
    full = ui.dialog(ImportArchiveDialog(ctx, slug="hollow-knight", title="Hollow Knight", archive_path=str(archive)))
    full.show()
    wait_for(lambda: "replaces" in full.notice_text.text())
    shoot(full, f"import_archive_{theme}", size=(620, 700))
    full.close()

    patch = ui.dialog(ImportArchiveDialog(ctx, slug="celeste", title="Celeste", kind=DownloadKind.PATCH))
    patch.show()
    wait_for(lambda: "isn't installed" in patch.notice_text.text())
    shoot(patch, f"import_archive_patch_{theme}", size=(620, 700))
    patch.close()

    folders = ui.dialog(ImportFoldersDialog(ctx))
    folders.show()
    wait_for(lambda: folders.table.rowCount() == 1)
    shoot(folders, f"import_folders_{theme}", size=(780, 560))
    folders.close()

    summary = ctx.catalog.get("roadhouse-simulator")
    match = ui.dialog(_MatchDialog(ctx, "Roadhouse Simulator", summary))
    match.show()
    wait_for(match.use_button.isEnabled)
    shoot(match, f"match_dialog_{theme}", size=(480, 460))
    match.close()

    captured: list[bool] = []

    def capture(box: QMessageBox) -> int:
        shoot(box, f"confirm_uninstall_{theme}", size=(box.sizeHint().width(), box.sizeHint().height()))
        captured.append(True)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", capture)
    page = ui.page(LibraryPage)
    wait_for(lambda: page._loaded)
    page.select("hollow-knight")
    page._uninstall("hollow-knight")
    assert captured

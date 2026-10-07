"""7-Zip detection and the status panel shown in Settings → Downloads and the first-run wizard.

Detection runs ``SevenZip.locate(configured)`` then ``SevenZip(path).version()``
off the GUI thread. Any failure (including the install package not being
available) becomes a readable :class:`SevenZipInfo` instead of an exception.

A program the user browses to is only run (``7z i``) when its file name is one
of 7-Zip's (``7z.exe``, ``7za.exe``, ``7zz.exe``, or the window programs
``7zFM.exe``/``7zG.exe``, which are swapped for the ``7z.exe`` next to them):
running any other program with an argument could start a game or an installer.
Its banner must carry a version number; the console executable's path is the
one remembered.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from anker_client.core.tasks import CancelToken, TaskHandle, TaskRunner
from anker_client.services.install.sevenzip import SevenZip
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.widgets.common import button, label
from anker_client.ui.widgets.settings_controls import IconBinder, StatusLine, open_url

log = logging.getLogger(__name__)

SEVEN_ZIP_DOWNLOAD_URL = "https://www.7-zip.org/download.html"
#: File names of 7-Zip programs (console ones plus the window programs ``SevenZip`` maps to 7z.exe).
SEVEN_ZIP_NAMES = frozenset({"7z.exe", "7za.exe", "7zz.exe", "7zfm.exe", "7zg.exe", "7z", "7za", "7zz"})
_VERSION_NUMBER = re.compile(r"\d+\.\d+")


@dataclass(frozen=True, slots=True)
class SevenZipInfo:
    path: str = ""
    version: str = ""
    error: str = ""  # why it is unusable ("" when found and working)
    custom: bool = False  # path came from the user's setting

    @property
    def found(self) -> bool:
        return bool(self.path) and not self.error


def _version_of(path: str) -> tuple[str, str]:
    """``(console executable actually run, version banner)``."""
    seven = SevenZip(path)
    version = seven.version().strip()
    try:
        exe = str(seven.exe_path or path)
    except AttributeError:  # test doubles without the attribute
        exe = path
    return exe, version


def _is_configured(found: str, configured: str) -> bool:
    """Whether ``locate`` returned the user's setting (a 7z.exe, its folder, or 7zFM.exe beside it)."""
    if not configured:
        return False
    folder = os.path.dirname(found)
    return _same_path(found, configured) or _same_path(folder, configured) or (
        os.path.basename(configured).casefold() in SEVEN_ZIP_NAMES
        and _same_path(folder, os.path.dirname(configured))
    )


def detect_seven_zip(configured: str = "", *, token: CancelToken | None = None) -> SevenZipInfo:
    """Locate 7-Zip (configured path first) and read its version. Blocking."""
    try:
        path = SevenZip.locate(configured or None)
    except NotImplementedError:
        return SevenZipInfo(error="7-Zip detection is not available in this build.")
    except Exception as exc:
        log.warning("7-Zip detection failed", exc_info=True)
        return SevenZipInfo(error=f"7-Zip detection failed: {error_text(exc)}")
    if token is not None:
        token.raise_if_cancelled()
    if not path:
        return SevenZipInfo(error="7-Zip was not found on this PC.")
    custom = _is_configured(path, configured)
    try:
        _exe, version = _version_of(path)
    except Exception as exc:
        log.warning("7-Zip at %s did not start", path, exc_info=True)
        return SevenZipInfo(path=path, error=f"7-Zip was found but could not be started ({error_text(exc)}).",
                            custom=custom)
    return SevenZipInfo(path=path, version=version or "7-Zip", custom=custom)


def check_seven_zip(path: str, *, token: CancelToken | None = None) -> SevenZipInfo:
    """Validate a user-chosen executable. Blocking."""
    if os.path.basename(path).casefold() not in SEVEN_ZIP_NAMES:
        return SevenZipInfo(path=path, error="Choose 7z.exe in your 7-Zip folder (usually C:\\Program Files\\7-Zip).",
                            custom=True)
    try:
        exe, version = _version_of(path)
    except NotImplementedError:
        return SevenZipInfo(path=path, error="7-Zip checks are not available in this build.", custom=True)
    except Exception as exc:
        return SevenZipInfo(path=path, error=f"That file is not a working 7-Zip ({error_text(exc)}).", custom=True)
    lowered = version.lower()
    # SevenZip.version() falls back to a bare "7-Zip" for a banner it does not recognise.
    if not ("7-zip" in lowered or "p7zip" in lowered) or not _VERSION_NUMBER.search(version):
        return SevenZipInfo(path=path, error="That program does not look like 7-Zip.", custom=True)
    return SevenZipInfo(path=exe, version=version, custom=True)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class SevenZipPanel(QWidget):
    """Status of 7-Zip with "Browse…", "Detect automatically" and "Get 7-Zip"."""

    #: The user picked an executable that works (path).
    path_chosen = pyqtSignal(str)
    #: The user asked to forget the custom path and detect automatically.
    auto_detect_requested = pyqtSignal()
    #: Detection finished (SevenZipInfo).
    detected = pyqtSignal(object)

    def __init__(self, runner: TaskRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "transparent")
        self._runner = runner
        self._handle: TaskHandle[SevenZipInfo] | None = None
        self._info: SevenZipInfo | None = None
        self._binder = IconBinder()
        self.status = StatusLine("Looking for 7-Zip…", "busy")
        self.path_label = label("", "caption", wrap=True)
        self.path_label.hide()
        self.browse_button = button("Browse…", "folder", size="sm", on_click=self.browse)
        self.detect_button = button("Detect automatically", "refresh", size="sm",
                                    on_click=self.auto_detect_requested.emit)
        self.get_button = button("Get 7-Zip", "external", variant="ghost", size="sm",
                                 on_click=lambda: open_url(SEVEN_ZIP_DOWNLOAD_URL))
        self.get_button.hide()
        for btn, name in ((self.browse_button, "folder"), (self.detect_button, "refresh"),
                          (self.get_button, "external")):
            self._binder.bind(btn, name, "text", 16)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.browse_button)
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.get_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.status)
        layout.addWidget(self.path_label)
        layout.addSpacing(4)
        layout.addLayout(buttons)

    def info(self) -> SevenZipInfo | None:
        return self._info

    def detect(self, configured: str = "") -> None:
        """(Re)detect in the background; ``configured`` is the user's custom path or ""."""
        if self._handle is not None:
            self._handle.cancel()
        self.status.set_status("Looking for 7-Zip…", "busy")
        self.path_label.hide()
        self._handle = run_async(self, self._runner, detect_seven_zip, configured,
                                 on_result=self._show, on_error=self._on_error)

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate 7-Zip", "", "7-Zip (7z.exe 7za.exe 7zz.exe 7zFM.exe 7zG.exe)"
        )
        if path:
            self.check_path(path)

    def check_path(self, path: str) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self.status.set_status("Checking the selected program…", "busy")
        self._handle = run_async(self, self._runner, check_seven_zip, path,
                                 on_result=self._on_checked, on_error=self._on_error)

    def retint(self) -> None:
        self._binder.retint()
        self.status.retint()

    # --- results ----------------------------------------------------------------------
    def _on_checked(self, info: SevenZipInfo) -> None:
        if info.found:
            self._show(info)
            self.path_chosen.emit(info.path)
        else:
            self.status.set_status(info.error, "error")

    def _on_error(self, exc: BaseException) -> None:
        self._show(SevenZipInfo(error=error_text(exc)))

    def _show(self, info: SevenZipInfo) -> None:
        self._handle = None
        self._info = info
        if info.found:
            source = "Custom location" if info.custom else "Detected automatically"
            self.status.set_status(f"{info.version} is ready", "success")
            self.path_label.setText(f"{source} · {info.path}")
            self.path_label.show()
            self.get_button.hide()
        else:
            self.status.set_status(
                f"{info.error} AnkerClient needs 7-Zip to install games. Install it, then click Detect.",
                "warning",
            )
            self.path_label.setText(info.path)
            self.path_label.setVisible(bool(info.path))
            self.get_button.show()
        self.detected.emit(info)

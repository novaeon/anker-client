"""One row of the Downloads page: cover, title, state, progress, details and per-state actions.

The row is created once per job and updated in place (``update_job``) on
every ``JobUpdated`` — it only touches the labels/bars whose content changed
so a progress tick costs a handful of ``setText`` calls.

Pure helpers (``job_actions``, ``job_detail_text``, ``job_badge``,
``job_progress_state``) hold the presentation rules and are unit-tested
without widgets. Clicks are emitted as ``action_requested(action, job_id)``;
actions: pause, resume, retry, cancel, install, remove, move_up, move_down,
open_browser, import_archive, play, show_in_library. Buttons are laid out in
``job_actions`` order.

A failed job always offers "retry": ``DownloadManager.retry`` re-installs
from a complete archive and downloads again when the archive is gone, so the
row can never get stuck (``install`` silently ignores a failed job whose
archive was lost). The button reads "Retry install" when the extraction or
install step failed.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.formatting import format_bytes, format_duration, format_relative_time, format_speed
from anker_client.core.models import DownloadJob, DownloadKind, ErrorKind, JobState
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.widgets.common import Badge, button, label, repolish
from anker_client.ui.widgets.image_label import AsyncImage
from anker_client.ui.widgets.library_common import ElidedLabel, IconTinter

#: Error kinds after which the user can fetch the archive manually and import it.
MANUAL_DOWNLOAD_KINDS = frozenset({ErrorKind.EXTERNAL_HOST, ErrorKind.VERIFICATION})
#: Failures that happened after the download finished — the archive is still on disk.
INSTALL_FAILURE_KINDS = frozenset({ErrorKind.EXTRACTION, ErrorKind.INSTALL, ErrorKind.DISK_SPACE})

_WAIT_REASONS = {
    ErrorKind.RATE_LIMITED: "the site asked us to slow down",
    ErrorKind.NETWORK: "connection problem",
    ErrorKind.LINK_EXPIRED: "the link expired",
    ErrorKind.VERIFICATION: "verification failed",
}

_BADGE_KINDS = {
    JobState.QUEUED: "",
    JobState.RESOLVING: "accent",
    JobState.VERIFYING: "accent",
    JobState.DOWNLOADING: "accent",
    JobState.PAUSED: "",
    JobState.WAITING: "warning",
    JobState.EXTRACTING: "warning",
    JobState.INSTALLING: "warning",
    JobState.COMPLETED: "success",
    JobState.FAILED: "danger",
    JobState.CANCELLED: "",
}


# --- presentation rules ---------------------------------------------------------------------


def is_downloaded_not_installed(job: DownloadJob) -> bool:
    return job.state is JobState.COMPLETED and not job.install_path


def is_install_failure(job: DownloadJob) -> bool:
    return job.state is JobState.FAILED and job.error_kind in INSTALL_FAILURE_KINDS and bool(job.archive_path)


def job_actions(job: DownloadJob) -> list[str]:
    """Buttons shown for ``job``, in display order (main action first, icon buttons last)."""
    state = job.state
    if state is JobState.QUEUED:
        return ["pause", "move_up", "move_down", "cancel"]
    if state in (JobState.RESOLVING, JobState.VERIFYING, JobState.DOWNLOADING):
        return ["pause", "cancel"]
    if state is JobState.PAUSED:
        return ["resume", "cancel"]
    if state is JobState.WAITING:
        return ["retry", "pause", "cancel"]
    if state in (JobState.EXTRACTING, JobState.INSTALLING):
        return ["cancel"]
    if state is JobState.COMPLETED:
        if not job.install_path:
            return ["install", "remove"]
        return ["play", "show_in_library", "remove"]
    if state is JobState.FAILED:
        actions = ["retry"]
        if job.error_url:
            actions.append("open_browser")
        if job.error_kind in MANUAL_DOWNLOAD_KINDS:
            actions.append("import_archive")
        actions.append("remove")
        return actions
    return ["retry", "remove"]  # CANCELLED


def job_badge(job: DownloadJob) -> tuple[str, str]:
    """(text, badge kind) for the state pill."""
    if is_downloaded_not_installed(job):
        return "Downloaded", "accent"
    if job.state is JobState.COMPLETED:
        return "Installed", "success"
    return job.state.label, _BADGE_KINDS.get(job.state, "")


def job_progress_state(job: DownloadJob) -> str:
    """Value of the progress bar's ``state`` QSS property."""
    state = job.state
    if state in (JobState.PAUSED, JobState.WAITING, JobState.QUEUED, JobState.CANCELLED):
        return "paused"
    if state is JobState.FAILED:
        return "error"
    if state is JobState.COMPLETED:
        return "success"
    if state in (JobState.EXTRACTING, JobState.INSTALLING):
        return "install"
    return ""


def retry_text(job: DownloadJob) -> str:
    """Caption of the retry button."""
    if is_install_failure(job):
        return "Retry install"
    if job.state is JobState.WAITING:
        return "Retry now"
    return "Retry"


def _percent(value: float) -> int:
    return int(max(0.0, min(1.0, value)) * 100)


def _bytes_progress(job: DownloadJob) -> str:
    if job.bytes_total:
        return f"{format_bytes(job.bytes_done)} of {format_bytes(job.bytes_total)}"
    return f"{format_bytes(job.bytes_done)} downloaded"


def waiting_text(job: DownloadJob, now: float) -> str:
    reason = _WAIT_REASONS.get(job.error_kind) if job.error_kind else ""
    reason = reason or job.error
    if job.retry_at is None:
        return job.status_text or (f"Waiting to retry · {reason}" if reason else "Waiting to retry")
    remaining = job.retry_at - now
    text = f"Retrying in {format_duration(remaining)}" if remaining >= 0.5 else "Retrying now…"
    return f"{text} · {reason}" if reason else text


def job_detail_text(job: DownloadJob, now: float | None = None) -> str:
    """The muted line under the progress bar ("1.2 GB of 4.5 GB · 12.3 MB/s · 4m 09s left")."""
    now = time.time() if now is None else now
    state = job.state
    if state is JobState.DOWNLOADING:
        parts = [_bytes_progress(job)]
        if job.speed_bps > 0:
            parts.append(format_speed(job.speed_bps))
            if job.eta_seconds is not None and job.eta_seconds >= 0:
                parts.append(f"{format_duration(job.eta_seconds)} left")
        return " · ".join(parts)
    if state is JobState.QUEUED:
        return "Starts when a download slot is free"
    if state is JobState.RESOLVING:
        return job.status_text or "Preparing the download link…"
    if state is JobState.VERIFYING:
        return job.status_text or "Waiting for the AnkerGames browser check…"
    if state is JobState.PAUSED:
        if job.bytes_done:
            return f"Paused · {_bytes_progress(job)}"
        return "Paused"
    if state is JobState.WAITING:
        return waiting_text(job, now)
    if state in (JobState.EXTRACTING, JobState.INSTALLING):
        verb = "Extracting" if state is JobState.EXTRACTING else "Installing"
        return f"{verb} {_percent(job.phase_progress)}%"
    if state is JobState.COMPLETED:
        when = format_relative_time(job.completed_at) if job.completed_at else ""
        if not job.install_path:  # the caption above already shows the size
            return "Download finished · ready to install"
        return f"Installed {when}" if when else "Installed"
    if state is JobState.FAILED:
        return job.error or "The download failed."
    return "Cancelled"


_IMPORTED_LABELS = {
    DownloadKind.FULL: "Imported archive",
    DownloadKind.PATCH: "Imported update",
    DownloadKind.ADDON: "Imported add-on",
}


def job_option_text(job: DownloadJob) -> str:
    """Caption under the title: what is being installed + its size."""
    parts = []
    if job.imported_archive:
        parts.append(_IMPORTED_LABELS.get(job.option.kind, "Imported archive"))
    elif job.option.label:
        parts.append(job.option.label)
    if job.bytes_total:
        parts.append(format_bytes(job.bytes_total))
    return " · ".join(parts)


def percent_text(job: DownloadJob) -> str:
    started = job.bytes_done > 0 or job.state is JobState.DOWNLOADING
    if job.state in (JobState.DOWNLOADING, JobState.PAUSED, JobState.WAITING) and job.bytes_total and started:
        return f"{_percent(job.progress)}%"
    if job.state in (JobState.EXTRACTING, JobState.INSTALLING):
        return f"{_percent(job.phase_progress)}%"
    return ""


# --- widget -----------------------------------------------------------------------------------

_BUTTON_SPECS: dict[str, tuple[str, str, str, str]] = {
    # action: (text, icon, variant, tooltip) — empty text = icon-only tool button
    "pause": ("Pause", "pause", "", "Pause this download"),
    "resume": ("Resume", "resume", "primary", "Resume this download"),
    "retry": ("Retry", "retry", "", "Try again now"),
    "install": ("Install", "download", "primary", "Install the downloaded archive"),
    "play": ("Play", "play", "primary", "Start the game"),
    "show_in_library": ("Show in library", "library", "", "Open this game in your library"),
    "open_browser": ("Open in browser", "external", "", "Open the download page in your web browser"),
    "import_archive": ("Import archive…", "archive", "", "Install an archive you downloaded yourself"),
    "move_up": ("", "arrow_up", "", "Move up in the queue"),
    "move_down": ("", "arrow_down", "", "Move down in the queue"),
    "cancel": ("", "close", "", "Cancel download"),
    "remove": ("", "trash", "", "Remove from list"),
}
_ON_ACCENT = {"primary", "success"}


class JobRow(QFrame):
    action_requested = pyqtSignal(str, str)  # action, job id

    THUMB = QSize(48, 72)
    THUMB_COMPACT = QSize(32, 48)
    #: Minimum width of the action column, so progress bars line up across rows.
    BUTTON_COLUMN = 260

    def __init__(self, job: DownloadJob, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._job = job.copy()
        self._tint = IconTinter(self)
        self._actions: list[str] = []
        self._badge_kind: str | None = None
        self._bar_state: str | None = None
        self._compact: bool | None = None
        self._can_move_up = True
        self._can_move_down = True

        self.thumb = AsyncImage(loader, radius=4)
        self.thumb.setFixedSize(self.THUMB)
        self.thumb.set_image(job.cover_url, job.title)

        # The thumbnail column keeps one width in both sizes so titles line up across sections.
        thumb_box = QWidget()
        thumb_box.setFixedWidth(self.THUMB.width())
        thumb_layout = QHBoxLayout(thumb_box)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignCenter)

        self.title = ElidedLabel(job.title, "heading")
        self.badge = Badge("", "")
        self.option = label("", "caption")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.percent = label("", "muted")
        self.percent.setMinimumWidth(38)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.detail = label("", "muted")
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        title_row = QHBoxLayout()  # the state pill follows the title, wherever the buttons end
        title_row.setSpacing(8)
        title_row.addWidget(self.title)
        title_row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        bar_row.addWidget(self.progress, 1, Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(self.percent)
        self._bar_box = QWidget()
        self._bar_box.setLayout(bar_row)
        bar_row.setContentsMargins(0, 0, 0, 0)

        center = QVBoxLayout()
        center.setSpacing(4)
        center.addLayout(title_row)
        center.addWidget(self.option)
        center.addWidget(self._bar_box)
        center.addWidget(self.detail)

        self._buttons: dict[str, QAbstractButton] = {}
        button_box = QWidget()
        button_box.setMinimumWidth(self.BUTTON_COLUMN)
        buttons = QHBoxLayout(button_box)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self._button_layout = buttons
        for action, (text, icon_name, variant, tooltip) in _BUTTON_SPECS.items():
            btn = self._make_button(action, text, icon_name, variant, tooltip)
            btn.hide()
            self._buttons[action] = btn
            buttons.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 14, 10)
        root.setSpacing(14)
        root.addWidget(thumb_box, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(center, 1)
        root.addSpacing(8)
        root.addWidget(button_box)
        self._render(self._job, force=True)

    def _make_button(self, action: str, text: str, icon_name: str, variant: str, tooltip: str) -> QAbstractButton:
        btn: QAbstractButton
        if text:
            btn = button(text, variant=variant, size="sm", tooltip=tooltip)
            self._tint.set(btn, icon_name, tint="on_accent" if variant in _ON_ACCENT else "text", size=14)
        else:
            tool = QToolButton()
            tool.setProperty("variant", "icon")
            tool.setToolTip(tooltip)
            tool.setAutoRaise(True)
            tool.setCursor(Qt.CursorShape.PointingHandCursor)
            self._tint.set(tool, icon_name, tint="danger" if action == "cancel" else "muted", size=16)
            btn = tool
        btn.setObjectName(f"job_{action}")
        btn.setAccessibleName(tooltip)
        btn.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(a, self._job.id))
        return btn

    # --- public API ---------------------------------------------------------------------------
    @property
    def job(self) -> DownloadJob:
        return self._job

    @property
    def job_id(self) -> str:
        return self._job.id

    def update_job(self, job: DownloadJob) -> None:
        if job.id != self._job.id:
            raise ValueError("update_job() got a different job")
        self._render(job)
        self._job = job.copy()

    def set_queue_neighbours(self, *, can_move_up: bool, can_move_down: bool) -> None:
        self._can_move_up = can_move_up
        self._can_move_down = can_move_down
        self._buttons["move_up"].setEnabled(can_move_up)
        self._buttons["move_down"].setEnabled(can_move_down)

    def tick(self, now: float | None = None) -> None:
        """Refresh time-dependent text (the WAITING countdown)."""
        if self._job.state is JobState.WAITING:
            self._set_text(self.detail, job_detail_text(self._job, now))

    def button_for(self, action: str) -> QAbstractButton:
        return self._buttons[action]

    def visible_actions(self) -> list[str]:
        return list(self._actions)

    # --- rendering ------------------------------------------------------------------------------
    @staticmethod
    def _set_text(widget: QLabel, text: str) -> None:
        if widget.text() != text:
            widget.setText(text)

    def _render(self, job: DownloadJob, *, force: bool = False) -> None:
        old = self._job
        if force or job.title != old.title:
            self.title.set_full_text(job.title)
        if force or job.cover_url != old.cover_url:
            self.thumb.set_image(job.cover_url, job.title)

        text, kind = job_badge(job)
        self._set_text(self.badge, text)
        if kind != self._badge_kind:
            self._badge_kind = kind
            self.badge.set_kind(kind)

        compact = job.state.is_finished
        if compact != self._compact:
            self._compact = compact
            self._bar_box.setVisible(not compact)
            self.thumb.setFixedSize(self.THUMB_COMPACT if compact else self.THUMB)
            margins = (12, 8, 14, 8) if compact else (12, 10, 14, 10)
            self.layout().setContentsMargins(*margins)

        self._set_text(self.option, job_option_text(job))
        self._render_progress(job)
        detail = job_detail_text(job)
        self._set_text(self.detail, detail)
        self.detail.setToolTip(detail if len(detail) > 60 else "")
        role = "error" if job.state is JobState.FAILED else "muted"
        if self.detail.property("role") != role:
            self.detail.setProperty("role", role)
            repolish(self.detail)

        actions = job_actions(job)
        if actions != self._actions:
            self._arrange_buttons(actions)
            self._actions = actions
        retry = self._buttons["retry"]
        if isinstance(retry, QPushButton):
            self._set_button_text(retry, retry_text(job))
        self._buttons["move_up"].setEnabled(self._can_move_up)
        self._buttons["move_down"].setEnabled(self._can_move_down)
        self._buttons["cancel"].setToolTip(
            "Cancel installation" if job.state in (JobState.EXTRACTING, JobState.INSTALLING) else "Cancel download")

    def _arrange_buttons(self, actions: list[str]) -> None:
        """Show exactly ``actions``, laid out in that order after the leading stretch."""
        layout = self._button_layout
        for name, btn in self._buttons.items():
            if name not in actions:
                btn.hide()
        for position, name in enumerate(actions, start=1):  # index 0 is the stretch
            btn = self._buttons[name]
            if layout.indexOf(btn) != position:
                layout.removeWidget(btn)
                layout.insertWidget(position, btn, 0, Qt.AlignmentFlag.AlignVCenter)
            btn.show()

    @staticmethod
    def _set_button_text(btn: QPushButton, text: str) -> None:
        if btn.text() != text:
            btn.setText(text)

    def _render_progress(self, job: DownloadJob) -> None:
        bar_state = job_progress_state(job)
        if bar_state != self._bar_state:
            self._bar_state = bar_state
            self.progress.setProperty("state", bar_state)
            repolish(self.progress)
        indeterminate = job.state in (JobState.RESOLVING, JobState.VERIFYING) or (
            job.state is JobState.DOWNLOADING and not job.bytes_total)
        if indeterminate:
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() != 1000:
                self.progress.setRange(0, 1000)
            value = int(job.progress * 1000)
            if self.progress.value() != value:
                self.progress.setValue(value)
        self._set_text(self.percent, percent_text(job))

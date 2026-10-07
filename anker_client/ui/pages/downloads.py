"""Downloads page: live queue with per-job controls and history.

Layout (docs/ARCHITECTURE.md §UI "Downloads page")
* Header: "Downloads" + Pause all / Resume all / Clear finished and the
  speed-limit menu (Unlimited, 1/5/10/25/50 MB/s, Custom…) which writes
  ``settings.speed_limit_kbps``.
* Summary card: total download speed, active / queued counts, time left
  (hidden while loading and when there is nothing to show).
* "In progress": one :class:`JobRow` per unfinished job (plus downloads that
  finished but are not installed yet), ordered by queue position.
* "History": completed / failed / cancelled jobs, newest first; collapsible.
* States: loading, empty (+ Browse the store), error (+ Try again).

Live updates: rows are created once and updated in place from
``job_added`` / ``job_updated`` (never rebuilt per progress tick);
``job_removed`` drops a row; ``queue_changed`` re-reads ``downloads.jobs()``
on a worker and reconciles (reordering rows, keeping widgets). A snapshot
that raced with newer events never overwrites them — except for the queue
position, which only ever changes with ``queue_changed`` and is therefore
always taken from the snapshot. A 1 s clock refreshes the "Retrying in 42s"
countdown of WAITING jobs (no service polling).

Destructive actions are confirmed and name the game: Cancel, Remove of a
failed download that still has data on disk (the manager deletes it) or of a
download waiting for Install, and Clear finished when it deletes partial
data. "Clear finished" only clears the History section: downloads
waiting for Install are kept (``downloads.clear_finished`` would drop them,
so they are cleared one by one with ``downloads.remove`` instead).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anker_client.core.errors import ExecutableNotSetError
from anker_client.core.formatting import format_bytes, format_duration, format_speed, pluralize
from anker_client.core.models import DownloadJob, JobState
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.game_dialogs import ImportArchiveDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.navigator import Navigator
from anker_client.ui.widgets.common import EmptyState, LoadingOverlay, button, label
from anker_client.ui.widgets.job_row import JobRow, is_downloaded_not_installed
from anker_client.ui.widgets.library_common import (
    Connections,
    IconTinter,
    confirm,
    open_url,
    tokenless,
    widen_empty_state,
)

log = logging.getLogger(__name__)

#: Speed-limit presets in KB/s (``Settings.speed_limit_kbps``); 0 = unlimited.
SPEED_PRESETS_KBPS: tuple[int, ...] = (0, 1024, 5 * 1024, 10 * 1024, 25 * 1024, 50 * 1024)

_STACK_LOADING, _STACK_EMPTY, _STACK_CONTENT, _STACK_ERROR = range(4)
_PAUSABLE = frozenset({JobState.QUEUED, JobState.RESOLVING, JobState.VERIFYING, JobState.DOWNLOADING,
                       JobState.WAITING})


def format_limit(kbps: int) -> str:
    if kbps <= 0:
        return "Unlimited"
    if kbps % 1024 == 0:
        return f"{kbps // 1024} MB/s"
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps} KB/s"


def in_active_section(job: DownloadJob) -> bool:
    """Unfinished jobs, plus finished downloads that still need "Install"."""
    return not job.state.is_finished or is_downloaded_not_installed(job)


def _history_key(job: DownloadJob) -> str:
    return job.completed_at or job.updated_at or job.created_at


def leftover_bytes(job: DownloadJob) -> int | None:
    """Downloaded data that removing ``job`` deletes from disk (None = nothing is deleted).

    The download manager deletes a FAILED job's private download folder on remove; an
    imported archive is the user's own file and is never deleted.
    """
    if job.state is not JobState.FAILED or job.imported_archive:
        return None
    if job.bytes_done > 0:
        return job.bytes_done
    return 0 if job.archive_path else None


class _Stat(QWidget):
    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.caption = label(caption.upper(), "caption")
        self.value = label("—", "title")
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        if self.value.text() != text:
            self.value.setText(text)


class DownloadsPage(QWidget):
    RELOAD_DELAY_MS = 100

    def __init__(self, ctx: AppContext, bridge: QtEventBridge, nav: Navigator, loader: ImageLoader,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self._ctx = ctx
        self._bridge = bridge
        self._nav = nav
        self._loader = loader
        self._tint = IconTinter(self)

        self._jobs: dict[str, DownloadJob] = {}
        self._rows: dict[str, JobRow] = {}
        self._ordered_ids: list[str] = []  # every job by queue position (what ``move`` indexes into)
        self._loaded = False
        self._load_seq = 0
        self._load_handle: TaskHandle[Any] | None = None
        self._touched_since_load: set[str] = set()
        self._removed_since_load: set[str] = set()
        self._history_expanded = True
        self._active = True
        self._dialog: QDialog | None = None
        self._error_message = ""
        self._shut_down = False
        self._connections = Connections()

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(self.RELOAD_DELAY_MS)
        self._reload_timer.timeout.connect(self._reload)
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick)

        self._build()
        self._connect_bridge()
        self._tint.on_theme_changed(self._refresh_theme)
        self._render_speed_limit(ctx.settings.get().speed_limit_kbps)
        self._stack.setCurrentIndex(_STACK_LOADING)
        self._reload()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        self.pause_all_button = button("Pause all", on_click=self._pause_all)
        self._tint.set(self.pause_all_button, "pause", size=16)
        self.resume_all_button = button("Resume all", on_click=self._resume_all)
        self._tint.set(self.resume_all_button, "resume", size=16)
        self.clear_button = button("Clear finished", on_click=self._clear_finished)
        self._tint.set(self.clear_button, "trash", size=16)
        self.speed_button = button("Speed limit")
        self._tint.set(self.speed_button, "clock", size=16)
        self._speed_menu = QMenu(self.speed_button)
        self._speed_group = QActionGroup(self)
        self._speed_group.setExclusive(True)
        self._speed_actions: dict[int, QAction] = {}
        for kbps in SPEED_PRESETS_KBPS:
            act = self._speed_menu.addAction(format_limit(kbps))
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, k=kbps: self._set_speed_limit(k))
            self._speed_group.addAction(act)
            self._speed_actions[kbps] = act
        self._speed_menu.addSeparator()
        self._custom_action = self._speed_menu.addAction("Custom…")
        self._custom_action.setCheckable(True)
        self._custom_action.triggered.connect(lambda _c=False: self._choose_custom_limit())
        self._speed_group.addAction(self._custom_action)
        self.speed_button.setMenu(self._speed_menu)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(label("Downloads", "display"))
        header.addStretch(1)
        header.addWidget(self.pause_all_button)
        header.addWidget(self.resume_all_button)
        header.addWidget(self.clear_button)
        header.addWidget(self.speed_button)
        root.addLayout(header)

        self._summary_card = QFrame()
        self._summary_card.setProperty("role", "card")
        summary = QHBoxLayout(self._summary_card)
        summary.setContentsMargins(20, 14, 20, 14)
        summary.setSpacing(32)
        self.stat_speed = _Stat("Download speed")
        self.stat_active = _Stat("Active")
        self.stat_queued = _Stat("Queued")
        self.stat_eta = _Stat("Time left")
        for stat in (self.stat_speed, self.stat_active, self.stat_queued, self.stat_eta):
            summary.addWidget(stat)
        summary.addStretch(1)
        self.summary_text = label("", "muted")
        self.summary_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        summary.addWidget(self.summary_text)
        self._summary_card.hide()  # shown once there is something to summarise
        root.addWidget(self._summary_card)

        self._stack = QStackedWidget()
        self._stack.addWidget(LoadingOverlay("Loading downloads…"))
        self._empty = EmptyState("download", "No downloads yet",
                                 "Games you install from the store appear here while they download and install.",
                                 "Browse the store")
        self._empty.action_clicked.connect(self._nav.show_store)
        widen_empty_state(self._empty)
        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._build_lists())
        self._error_state = EmptyState("error", "Couldn't load your downloads", "", "Try again")
        self._error_state.action_clicked.connect(self._retry_load)
        widen_empty_state(self._error_state)
        self._stack.addWidget(self._error_state)
        root.addWidget(self._stack, 1)

    def _build_lists(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setProperty("role", "transparent")
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(10)

        self.active_heading = label("In progress", "heading")
        self.active_count = label("", "caption")
        heading_row = QHBoxLayout()
        heading_row.setSpacing(8)
        heading_row.addWidget(self.active_heading)
        heading_row.addWidget(self.active_count, 0, Qt.AlignmentFlag.AlignBottom)
        heading_row.addStretch(1)
        col.addLayout(heading_row)
        self._active_box = QVBoxLayout()
        self._active_box.setSpacing(8)
        col.addLayout(self._active_box)
        self.active_placeholder = label("Nothing is downloading right now.", "muted")
        self.active_placeholder.setContentsMargins(2, 4, 0, 4)
        col.addWidget(self.active_placeholder)

        col.addSpacing(10)
        self.history_toggle = QPushButton("History")
        self.history_toggle.setProperty("variant", "ghost")
        self.history_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_toggle.clicked.connect(self._toggle_history)
        history_row = QHBoxLayout()
        history_row.addWidget(self.history_toggle)
        history_row.addStretch(1)
        self._history_header = QWidget()
        self._history_header.setLayout(history_row)
        history_row.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self._history_header)
        self._history_container = QWidget()
        self._history_box = QVBoxLayout(self._history_container)
        self._history_box.setContentsMargins(0, 0, 0, 0)
        self._history_box.setSpacing(6)
        col.addWidget(self._history_container)
        col.addStretch(1)
        scroll.setWidget(body)
        self._scroll = scroll
        return scroll

    def _connect_bridge(self) -> None:
        bridge, connect = self._bridge, self._connections.connect
        connect(bridge.job_added, self._on_job_added)
        connect(bridge.job_updated, self._on_job_updated)
        connect(bridge.job_removed, self._on_job_removed)
        connect(bridge.queue_changed, self._on_queue_changed)
        connect(bridge.settings_changed, self._on_settings_changed)

    # ------------------------------------------------------------------ Page API
    def on_activated(self) -> None:
        self._active = True
        self._update_countdown()
        if self._loaded:
            self._schedule_reload()

    def on_deactivated(self) -> None:
        self._active = False
        self._countdown.stop()

    def shutdown(self) -> None:
        """Stop listening, stop the clock and cancel the pending read; no new work afterwards."""
        self._shut_down = True
        self._connections.disconnect_all()
        self._reload_timer.stop()
        self._countdown.stop()
        if self._load_handle is not None:
            self._load_handle.cancel()

    def row(self, job_id: str) -> JobRow | None:
        """The row widget for ``job_id`` (used by tests and the shell's "show job")."""
        return self._rows.get(job_id)

    # ------------------------------------------------------------------ loading
    def _schedule_reload(self) -> None:
        if not self._shut_down:
            self._reload_timer.start()

    def _retry_load(self) -> None:
        self._stack.setCurrentIndex(_STACK_LOADING)
        self._reload()

    def _reload(self) -> None:
        self._reload_timer.stop()
        if self._shut_down:
            return
        self._load_seq += 1
        seq = self._load_seq
        self._touched_since_load.clear()
        self._removed_since_load.clear()
        if self._load_handle is not None:
            self._load_handle.cancel()
        self._load_handle = run_async(
            self, self._ctx.runner, tokenless(self._ctx.downloads.jobs),
            on_result=lambda jobs, s=seq: self._on_loaded(s, jobs),
            on_error=lambda exc, s=seq: self._on_load_failed(s, exc),
        )

    def _on_loaded(self, seq: int, jobs: list[DownloadJob]) -> None:
        if seq != self._load_seq:
            return
        self._loaded = True
        snapshot = {job.id: job for job in jobs}
        for job in jobs:
            if job.id in self._removed_since_load:
                continue
            if job.id in self._touched_since_load:
                # An event newer than this snapshot already updated the job, but reorders publish no
                # per-job event: the queue position is only ever current in the snapshot.
                current = self._jobs.get(job.id)
                if current is not None and current.position != job.position:
                    self._jobs[job.id] = replace(current, position=job.position)
                continue
            self._apply_job(job)
        for job_id in list(self._rows):
            if job_id not in snapshot and job_id not in self._touched_since_load:
                self._drop_row(job_id)
        self._ordered_ids = [j.id for j in sorted(self._jobs.values(), key=lambda j: j.position)]
        self._layout_rows()
        self._refresh_chrome()

    def _on_load_failed(self, seq: int, exc: BaseException) -> None:
        if seq != self._load_seq:
            return
        log.warning("Loading downloads failed: %s", exc)
        if self._loaded:
            self._nav.toast(f"Couldn't refresh downloads: {error_text(exc)}", "error")
            return
        self._error_message = error_text(exc)
        self._error_state.set_content("error", "Couldn't load your downloads", self._error_message, "Try again")
        self._stack.setCurrentIndex(_STACK_ERROR)
        self._summary_card.hide()

    # ------------------------------------------------------------------ rows
    def _apply_job(self, job: DownloadJob) -> bool:
        """Create or update the row for ``job``; True when its placement must be recomputed."""
        previous = self._jobs.get(job.id)
        self._jobs[job.id] = job
        row = self._rows.get(job.id)
        if row is None:
            row = JobRow(job, self._loader)
            row.action_requested.connect(self._on_row_action)
            self._rows[job.id] = row
            return True
        row.update_job(job)
        if previous is None:
            return True
        return (in_active_section(previous) != in_active_section(job) or previous.position != job.position
                or previous.state is not job.state or _history_key(previous) != _history_key(job))

    def _drop_row(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        row = self._rows.pop(job_id, None)
        if row is not None:
            row.hide()
            row.setParent(None)
            row.deleteLater()
        if job_id in self._ordered_ids:
            self._ordered_ids.remove(job_id)

    def _layout_rows(self) -> None:
        jobs = list(self._jobs.values())
        active = sorted((j for j in jobs if in_active_section(j)), key=lambda j: (j.position, j.created_at))
        history = sorted((j for j in jobs if not in_active_section(j)), key=_history_key, reverse=True)
        self._place(self._active_box, [self._rows[j.id] for j in active])
        self._place(self._history_box, [self._rows[j.id] for j in history])
        self._update_queue_neighbours(active)

    @staticmethod
    def _place(box: QVBoxLayout, rows: list[JobRow]) -> None:
        current = [box.itemAt(i).widget() for i in range(box.count())]
        if current == rows:
            return
        for widget in current:
            box.removeWidget(widget)
        for index, row in enumerate(rows):
            box.insertWidget(index, row)
            row.show()

    def _update_queue_neighbours(self, active: list[DownloadJob]) -> None:
        queued = [j.id for j in active if j.state is JobState.QUEUED]
        for index, job_id in enumerate(queued):
            self._rows[job_id].set_queue_neighbours(can_move_up=index > 0, can_move_down=index < len(queued) - 1)

    # ------------------------------------------------------------------ bridge
    def _on_job_added(self, job: DownloadJob) -> None:
        self._on_job_event(job)

    def _on_job_updated(self, job: DownloadJob) -> None:
        self._on_job_event(job)

    def _on_job_event(self, job: DownloadJob) -> None:
        self._touched_since_load.add(job.id)
        self._removed_since_load.discard(job.id)
        if job.id not in self._ordered_ids:
            self._ordered_ids.append(job.id)
        if self._apply_job(job):
            self._ordered_ids.sort(key=lambda i: self._jobs[i].position if i in self._jobs else 1 << 30)
            self._layout_rows()
            self._refresh_chrome()
        else:
            self._refresh_summary()
            if job.state is JobState.WAITING:
                self._update_countdown()

    def _on_job_removed(self, job_id: str) -> None:
        self._removed_since_load.add(job_id)
        self._touched_since_load.discard(job_id)
        if job_id in self._rows or job_id in self._jobs:
            self._drop_row(job_id)
            self._layout_rows()
            self._refresh_chrome()

    def _on_queue_changed(self) -> None:
        self._schedule_reload()

    def _on_settings_changed(self, keys: Any) -> None:
        if "speed_limit_kbps" in set(keys or ()):
            self._render_speed_limit(self._ctx.settings.get().speed_limit_kbps)

    # ------------------------------------------------------------------ chrome
    def _refresh_chrome(self) -> None:
        jobs = list(self._jobs.values())
        if not self._loaded:
            return
        if not jobs:
            self._stack.setCurrentIndex(_STACK_EMPTY)
        else:
            self._stack.setCurrentIndex(_STACK_CONTENT)
        self._summary_card.setVisible(bool(jobs))
        active = [j for j in jobs if in_active_section(j)]
        history = [j for j in jobs if not in_active_section(j)]
        self.active_count.setText(pluralize(len(active), "item") if active else "")
        self.active_placeholder.setVisible(not active)
        self._history_header.setVisible(bool(history))
        self._history_container.setVisible(bool(history) and self._history_expanded)
        self._render_history_toggle(len(history))
        self.clear_button.setEnabled(any(j.state.is_finished and not is_downloaded_not_installed(j) for j in jobs))
        self._refresh_summary()
        self._update_countdown()

    def _refresh_summary(self) -> None:
        jobs = list(self._jobs.values())
        downloading = [j for j in jobs if j.state is JobState.DOWNLOADING]
        speed = sum(max(0.0, j.speed_bps) for j in downloading)
        active = sum(1 for j in jobs if j.state.is_active)
        queued = sum(1 for j in jobs if j.state in (JobState.QUEUED, JobState.WAITING))
        paused = sum(1 for j in jobs if j.state is JobState.PAUSED)
        remaining = sum(max(0, (j.bytes_total or 0) - j.bytes_done) for j in downloading if j.bytes_total)
        self.stat_speed.set(format_speed(speed))
        self.stat_active.set(str(active))
        self.stat_queued.set(str(queued))
        self.stat_eta.set(format_duration(remaining / speed) if speed > 0 and remaining else "—")
        parts = []
        if paused:
            parts.append(f"{paused} paused")
        failed = sum(1 for j in jobs if j.state is JobState.FAILED)
        if failed:
            parts.append(f"{failed} failed")
        ready = sum(1 for j in jobs if is_downloaded_not_installed(j))
        if ready:
            parts.append(f"{ready} ready to install")
        if remaining:
            parts.append(f"{format_bytes(remaining)} to go")
        self.summary_text.setText(" · ".join(parts))
        self.pause_all_button.setEnabled(any(j.state in _PAUSABLE for j in jobs))
        self.resume_all_button.setEnabled(paused > 0)

    def _render_history_toggle(self, count: int) -> None:
        self.history_toggle.setText(f"History  ·  {count}")
        self._tint.set(self.history_toggle, "chevron_down" if self._history_expanded else "chevron_right",
                       tint="muted", size=16)

    def _toggle_history(self) -> None:
        self._history_expanded = not self._history_expanded
        self._refresh_chrome()

    def _update_countdown(self) -> None:
        waiting = any(j.state is JobState.WAITING and j.retry_at is not None for j in self._jobs.values())
        if waiting and self._active and not self._shut_down:
            if not self._countdown.isActive():
                self._countdown.start()
        else:
            self._countdown.stop()

    def _tick(self) -> None:
        any_waiting = False
        for job_id, job in self._jobs.items():
            if job.state is JobState.WAITING:
                any_waiting = True
                row = self._rows.get(job_id)
                if row is not None:
                    row.tick()
        if not any_waiting:
            self._countdown.stop()

    def _refresh_theme(self) -> None:
        self._empty.set_content("download", "No downloads yet",
                                "Games you install from the store appear here while they download and install.",
                                "Browse the store")
        self._error_state.set_content("error", "Couldn't load your downloads", self._error_message, "Try again")

    # ------------------------------------------------------------------ speed limit
    def _render_speed_limit(self, kbps: int) -> None:
        self.speed_button.setText(f"Limit: {format_limit(kbps)}")
        preset = self._speed_actions.get(kbps)
        if preset is not None:
            preset.setChecked(True)
            self._custom_action.setText("Custom…")
        else:
            self._custom_action.setChecked(True)
            self._custom_action.setText(f"Custom ({format_limit(kbps)})…")

    def _set_speed_limit(self, kbps: int) -> None:
        kbps = max(0, int(kbps))
        self._render_speed_limit(kbps)
        run_async(self, self._ctx.runner, tokenless(self._ctx.settings.update, speed_limit_kbps=kbps),
                  on_error=lambda exc: self._nav.toast(f"Couldn't change the speed limit: {error_text(exc)}",
                                                       "error"))

    def _ask_custom_limit(self, current_mb: float) -> float | None:
        value, ok = QInputDialog.getDouble(self, "Download speed limit", "Maximum download speed in MB/s:",
                                           current_mb or 8.0, 0.1, 10_000.0, 1)
        return value if ok else None

    def _choose_custom_limit(self) -> None:
        current = self._ctx.settings.get().speed_limit_kbps
        value = self._ask_custom_limit(current / 1024 if current else 0.0)
        if value is None:
            self._render_speed_limit(current)  # restore the checked entry
            return
        self._set_speed_limit(round(value * 1024))

    # ------------------------------------------------------------------ queue commands
    def _call(self, verb: str, fn: Any, *args: Any, title: str = "") -> None:
        what = f" {title}" if title else ""
        run_async(self, self._ctx.runner, tokenless(fn, *args),
                  on_error=lambda exc: self._nav.toast(f"Couldn't {verb}{what}: {error_text(exc)}", "error"))

    def _pause_all(self) -> None:
        self._call("pause downloads", self._ctx.downloads.pause_all)

    def _resume_all(self) -> None:
        self._call("resume downloads", self._ctx.downloads.resume_all)

    def _clear_finished(self) -> None:
        history = [j for j in self._jobs.values() if not in_active_section(j)]
        if not history:
            return
        leftovers = [(j, size) for j in history if (size := leftover_bytes(j)) is not None]
        if leftovers:
            names = ", ".join(j.title for j, _size in leftovers[:3]) + ("…" if len(leftovers) > 3 else "")
            total = sum(size for _j, size in leftovers)
            amount = f" ({format_bytes(total)})" if total else ""
            if not confirm(
                self, title="Clear finished downloads", text=f"Clear {pluralize(len(history), 'finished download')}?",
                informative=f"The partial data of failed downloads{amount} will be deleted: {names}.",
                confirm_text="Clear and delete",
            ):
                return
        downloads = self._ctx.downloads
        if any(is_downloaded_not_installed(j) for j in self._jobs.values()):
            # clear_finished() would also drop downloads that are still waiting for "Install".
            ids = [j.id for j in history]

            def remove_each(*, token: CancelToken) -> None:
                for job_id in ids:
                    token.raise_if_cancelled()
                    downloads.remove(job_id)

            run_async(self, self._ctx.runner, remove_each,
                      on_error=lambda exc: self._nav.toast(f"Couldn't clear finished downloads: {error_text(exc)}",
                                                           "error"))
            return
        self._call("clear finished downloads", downloads.clear_finished)

    def _on_row_action(self, action: str, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        downloads = self._ctx.downloads
        simple = {
            "pause": ("pause", downloads.pause),
            "resume": ("resume", downloads.resume),
            "retry": ("retry", downloads.retry),
            "install": ("install", downloads.install),
        }
        if action in simple:
            verb, fn = simple[action]
            self._call(verb, fn, job_id, title=job.title)
        elif action == "cancel":
            self._cancel(job)
        elif action == "remove":
            self._remove(job)
        elif action in ("move_up", "move_down"):
            self._move(job, up=action == "move_up")
        elif action == "open_browser":
            if not open_url(job.error_url):
                self._nav.toast("Couldn't open your web browser.", "error")
        elif action == "import_archive":
            self._open_import(job)
        elif action in ("play", "show_in_library"):
            self._with_install(job, play=action == "play")

    def _cancel(self, job: DownloadJob) -> None:
        installing = job.state in (JobState.EXTRACTING, JobState.INSTALLING)
        if installing:
            text = f"Stop installing {job.title}?"
            detail = "Files extracted so far are removed."
            if not job.imported_archive:
                detail += " The downloaded archive is deleted too."
            confirm_text, keep_text = "Stop install", "Keep installing"
        else:
            text = f"Cancel the download of {job.title}?"
            if job.imported_archive:
                detail = "Your archive file is kept."
            elif job.bytes_done:
                detail = f"The {format_bytes(job.bytes_done)} downloaded so far will be deleted."
            else:
                detail = "Nothing has been downloaded yet."
            confirm_text, keep_text = "Cancel download", "Keep downloading"
        if confirm(self, title="Cancel download", text=text, informative=detail, confirm_text=confirm_text,
                   cancel_text=keep_text):
            self._call("cancel", self._ctx.downloads.cancel, job.id, title=job.title)

    def _remove(self, job: DownloadJob) -> None:
        leftover = leftover_bytes(job)
        if is_downloaded_not_installed(job):
            confirmed = confirm(
                self, title="Remove download", text=f"Remove {job.title} from the list?",
                informative="It has been downloaded but not installed yet. You won't be able to install it from "
                            "here afterwards.",
                confirm_text="Remove",
            )
        elif leftover is not None:
            amount = f"The {format_bytes(leftover)} downloaded so far" if leftover else "The downloaded data"
            confirmed = confirm(
                self, title="Remove download", text=f"Remove {job.title} from the list?",
                informative=f"{amount} will be deleted. Use Retry instead to continue the download later.",
                confirm_text="Remove and delete",
            )
        else:
            confirmed = True
        if confirmed:
            self._call("remove", self._ctx.downloads.remove, job.id, title=job.title)

    def _move(self, job: DownloadJob, *, up: bool) -> None:
        ordered = [i for i in self._ordered_ids if i in self._jobs]
        if job.id not in ordered:
            return
        queued = [i for i in ordered if self._jobs[i].state is JobState.QUEUED]
        index = queued.index(job.id) if job.id in queued else -1
        neighbour_index = index - 1 if up else index + 1
        if index < 0 or not 0 <= neighbour_index < len(queued):
            return
        target = ordered.index(queued[neighbour_index])
        self._call("reorder", self._ctx.downloads.move, job.id, target, title=job.title)

    def _open_import(self, job: DownloadJob) -> None:
        # The job's kind matters: a patch imported as a full game would replace the installation.
        dialog = ImportArchiveDialog(self._ctx, self, slug=job.slug, title=job.title, kind=job.option.kind)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.accepted.connect(lambda: self._nav.toast(f"Installing {job.title} from your archive.", "success"))
        self._dialog = dialog
        dialog.open()

    def _with_install(self, job: DownloadJob, *, play: bool) -> None:
        library = self._ctx.library

        def work(*, token: CancelToken) -> str:
            game = library.find_by_path(job.install_path) if job.install_path else None
            token.raise_if_cancelled()
            if game is None and job.slug:
                game = library.find_by_slug(job.slug)
            return game.install_id if game is not None else ""

        def found(install_id: str) -> None:
            if not install_id:
                self._nav.toast(f"{job.title} is no longer installed.", "warning")
            elif play:
                self._launch(install_id, job.title)
            else:
                self._nav.show_library(install_id)

        run_async(self, self._ctx.runner, work, on_result=found,
                  on_error=lambda exc: self._nav.toast(error_text(exc), "error"))

    def _launch(self, install_id: str, title: str) -> None:
        def failed(exc: BaseException) -> None:
            if isinstance(exc, ExecutableNotSetError):
                self._nav.choose_executable(install_id)
            else:
                self._nav.toast(f"Couldn't start {title}: {error_text(exc)}", "error")

        run_async(self, self._ctx.runner, tokenless(self._ctx.launcher.launch, install_id), on_error=failed)

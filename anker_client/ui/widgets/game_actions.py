"""The Game page's action card and the pure state machine behind it.

:func:`derive_action_state` maps (details, installed game, download job,
running, loading) to one :class:`ActionKind`, in priority order:

1. ``RUNNING``  — the game is running → "Stop".
2. ``JOB``      — an unfinished download/install job exists → progress,
   Pause/Resume/Cancel ("View in Downloads").
3. ``UPDATE``   — installed and an update is known → "Update v1 → v2"
   ("Update to v2" / "Update" when versions are unknown). When both the
   installed and the site version are known they decide (the update checker's
   rule: the site version is newer), so a stale library flag never offers
   "v1.1 → v1.1"; otherwise the library's ``update_available`` flag decides.
   Uses the PATCH option whose ``from_version`` equals the installed version
   (``find_patch_option``), else the full download. Disabled until details load.
4. ``SETUP``    — installed but no executable chosen → "Choose executable".
5. ``PLAY``     — installed → "Play" (+ Manage menu).
6. ``LOADING``  — not installed and details still loading.
7. ``UNAVAILABLE`` — details failed, or the game lists no downloads.
8. ``INSTALL``  — "Install" with the primary FULL option; an options menu
   lists every option when there are several (PATCH/ADDON entries are disabled
   until the base game is installed).

A FAILED job for this game (kept by the page until a new attempt) adds an
error line with Retry above the primary button. While a job waits to retry,
its detail line counts down to ``retry_at`` once a second; the manager's own
status text loses its static "in 42s" so the two never disagree.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from PyQt6.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QMenu, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from anker_client.core.formatting import (
    compare_versions,
    format_bytes,
    format_duration,
    format_playtime,
    format_speed,
    normalize_version,
    versions_differ,
)
from anker_client.core.models import DownloadJob, DownloadKind, DownloadOption, GameDetails, InstalledGame, JobState
from anker_client.ui import icons
from anker_client.ui.theme import palette
from anker_client.ui.widgets.common import button, hbox, label, repolish
from anker_client.ui.widgets.game_common import display_version, relative_day, tag_icon
from anker_client.ui.widgets.game_sections import option_title
from anker_client.ui.widgets.store_cards import job_percent


class ActionKind(StrEnum):
    LOADING = "loading"
    UNAVAILABLE = "unavailable"
    INSTALL = "install"
    JOB = "job"
    UPDATE = "update"
    SETUP = "setup"
    PLAY = "play"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ActionState:
    kind: ActionKind
    option: DownloadOption | None = None  # INSTALL: primary option; UPDATE: patch or full option
    options: tuple[DownloadOption, ...] = ()  # INSTALL: every option (menu when > 1)
    installed: InstalledGame | None = None
    job: DownloadJob | None = None
    failed_job: DownloadJob | None = None
    installed_version: str = ""
    latest_version: str = ""
    is_patch: bool = False
    details_available: bool = False
    size_text: str = ""  # download size of ``option`` when known


def find_patch_option(details: GameDetails | None, installed_version: str,
                      latest_version: str = "") -> DownloadOption | None:
    """The PATCH option that applies to ``installed_version`` (prefer one ending at ``latest_version``)."""
    if details is None:
        return None
    current = normalize_version(installed_version)
    if not current:
        return None
    candidates = [o for o in details.download_options
                  if o.kind is DownloadKind.PATCH and normalize_version(o.from_version) == current]
    latest = normalize_version(latest_version)
    for option in candidates:
        if latest and normalize_version(option.to_version) == latest:
            return option
    return candidates[0] if candidates else None


def update_versions(details: GameDetails | None, installed: InstalledGame | None) -> tuple[bool, str]:
    """(update available?, latest version) for an installed game."""
    if installed is None or not installed.managed:
        return False, ""
    site_version = details.version if details is not None else ""
    latest = site_version or installed.latest_version
    if normalize_version(installed.version) and normalize_version(site_version):
        newer = versions_differ(installed.version, site_version) and \
            compare_versions(installed.version, site_version) <= 0
        return newer, site_version
    return installed.update_available, latest


def derive_action_state(
    details: GameDetails | None,
    installed: InstalledGame | None,
    job: DownloadJob | None = None,
    *,
    running: bool = False,
    details_loading: bool = False,
    failed_job: DownloadJob | None = None,
) -> ActionState:
    common: dict[str, Any] = {
        "installed": installed,
        "failed_job": failed_job,
        "details_available": details is not None,
        "installed_version": installed.version if installed else "",
    }
    if running and installed is not None:
        return ActionState(ActionKind.RUNNING, **common)
    if job is not None and not job.state.is_finished:
        return ActionState(ActionKind.JOB, job=job, **common)
    if installed is not None:
        has_update, latest = update_versions(details, installed)
        if has_update:
            patch = find_patch_option(details, installed.version, latest)
            option = patch or (details.primary_option if details is not None else None)
            if option is not None and option.kind is not DownloadKind.FULL and patch is None:
                option = None  # never offer an add-on as "the update"
            size = option.size_text if option is not None else ""
            if not size and patch is None and details is not None:
                size = details.size_text  # the full download is the whole game
            return ActionState(ActionKind.UPDATE, option=option, latest_version=latest,
                               is_patch=patch is not None, size_text=size, **common)
        if not installed.executable:
            return ActionState(ActionKind.SETUP, latest_version=latest, **common)
        return ActionState(ActionKind.PLAY, latest_version=latest, **common)
    if details is None:
        return ActionState(ActionKind.LOADING if details_loading else ActionKind.UNAVAILABLE, **common)
    if not details.download_options:
        return ActionState(ActionKind.UNAVAILABLE, **common)
    primary = details.primary_option
    size = primary.size_text if primary is not None else ""
    if not size and primary is not None and primary.kind is DownloadKind.FULL:
        size = details.size_text
    return ActionState(ActionKind.INSTALL, option=primary, options=tuple(details.download_options),
                       size_text=size, **common)


_COUNTDOWN_RE = re.compile(r"\b(?:in|waiting)\s+(?:\d+\s*[dhms]\s*)+", re.IGNORECASE)


def _without_countdown(status: str) -> str:
    """``"Retry 2 of 5 in 42s"`` → ``"Retry 2 of 5"``; ``"Waiting 42s (rate limited)"`` → ``"Rate limited"``."""
    text = " ".join(_COUNTDOWN_RE.sub(" ", status).split()).strip(" ()·-–—")
    return text[:1].upper() + text[1:]


def update_button_text(installed_version: str, latest_version: str) -> str:
    current, latest = display_version(installed_version), display_version(latest_version)
    if current and latest:
        return f"Update {current} → {latest}"
    return f"Update to {latest}" if latest else "Update"


def job_detail_text(job: DownloadJob, *, now: Callable[[], float] = time.time) -> str:
    """The second line under a job's progress bar."""
    done, total = job.bytes_done, job.bytes_total
    amount = f"{format_bytes(done)} of {format_bytes(total)}" if total else (format_bytes(done) if done else "")
    match job.state:
        case JobState.DOWNLOADING:
            parts = [amount, format_speed(job.speed_bps) if job.speed_bps else ""]
            if job.eta_seconds:
                parts.append(f"{format_duration(job.eta_seconds)} left")
            return " · ".join(p for p in parts if p and p != "—") or "Starting…"
        case JobState.PAUSED:
            return f"Paused · {amount}" if amount else "Paused"
        case JobState.QUEUED:
            return "Waiting for other downloads to finish"
        case JobState.WAITING:
            if job.retry_at:
                seconds = max(0, round(job.retry_at - now()))
                reason = _without_countdown(job.status_text)
                return f"Retrying in {format_duration(seconds)}" + (f" · {reason}" if reason else "")
            return job.status_text or "Waiting to retry"
        case JobState.RESOLVING:
            return job.status_text or "Preparing the download…"
        case JobState.VERIFYING:
            return job.status_text or "Waiting for the browser check…"
        case JobState.EXTRACTING:
            return f"Extracting files · {job_percent(job)}%"
        case JobState.INSTALLING:
            return job.status_text or "Finishing installation…"
        case _:
            return job.status_text


def job_headline(job: DownloadJob) -> str:
    if job.state in (JobState.DOWNLOADING, JobState.PAUSED, JobState.EXTRACTING):
        verb = {JobState.DOWNLOADING: "Downloading", JobState.PAUSED: "Paused",
                JobState.EXTRACTING: "Installing"}[job.state]
        return f"{verb} {job_percent(job)}%"
    return job.state.label


def _progress_state(job: DownloadJob) -> str:
    if job.state in (JobState.PAUSED, JobState.WAITING, JobState.QUEUED):
        return "paused"
    if job.state in (JobState.EXTRACTING, JobState.INSTALLING):
        return "install"
    return ""


class GameActionCard(QFrame):
    """Primary call-to-action card on the Game page."""

    install_requested = pyqtSignal(object)  # DownloadOption
    update_requested = pyqtSignal(object)  # DownloadOption
    play_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    setup_requested = pyqtSignal()
    pause_requested = pyqtSignal(str)  # job id
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    retry_requested = pyqtSignal(str)
    downloads_requested = pyqtSignal()
    manage_requested = pyqtSignal(str)  # library | folder | executable | reinstall

    def __init__(self, parent: QWidget | None = None, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._clock = clock
        self._state = ActionState(ActionKind.LOADING)

        self.status = label("", "caption")
        self.failure = label("", "error", wrap=True)
        self.failure_retry = button("Retry download", "retry", size="sm",
                                    on_click=self._retry_failed)
        self.primary = button("Install", "download", variant="primary", size="lg", on_click=self._primary_clicked)
        self.primary.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.menu_button = button("", "chevron_down", variant="primary", size="lg", tooltip="More options",
                                  on_click=self.show_menu)
        self.menu_button.setFixedWidth(52)
        self._menu = QMenu(self)
        self.caption = label("", "caption", wrap=True)
        self.secondary = button("Play", "play", on_click=self.play_requested.emit)

        self.job_title = label("", "heading")
        # Which download this is when it isn't the game itself (patch / add-on). Own wrapping
        # line: labels like "Update Only From V 1.0.0 To V 1.1.0" don't fit beside the headline.
        self.job_option = label("", "caption", wrap=True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setProperty("size", "lg")
        self.progress.setTextVisible(False)
        self.job_detail = label("", "caption", wrap=True)
        self.pause_button = button("Pause", "pause", size="sm", on_click=lambda: self._job_action("pause"))
        self.resume_button = button("Resume", "resume", variant="primary", size="sm",
                                    on_click=lambda: self._job_action("resume"))
        self.cancel_button = button("Cancel", "cancel", variant="danger", size="sm",
                                    on_click=lambda: self._job_action("cancel"))
        self.downloads_button = button("View in Downloads", variant="link", on_click=self.downloads_requested.emit)
        for widget, name in ((self.pause_button, "pause"), (self.resume_button, "resume"),
                             (self.cancel_button, "cancel"), (self.failure_retry, "retry")):
            tag_icon(widget, name)

        self._job_box = QWidget()
        job_layout = QVBoxLayout(self._job_box)
        job_layout.setContentsMargins(0, 0, 0, 0)
        job_layout.setSpacing(8)
        job_layout.addWidget(self.job_title)
        job_layout.addWidget(self.job_option)
        job_layout.addWidget(self.progress)
        job_layout.addWidget(self.job_detail)
        job_layout.addLayout(hbox(self.pause_button, self.resume_button, self.cancel_button, None,
                                  self.downloads_button, spacing=8))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addWidget(self.status)
        layout.addWidget(self.failure)
        layout.addWidget(self.failure_retry, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(hbox(self.primary, self.menu_button, spacing=6))
        layout.addWidget(self.caption)
        layout.addWidget(self.secondary)
        layout.addWidget(self._job_box)

        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick)
        self.set_state(self._state)

    # --- public ------------------------------------------------------------------------
    @property
    def state(self) -> ActionState:
        return self._state

    def menu(self) -> QMenu:
        return self._menu

    def show_menu(self) -> None:
        """Pop the options/manage menu up under its button (non-blocking)."""
        if not self._menu.isEmpty():
            self._menu.popup(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height() + 4)))

    def set_state(self, state: ActionState) -> None:
        self._state = state
        self._render()

    # --- rendering ---------------------------------------------------------------------
    def _set_primary(self, text: str, icon_name: str, variant: str, *, enabled: bool = True,
                     tooltip: str = "") -> None:
        self.primary.setText(text)
        self.primary.setProperty("variant", variant)
        on_accent = variant in {"primary", "success"}
        pal = palette.current()
        self.primary.setIcon(icons.icon(icon_name, pal.accent_text if on_accent else None,
                                        disabled_color=pal.text_faint))
        self.primary.setIconSize(QSize(20, 20))
        self.primary.setEnabled(enabled)
        self.primary.setToolTip(tooltip)
        repolish(self.primary)
        self.primary.show()

    def _set_menu(self, entries: list[tuple[str, str, Callable[[], None] | None, str]], variant: str) -> None:
        """entries: (text, icon, callback or None for disabled, tooltip); "-" text = separator."""
        self._menu.clear()
        for text, icon_name, callback, tooltip in entries:
            if text == "-":
                self._menu.addSeparator()
                continue
            action = self._menu.addAction(icons.icon(icon_name) if icon_name else icons.icon("info"), text)
            if action is None:
                continue
            action.setToolTip(tooltip)
            if callback is None:
                action.setEnabled(False)
            else:
                action.triggered.connect(lambda _c=False, cb=callback: cb())
        self._menu.setToolTipsVisible(True)
        self.menu_button.setProperty("variant", variant)
        pal = palette.current()
        self.menu_button.setIcon(icons.icon("chevron_down", pal.accent_text if variant in {"primary", "success"}
                                            else None))
        repolish(self.menu_button)
        self.menu_button.setVisible(bool(entries))

    def _render(self) -> None:
        s = self._state
        self._countdown.stop()
        self._job_box.hide()
        self.secondary.hide()
        self.caption.setText("")
        failed = s.failed_job
        self.failure.setVisible(failed is not None and s.kind is not ActionKind.JOB)
        self.failure_retry.setVisible(self.failure.isVisibleTo(self))
        if failed is not None:
            self.failure.setText(f"Download failed: {failed.error or 'unknown error'}")
        render = {
            ActionKind.LOADING: self._render_loading,
            ActionKind.UNAVAILABLE: self._render_unavailable,
            ActionKind.INSTALL: self._render_install,
            ActionKind.JOB: self._render_job,
            ActionKind.UPDATE: self._render_update,
            ActionKind.SETUP: self._render_setup,
            ActionKind.PLAY: self._render_play,
            ActionKind.RUNNING: self._render_running,
        }[s.kind]
        render()
        self.caption.setVisible(bool(self.caption.text()))

    def _render_loading(self) -> None:
        self.status.setText("Checking…")
        self._set_primary("Loading…", "download", "primary", enabled=False)
        self._set_menu([], "primary")

    def _render_unavailable(self) -> None:
        self.status.setText("Not available")
        self._set_primary("Not available", "download", "primary", enabled=False)
        self._set_menu([], "primary")
        self.caption.setText(
            "No downloads are listed for this game right now." if self._state.details_available
            else "Game details could not be loaded.")

    def _render_install(self) -> None:
        s = self._state
        option = s.option
        self.status.setText("Not installed")
        self._set_primary("Install", "download", "primary", enabled=option is not None)
        if option is not None:
            self.caption.setText(" · ".join(p for p in (option_title(option), s.size_text) if p))
        entries: list[tuple[str, str, Callable[[], None] | None, str]] = []
        if len(s.options) > 1:
            for opt in s.options:
                text = option_title(opt) + (f" ({opt.size_text})" if opt.size_text else "")
                if opt.kind is DownloadKind.FULL:
                    entries.append((text, "download", lambda o=opt: self.install_requested.emit(o), ""))
            extras = [o for o in s.options if o.kind is not DownloadKind.FULL]
            if extras:
                entries.append(("-", "", None, ""))
                for opt in extras:
                    text = option_title(opt) + (f" ({opt.size_text})" if opt.size_text else "")
                    entries.append((text, "package", None, "Install the game first"))
        self._set_menu(entries, "primary")

    def _render_job(self) -> None:
        job = self._state.job
        assert job is not None
        self.status.setText("In your downloads")
        self.primary.hide()
        self.menu_button.hide()
        self._job_box.show()
        self.job_title.setText(job_headline(job))
        extra = job.option is not None and job.option.kind is not DownloadKind.FULL
        self.job_option.setText(option_title(job.option) if extra else "")
        self.job_option.setVisible(extra)
        indeterminate = job.state in (JobState.RESOLVING, JobState.VERIFYING) or \
            (job.state is JobState.DOWNLOADING and not job.bytes_total)
        if indeterminate:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(job.progress * 1000))
        self.progress.setProperty("state", _progress_state(job))
        repolish(self.progress)
        self.job_detail.setText(job_detail_text(job, now=self._clock))
        installing = job.state in (JobState.EXTRACTING, JobState.INSTALLING)
        self.pause_button.setVisible(job.state in (JobState.DOWNLOADING, JobState.QUEUED, JobState.WAITING,
                                                   JobState.RESOLVING, JobState.VERIFYING))
        self.resume_button.setVisible(job.state is JobState.PAUSED)
        self.cancel_button.setVisible(not installing)
        if job.state is JobState.WAITING and job.retry_at:
            self._countdown.start()

    def _render_update(self) -> None:
        s = self._state
        current = display_version(s.installed_version)
        self.status.setText("Update available")
        enabled = s.option is not None
        tooltip = "" if enabled else ("Checking the store for the update…" if not s.details_available
                                      else "No download is listed for this update.")
        self._set_primary(update_button_text(s.installed_version, s.latest_version), "update", "primary",
                          enabled=enabled, tooltip=tooltip)
        if s.option is not None:
            kind = "Patch" if s.is_patch else "Full download"
            self.caption.setText(f"{kind} · {s.size_text}" if s.size_text else kind)
        self._set_menu(self._manage_entries(), "primary")
        self.secondary.setText(f"Play {current}" if current else "Play")
        self.secondary.setIcon(icons.icon("play"))
        self.secondary.setVisible(bool(s.installed and s.installed.executable))

    def _render_setup(self) -> None:
        self.status.setText("Installed · choose how to start it")
        self._set_primary("Choose executable", "wrench", "primary")
        self._set_menu(self._manage_entries(), "primary")
        self.caption.setText("AnkerClient couldn't tell which program starts this game.")

    def _render_play(self) -> None:
        s = self._state
        game = s.installed
        version = display_version(s.installed_version)
        self.status.setText(f"Installed · {version}" if version else "Installed")
        self._set_primary("Play", "play", "success")
        self._set_menu(self._manage_entries(), "success")
        if game is not None:
            played = format_playtime(game.playtime_seconds)
            last = f"Last played {relative_day(game.last_played)}" if game.last_played else ""
            self.caption.setText(" · ".join(p for p in (played, last) if p))

    def _render_running(self) -> None:
        self.status.setText("Playing now")
        self._set_primary("Stop", "stop", "danger")
        self._set_menu([], "danger")
        self.caption.setText("Stopping closes the game without saving.")

    def _manage_entries(self) -> list[tuple[str, str, Callable[[], None] | None, str]]:
        entries: list[tuple[str, str, Callable[[], None] | None, str]] = [
            ("Show in library", "library", lambda: self.manage_requested.emit("library"), ""),
            ("Open install folder", "folder", lambda: self.manage_requested.emit("folder"), ""),
            ("Choose executable…", "wrench", lambda: self.manage_requested.emit("executable"), ""),
        ]
        if self._state.details_available:
            entries += [("-", "", None, ""),
                        ("Reinstall…", "retry", lambda: self.manage_requested.emit("reinstall"), "")]
        return entries

    # --- actions -----------------------------------------------------------------------
    def _primary_clicked(self) -> None:
        s = self._state
        match s.kind:
            case ActionKind.INSTALL if s.option is not None:
                self.install_requested.emit(s.option)
            case ActionKind.UPDATE if s.option is not None:
                self.update_requested.emit(s.option)
            case ActionKind.PLAY:
                self.play_requested.emit()
            case ActionKind.SETUP:
                self.setup_requested.emit()
            case ActionKind.RUNNING:
                self.stop_requested.emit()
            case _:
                pass

    def _job_action(self, action: str) -> None:
        job = self._state.job
        if job is None:
            return
        {"pause": self.pause_requested, "resume": self.resume_requested,
         "cancel": self.cancel_requested}[action].emit(job.id)

    def _retry_failed(self) -> None:
        if self._state.failed_job is not None:
            self.retry_requested.emit(self._state.failed_job.id)

    def _tick(self) -> None:
        job = self._state.job
        if job is None or job.state is not JobState.WAITING:
            self._countdown.stop()
            return
        self.job_detail.setText(job_detail_text(job, now=self._clock))

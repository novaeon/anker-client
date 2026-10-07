"""Store card plumbing shared by every Store view (Discover rows, Browse, Search, Wishlist).

* :func:`summary_item` turns a ``GameSummary`` into an undecorated ``CoverItem``.
* :class:`CardStates` keeps a slug → state index (installed / update / download
  job / running / wishlisted) in sync with the bridge signals and decorates
  items with badges, a progress bar and the favourite heart. Views listen to
  ``changed(frozenset[slug])`` (empty set = everything) and re-decorate. Per
  game it shows the most relevant unfinished job; when that one finishes the
  next (e.g. a queued add-on) is looked up with ``downloads.job_for``.
* :class:`StoreEnv` bundles what views need (ctx, nav, loader, states) plus the
  shared card actions (wishlist toggle, copy link, catalog upsert, NSFW filter).
* :func:`build_card_menu` builds the card context menu: View details, Show in
  library (when installed), Add/Remove wishlist, Open on website, Copy link.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from PyQt6.QtCore import QObject, QPoint, pyqtSignal
from PyQt6.QtWidgets import QMenu, QWidget

from anker_client.core.errors import OperationCancelled
from anker_client.core.events import GameExited, GameLaunched
from anker_client.core.models import DownloadJob, GameSummary, InstalledGame, JobState
from anker_client.core.tasks import CancelToken
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.navigator import Navigator
from anker_client.ui.widgets import game_common
from anker_client.ui.widgets.cover_grid import CoverBadge, CoverGridView, CoverItem
from anker_client.ui.widgets.game_common import no_token

log = logging.getLogger(__name__)

NSFW_GENRES = frozenset({"nsfw", "hentai", "adult", "18+"})

# Unfinished jobs ranked by how relevant they are for a "Downloading…" badge.
_JOB_RANK = {
    JobState.DOWNLOADING: 0,
    JobState.EXTRACTING: 0,
    JobState.INSTALLING: 0,
    JobState.RESOLVING: 1,
    JobState.VERIFYING: 1,
    JobState.WAITING: 2,
    JobState.QUEUED: 3,
    JobState.PAUSED: 4,
}


def is_nsfw(game: GameSummary) -> bool:
    return game.primary_genre.strip().casefold() in NSFW_GENRES


def summary_subtitle(game: GameSummary) -> str:
    parts = [game.primary_genre]
    if game.size_text:
        parts.append(game.size_text)
    elif game.year:
        parts.append(str(game.year))
    return " · ".join(p for p in parts if p)


def summary_item(game: GameSummary) -> CoverItem:
    return CoverItem(
        key=game.slug,
        title=game.title,
        subtitle=summary_subtitle(game),
        cover_url=game.cover_url,
        payload=game,
    )


def job_percent(job: DownloadJob) -> int:
    return round(job.progress * 100)


def job_badge(job: DownloadJob) -> tuple[str, str, float | None]:
    """(badge text, badge kind, progress or None) for an unfinished job."""
    pct = job_percent(job)
    has_bytes = bool(job.bytes_total and job.bytes_done)
    match job.state:
        case JobState.DOWNLOADING:
            return f"Downloading {pct}%", "accent", job.progress
        case JobState.PAUSED:
            return (f"Paused {pct}%" if has_bytes else "Paused"), "", (job.progress if has_bytes else None)
        case JobState.WAITING:
            return "Waiting", "warning", (job.progress if has_bytes else None)
        case JobState.RESOLVING | JobState.VERIFYING:
            return "Preparing", "accent", None
        case JobState.EXTRACTING:
            return f"Installing {pct}%", "accent", job.progress
        case JobState.INSTALLING:
            return "Installing", "accent", job.progress
        case JobState.QUEUED:
            return "Queued", "", None
        case _:
            return job.state.label, "", None


@dataclass(slots=True)
class _Snapshot:
    installed: list[InstalledGame] = field(default_factory=list)
    jobs: list[DownloadJob] = field(default_factory=list)
    wishlist: set[str] = field(default_factory=set)
    running: set[str] = field(default_factory=set)


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except OperationCancelled:
        raise
    except Exception:  # one unavailable service must not hide the others' badges
        log.debug("Card state source %r failed", fn, exc_info=True)
        return default


class CardStates(QObject):
    """Slug → card state index kept current from bridge signals."""

    changed = pyqtSignal(object)  # frozenset[str] of slugs; empty = everything

    def __init__(self, ctx: AppContext, bridge: QtEventBridge, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._installed: dict[str, InstalledGame] = {}
        self._jobs: dict[str, DownloadJob] = {}
        self._job_slugs: dict[str, str] = {}
        self._wishlist: set[str] = set()
        self._running_ids: set[str] = set()
        # Slugs changed by events while a snapshot is in flight; the (older)
        # snapshot must not overwrite them.
        self._touched_jobs: set[str] = set()
        self._touched_wishlist: set[str] = set()
        self._touched_running: set[str] = set()
        self._snapshot_handle: Any = None
        self._library_handle: Any = None
        self._loaded = False
        bridge.job_added.connect(self._on_job)
        bridge.job_updated.connect(self._on_job)
        bridge.job_removed.connect(self._on_job_removed)
        bridge.queue_changed.connect(self.refresh)
        bridge.library_changed.connect(self._on_library_changed)
        bridge.wishlist_changed.connect(self._on_wishlist_changed)
        bridge.game_launched.connect(self._on_launched)
        bridge.game_exited.connect(self._on_exited)

    # --- queries ---------------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded

    def installed(self, slug: str) -> InstalledGame | None:
        return self._installed.get(slug)

    def job(self, slug: str) -> DownloadJob | None:
        return self._jobs.get(slug)

    def is_wishlisted(self, slug: str) -> bool:
        return slug in self._wishlist

    def is_running(self, slug: str) -> bool:
        game = self._installed.get(slug)
        return game is not None and game.install_id in self._running_ids

    def decorate(self, item: CoverItem) -> CoverItem:
        slug = item.key
        badges: list[CoverBadge] = []
        progress: float | None = None
        job = self._jobs.get(slug)
        game = self._installed.get(slug)
        if job is not None:
            text, kind, progress = job_badge(job)
            badges.append(CoverBadge(text, kind))
        elif self.is_running(slug):
            badges.append(CoverBadge("Playing", "accent"))
        elif game is not None:
            if game.update_available:
                badges.append(CoverBadge("Update", "warning"))
            else:
                badges.append(CoverBadge("Installed", "success"))
        return replace(item, badges=tuple(badges), progress=progress, favorite=slug in self._wishlist)

    def decorate_all(self, items: Iterable[CoverItem]) -> list[CoverItem]:
        return [self.decorate(i) for i in items]

    def apply_to(self, view: CoverGridView, slugs: frozenset[str]) -> None:
        """Re-decorate ``slugs`` (or every item when empty) in ``view``'s model."""
        model = view.grid_model
        if slugs:
            for slug in slugs:
                item = model.item(slug)
                if item is not None:
                    model.replace_item(self.decorate(item))
        else:
            for item in model.items():
                model.replace_item(self.decorate(item))

    # --- loading -------------------------------------------------------------------------------
    def cancel(self) -> None:
        """Drop in-flight reads (page shutdown)."""
        for handle in (self._snapshot_handle, self._library_handle):
            if handle is not None:
                handle.cancel()
        self._snapshot_handle = None
        self._library_handle = None

    def refresh(self) -> None:
        """Re-read everything from the services (off the GUI thread)."""
        if self._snapshot_handle is not None:
            self._snapshot_handle.cancel()
        self._touched_jobs.clear()
        self._touched_wishlist.clear()
        self._touched_running.clear()
        ctx = self._ctx

        def snapshot(*, token: CancelToken) -> _Snapshot:
            snap = _Snapshot()
            snap.installed = _safe(lambda: ctx.library.games(include_hidden=True), [])
            token.raise_if_cancelled()
            snap.jobs = _safe(ctx.downloads.jobs, [])
            token.raise_if_cancelled()
            snap.wishlist = {g.slug for g in _safe(ctx.catalog.wishlist, [])}
            snap.running = set(_safe(ctx.launcher.running, set()))
            return snap

        def failed(exc: BaseException) -> None:
            self._snapshot_handle = None
            log.warning("Could not read card states: %s", exc)

        self._snapshot_handle = run_async(self, ctx.runner, snapshot, on_result=self._apply_snapshot,
                                          on_error=failed)

    def _apply_snapshot(self, snap: _Snapshot) -> None:
        self._snapshot_handle = None
        self._set_installed(snap.installed)
        jobs: dict[str, DownloadJob] = {}
        for job in snap.jobs:
            if job.state.is_finished or not job.slug:
                continue
            best = jobs.get(job.slug)
            if best is None or _JOB_RANK.get(job.state, 9) < _JOB_RANK.get(best.state, 9):
                jobs[job.slug] = job
        for slug in self._touched_jobs:
            if slug in self._jobs:
                jobs[slug] = self._jobs[slug]
            else:
                jobs.pop(slug, None)
        self._jobs = jobs
        self._job_slugs = {j.id: j.slug for j in jobs.values()}
        wishlist = set(snap.wishlist)
        for slug in self._touched_wishlist:
            if slug in self._wishlist:
                wishlist.add(slug)
            else:
                wishlist.discard(slug)
        self._wishlist = wishlist
        running = set(snap.running)
        for install_id in self._touched_running:
            if install_id in self._running_ids:
                running.add(install_id)
            else:
                running.discard(install_id)
        self._running_ids = running
        self._loaded = True
        self.changed.emit(frozenset())

    def _set_installed(self, games: list[InstalledGame]) -> set[str]:
        fresh = {g.slug: g for g in games if g.slug}
        changed = {s for s in fresh.keys() | self._installed.keys() if fresh.get(s) != self._installed.get(s)}
        self._installed = fresh
        return changed

    # --- live updates --------------------------------------------------------------------------
    def _in_flight(self) -> bool:
        return self._snapshot_handle is not None

    def _on_job(self, job: DownloadJob) -> None:
        if not job.slug:
            return
        if self._in_flight():
            self._touched_jobs.add(job.slug)
        current = self._jobs.get(job.slug)
        if job.state.is_finished:
            self._job_slugs.pop(job.id, None)
            if current is None or current.id != job.id:
                return
            del self._jobs[job.slug]
            self._reload_job(job.slug)  # another job for this game (e.g. an add-on) may be queued
        else:
            if current is not None and current.id != job.id and \
                    _JOB_RANK.get(current.state, 9) < _JOB_RANK.get(job.state, 9):
                return  # a more relevant job for the same game is already shown
            self._jobs[job.slug] = job
            self._job_slugs[job.id] = job.slug
        self.changed.emit(frozenset({job.slug}))

    def _on_job_removed(self, job_id: str) -> None:
        slug = self._job_slugs.pop(job_id, None)
        if slug is None:
            return
        if self._in_flight():
            self._touched_jobs.add(slug)
        current = self._jobs.get(slug)
        if current is not None and current.id == job_id:
            del self._jobs[slug]
            self.changed.emit(frozenset({slug}))
            self._reload_job(slug)

    def _reload_job(self, slug: str) -> None:
        def done(job: DownloadJob | None) -> None:
            if job is None or job.state.is_finished or slug in self._jobs:
                return  # nothing else queued, or an event already supplied a newer job
            self._jobs[slug] = job
            self._job_slugs[job.id] = slug
            self.changed.emit(frozenset({slug}))

        run_async(self, self._ctx.runner, no_token(self._ctx.downloads.job_for, slug), on_result=done,
                  on_error=lambda exc: log.debug("Job lookup for %s failed: %s", slug, exc))

    def _on_library_changed(self, _ids: object) -> None:
        if self._library_handle is not None:
            self._library_handle.cancel()
        library = self._ctx.library

        def load(*, token: CancelToken) -> list[InstalledGame]:
            token.raise_if_cancelled()
            return library.games(include_hidden=True)

        self._library_handle = run_async(self, self._ctx.runner, load, on_result=self._apply_library,
                                         on_error=lambda exc: log.debug("Library reload failed: %s", exc))

    def _apply_library(self, games: list[InstalledGame]) -> None:
        self._library_handle = None
        changed = self._set_installed(games)
        if changed:
            self.changed.emit(frozenset(changed))

    def _on_wishlist_changed(self, slug: str, wishlisted: bool) -> None:
        if self._in_flight():
            self._touched_wishlist.add(slug)
        if wishlisted:
            self._wishlist.add(slug)
        else:
            self._wishlist.discard(slug)
        self.changed.emit(frozenset({slug}))

    def _set_running(self, install_id: str, running: bool) -> None:
        if self._in_flight():
            self._touched_running.add(install_id)
        if running:
            self._running_ids.add(install_id)
        else:
            self._running_ids.discard(install_id)
        slugs = frozenset(s for s, g in self._installed.items() if g.install_id == install_id)
        if slugs:
            self.changed.emit(slugs)

    def _on_launched(self, event: GameLaunched) -> None:
        self._set_running(event.install_id, True)

    def _on_exited(self, event: GameExited) -> None:
        self._set_running(event.install_id, False)


@dataclass
class StoreEnv:
    """Everything a Store view needs, plus the card actions shared by all views."""

    ctx: AppContext
    nav: Navigator
    loader: ImageLoader
    states: CardStates
    owner: QObject

    def show_nsfw(self) -> bool:
        return bool(self.ctx.settings.get().show_nsfw)

    def visible(self, games: Iterable[GameSummary]) -> list[GameSummary]:
        """Drop NSFW games unless the user opted in."""
        games = list(games)
        return games if self.show_nsfw() else [g for g in games if not is_nsfw(g)]

    def items(self, games: Iterable[GameSummary]) -> list[CoverItem]:
        return self.states.decorate_all(summary_item(g) for g in games)

    def remember(self, games: list[GameSummary]) -> None:
        """Feed listing games to the local catalog index in the background."""
        if not games:
            return
        run_async(self.owner, self.ctx.runner, no_token(self.ctx.catalog.upsert, list(games)),
                  on_error=lambda exc: log.debug("Catalog upsert failed: %s", exc))

    def set_wishlisted(self, game: GameSummary, wishlisted: bool) -> None:
        def failed(exc: BaseException) -> None:
            self.nav.toast(f"Could not update your wishlist: {error_text(exc)}", "error")

        run_async(self.owner, self.ctx.runner, no_token(self.ctx.catalog.set_wishlisted, game, wishlisted),
                  on_error=failed)

    def copy_link(self, url: str) -> None:
        game_common.copy_text(url)
        self.nav.toast("Link copied to clipboard", "success")

    def open_on_website(self, url: str) -> None:
        if not game_common.open_url(url):
            self.nav.toast("Could not open your web browser.", "error")


def build_card_menu(parent: QWidget, game: GameSummary, env: StoreEnv) -> QMenu:
    """Context menu for a store card (not shown; call ``popup``)."""
    menu = QMenu(parent)
    menu.addAction(icons.icon("info"), "View details", lambda: env.nav.show_game(game.slug, game))
    installed = env.states.installed(game.slug)
    if installed is not None:
        install_id = installed.install_id
        menu.addAction(icons.icon("library"), "Show in library", lambda: env.nav.show_library(install_id))
    wishlisted = env.states.is_wishlisted(game.slug)
    menu.addAction(
        icons.icon("heart_filled" if wishlisted else "heart"),
        "Remove from wishlist" if wishlisted else "Add to wishlist",
        lambda: env.set_wishlisted(game, not wishlisted),
    )
    menu.addSeparator()
    menu.addAction(icons.icon("external"), "Open on website", lambda: env.open_on_website(game.page_url))
    menu.addAction(icons.icon("link"), "Copy link", lambda: env.copy_link(game.page_url))
    return menu


def show_card_menu(view: CoverGridView, slug: str, pos: QPoint, env: StoreEnv) -> QMenu | None:
    """Pop up the card menu for ``slug`` in ``view`` (non-blocking); returns the menu."""
    item = view.grid_model.item(slug)
    if item is None or not isinstance(item.payload, GameSummary):
        return None
    menu = build_card_menu(view, item.payload, env)
    # Deferred: the triggered action still runs before the menu is deleted.
    menu.aboutToHide.connect(menu.deleteLater)
    menu.popup(pos)
    return menu

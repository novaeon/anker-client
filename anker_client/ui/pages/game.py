"""Game details page.

``load(slug, summary)`` renders immediately from the listing summary (and the
cached details, ``catalog.cached_details``), then fetches fresh details with
``catalog.details`` — all off the GUI thread, guarded by a generation counter
so results for a previously shown game are dropped.

Layout (scrollable, centred, max ``MAX_CONTENT_WIDTH``):

* Hero (``widgets/game_hero``): artwork + scrim, Back, poster, title, meta line
  ("2019 · 12.4 GB · v1.2 · Updated 3 days ago"), genre chips (→ Store genre).
* Main column: screenshot carousel (click → ``LightboxDialog``), About with
  Show more/less, System requirements.
* Side column: action card (``widgets/game_actions`` — Install / options
  menu / download progress + Pause/Resume/Cancel / Play + Manage / Update
  "v1 → v2" (patch when it applies) / Stop), wishlist toggle, Website, Copy
  link, Details facts (Size, Version, Released, Updated, Torrent, playtime),
  Add-ons (ADDON options, installable once the base game is installed).
* Narrow windows (content < ``NARROW_WIDTH``): the side column moves directly
  below the hero and lays its cards out side by side; the hero gets compact.

Live updates come from the bridge: download jobs, library changes, launches/
exits, wishlist and sign-in changes. With several jobs for one game (game +
add-on) the most relevant is shown (running → queued/waiting → paused, like
``DownloadManager.job_for``); when it finishes the next one is looked up.
Overlapping library lookups and state reads are cancelled/re-run so a late,
older answer never overwrites a newer one. Errors: with something already
shown a banner offers Retry; with nothing to show a full-page error offers
Retry/Back. Destructive actions (cancel download, stop game) are confirmed by
name. Bridge slots are bound methods so they die with the page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QBoxLayout, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from anker_client.core.errors import ExecutableNotSetError, NotFoundError, OperationCancelled
from anker_client.core.events import GameExited, GameLaunched
from anker_client.core.formatting import format_playtime
from anker_client.core.models import (
    DownloadJob,
    DownloadKind,
    DownloadOption,
    GameDetails,
    GameSummary,
    InstalledGame,
    JobState,
)
from anker_client.core.tasks import CancelToken, TaskHandle
from anker_client.services.container import AppContext
from anker_client.ui import icons
from anker_client.ui.async_ import error_text, run_async
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.dialogs.lightbox import LightboxDialog
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.navigator import Navigator
from anker_client.ui.theme import palette
from anker_client.ui.widgets import game_common
from anker_client.ui.widgets.carousel import ScreenshotCarousel
from anker_client.ui.widgets.common import LoadingOverlay, button, hbox, label, repolish
from anker_client.ui.widgets.game_actions import ActionKind, ActionState, GameActionCard, derive_action_state
from anker_client.ui.widgets.game_common import (
    ThemeWatcher,
    display_version,
    format_date,
    genre_slug,
    no_token,
    relative_day,
    retint_icons,
    tag_icon,
)
from anker_client.ui.widgets.game_hero import GameHero
from anker_client.ui.widgets.game_sections import (
    AddonsCard,
    ExpandableText,
    Fact,
    FactsCard,
    GameErrorView,
    InlineBanner,
    RequirementsCard,
)

log = logging.getLogger(__name__)

_FORCE_REFRESH = timedelta(0)


@dataclass(slots=True)
class _LocalState:
    installed: InstalledGame | None = None
    job: DownloadJob | None = None
    running: bool = False
    wishlisted: bool = False
    logged_in: bool = False
    errors: list[str] = field(default_factory=list)


def _attempt(state: _LocalState, name: str, fn: Any, default: Any) -> Any:
    try:
        return fn()
    except OperationCancelled:
        raise
    except Exception as exc:  # a failing service must not blank the whole page
        log.debug("Game page: %s failed: %s", name, exc, exc_info=True)
        state.errors.append(name)
        return default


def _job_rank(job: DownloadJob) -> int:
    """Lower is more relevant: running, then queued/waiting, then paused."""
    if job.state.is_active:
        return 0
    return 1 if job.state in (JobState.QUEUED, JobState.WAITING) else 2


def _section(title: str, body: QWidget) -> QWidget:
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addWidget(label(title, "title"))
    layout.addWidget(body)
    return holder


class GamePage(QWidget):
    NARROW_WIDTH = 900
    SIDE_WIDTH = 340
    MAX_CONTENT_WIDTH = 1320
    MARGIN = 24

    def __init__(self, ctx: AppContext, bridge: QtEventBridge, nav: Navigator, loader: ImageLoader,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "page")
        self._ctx = ctx
        self._nav = nav
        self._loader = loader
        self._slug = ""
        self._summary: GameSummary | None = None
        self._details: GameDetails | None = None
        self._details_loading = False
        self._details_error: BaseException | None = None
        self._installed: InstalledGame | None = None
        self._job: DownloadJob | None = None
        self._failed_job: DownloadJob | None = None
        self._running = False
        self._launching = False
        self._wishlisted = False
        self._logged_in = False
        self._gen = 0
        self._handles: list[TaskHandle[Any]] = []
        self._state_handle: TaskHandle[Any] | None = None
        self._installed_handle: TaskHandle[Any] | None = None
        self._touched: set[str] = set()  # fields changed by events while a state snapshot is in flight
        self._narrow: bool | None = None
        self.last_lightbox: LightboxDialog | None = None

        self._build_ui()
        self._connect(bridge)
        self._theme_watcher = ThemeWatcher(self, self._on_theme_changed)
        self._stack.setCurrentWidget(self.error_view)
        self.error_view.show_error("No game selected", "Pick a game in the store to see its details.", retry=False)

    # --- construction ---------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.hero = GameHero(self._loader)
        self.banner = InlineBanner()
        self.carousel = ScreenshotCarousel(self._loader)
        self.about = ExpandableText()
        self.about_placeholder = label("", "muted")
        self.requirements = RequirementsCard()
        self.action_card = GameActionCard()
        self.wishlist_button = button("Add to wishlist", "heart", on_click=self._toggle_wishlist)
        self.wishlist_button.setCheckable(True)
        self.wishlist_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.website_button = button("Website", "external", variant="ghost", tooltip="Open on ankergames.net",
                                     on_click=self._open_website)
        self.copy_button = button("Copy link", "link", variant="ghost", tooltip="Copy the store link",
                                  on_click=self._copy_link)
        tag_icon(self.website_button, "external")
        tag_icon(self.copy_button, "link")
        self.facts = FactsCard("Details")
        self.addons = AddonsCard()

        about_body = QWidget()
        about_layout = QVBoxLayout(about_body)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_layout.addWidget(self.about)
        about_layout.addWidget(self.about_placeholder)
        self.screens_section = _section("Screenshots", self.carousel)
        self.about_section = _section("About this game", about_body)

        self.main_column = QWidget()
        main_layout = QVBoxLayout(self.main_column)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(28)
        main_layout.addWidget(self.screens_section)
        main_layout.addWidget(self.about_section)
        main_layout.addWidget(self.requirements)
        main_layout.addStretch(1)

        side_primary = QWidget()
        primary_layout = QVBoxLayout(side_primary)
        primary_layout.setContentsMargins(0, 0, 0, 0)
        primary_layout.setSpacing(10)
        primary_layout.addWidget(self.action_card)
        primary_layout.addWidget(self.wishlist_button)
        primary_layout.addLayout(hbox(self.website_button, self.copy_button, spacing=8))
        side_secondary = QWidget()
        secondary_layout = QVBoxLayout(side_secondary)
        secondary_layout.setContentsMargins(0, 0, 0, 0)
        secondary_layout.setSpacing(16)
        secondary_layout.addWidget(self.facts)
        secondary_layout.addWidget(self.addons)
        secondary_layout.addStretch(1)
        self.side_column = QWidget()
        self._side_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self.side_column)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(16)
        self._side_layout.addWidget(side_primary)
        self._side_layout.addWidget(side_secondary)

        columns = QWidget()
        self._columns_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, columns)
        self._columns_layout.setContentsMargins(0, 0, 0, 0)
        self._columns_layout.setSpacing(28)
        self._columns_layout.addWidget(self.main_column, 1)
        self._columns_layout.addWidget(self.side_column, 0)

        self._content = QWidget()
        self._content.setProperty("role", "transparent")
        self._content.setMaximumWidth(self.MAX_CONTENT_WIDTH)
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, 32)
        content_layout.setSpacing(20)
        content_layout.addWidget(self.hero)
        content_layout.addWidget(self.banner)
        content_layout.addWidget(columns)
        content_layout.addStretch(1)

        holder = QWidget()
        holder.setProperty("role", "transparent")
        holder.setLayout(hbox(None, self._content, None, spacing=0))
        holder.layout().setStretch(1, 100)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().setSingleStep(40)
        self.scroll.setWidget(holder)

        self.loading = LoadingOverlay("Loading game…")
        self.error_view = GameErrorView()
        self._stack = QStackedWidget(self)
        for page in (self.scroll, self.loading, self.error_view):
            self._stack.addWidget(page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self._apply_layout(narrow=False)

    def _connect(self, bridge: QtEventBridge) -> None:
        self.hero.back_clicked.connect(self._nav.back)
        self.hero.genre_clicked.connect(lambda name: self._nav.show_store(genre=genre_slug(name)))
        self.banner.action_clicked.connect(self.refresh)
        self.error_view.retry_clicked.connect(self.refresh)
        self.error_view.back_clicked.connect(self._nav.back)
        self.carousel.image_activated.connect(self._open_lightbox)
        card = self.action_card
        card.install_requested.connect(self._request_install)
        card.update_requested.connect(self._request_install)
        card.play_requested.connect(self._launch)
        card.stop_requested.connect(self._stop)
        card.setup_requested.connect(self._choose_executable)
        card.pause_requested.connect(lambda job_id: self._job_command("pause", job_id))
        card.resume_requested.connect(lambda job_id: self._job_command("resume", job_id))
        card.cancel_requested.connect(self._cancel_job)
        card.retry_requested.connect(lambda job_id: self._job_command("retry", job_id))
        card.downloads_requested.connect(self._nav.show_downloads)
        card.manage_requested.connect(self._manage)
        self.addons.install_requested.connect(self._request_install)

        bridge.job_added.connect(self._on_job)
        bridge.job_updated.connect(self._on_job)
        bridge.job_removed.connect(self._on_job_removed)
        # Bound methods only: PyQt disconnects them when this page is destroyed, whereas a
        # lambda on the (longer-lived) bridge would call into a deleted widget.
        bridge.library_changed.connect(self._on_library_changed)
        bridge.updates_found.connect(self._on_updates_found)
        bridge.game_launched.connect(self._on_launched)
        bridge.game_exited.connect(self._on_exited)
        bridge.wishlist_changed.connect(self._on_wishlist_changed)
        bridge.auth_changed.connect(self._on_auth_changed)

    # --- contract ------------------------------------------------------------------------
    def load(self, slug: str, summary: GameSummary | None = None) -> None:
        """Show ``slug`` immediately from ``summary``/cache, then fetch fresh details."""
        slug = (slug or "").strip()
        if not slug:
            return
        if slug == self._slug and (self._details is not None or self._details_loading):
            if summary is not None and self._summary is None:
                self._summary = summary
            self._refresh_local_state()
            if self._details is not None and not self._details_loading:
                self._fetch_details(quiet=True)
            return
        self._cancel_all()
        self._gen += 1
        self._slug = slug
        self._summary = summary
        self._details = None
        self._details_error = None
        self._details_loading = True
        self._installed = None
        self._job = None
        self._failed_job = None
        self._running = False
        self._launching = False
        self._wishlisted = False
        self.banner.hide()
        self.scroll.verticalScrollBar().setValue(0)
        if summary is not None:
            self._render_all()
            self._stack.setCurrentWidget(self.scroll)
        else:
            self._stack.setCurrentWidget(self.loading)
        self._load_cached()
        self._refresh_local_state()
        self._fetch_details()

    @property
    def slug(self) -> str:
        return self._slug

    def on_activated(self) -> None:
        if self._slug:
            self._refresh_local_state()

    def on_deactivated(self) -> None:
        self._close_lightbox()

    def shutdown(self) -> None:
        self._cancel_all()
        self._close_lightbox()

    # --- extras ----------------------------------------------------------------------------
    @property
    def details(self) -> GameDetails | None:
        return self._details

    @property
    def action_state(self) -> ActionState:
        return self.action_card.state

    def refresh(self) -> None:
        """Re-fetch details (bypassing the cache) and local state (F5 / Retry)."""
        if not self._slug:
            return
        self.banner.hide()
        if self._details is None and self._summary is None and self._installed is None:
            self._stack.setCurrentWidget(self.loading)
        self._details_loading = True
        self._render_actions()
        self._refresh_local_state()
        self._fetch_details(force=True)

    # --- loading -----------------------------------------------------------------------------
    def _track(self, handle: TaskHandle[Any]) -> TaskHandle[Any]:
        self._handles = [h for h in self._handles if not h.done()]
        self._handles.append(handle)
        return handle

    def _cancel_all(self) -> None:
        for handle in self._handles:
            handle.cancel()
        self._handles = []
        self._installed_handle = None
        if self._state_handle is not None:
            self._state_handle.cancel()
            self._state_handle = None

    def _load_cached(self) -> None:
        gen, slug = self._gen, self._slug

        def done(details: GameDetails | None) -> None:
            if gen != self._gen or details is None or self._details is not None:
                return
            self._details = details
            self._render_all()
            self._stack.setCurrentWidget(self.scroll)

        self._track(run_async(self, self._ctx.runner, no_token(self._ctx.catalog.cached_details, slug),
                              on_result=done, on_error=lambda exc: log.debug("Cached details failed: %s", exc)))

    def _fetch_details(self, *, force: bool = False, quiet: bool = False) -> None:
        gen, slug = self._gen, self._slug
        self._details_loading = True
        kwargs: dict[str, Any] = {"max_age": _FORCE_REFRESH} if force else {}

        def done(details: GameDetails) -> None:
            if gen != self._gen:
                return
            self._details = details
            self._details_loading = False
            self._details_error = None
            self.banner.hide()
            self._render_all()
            self._stack.setCurrentWidget(self.scroll)

        def failed(exc: BaseException) -> None:
            if gen != self._gen:
                return
            self._details_loading = False
            self._details_error = exc
            log.info("Details for %s failed: %s", slug, exc)
            self._show_details_error(exc, quiet=quiet)

        self._track(run_async(self, self._ctx.runner, self._ctx.catalog.details, slug, **kwargs,
                              on_result=done, on_error=failed))

    def _show_details_error(self, exc: BaseException, *, quiet: bool) -> None:
        has_content = self._details is not None or self._summary is not None or self._installed is not None
        if not has_content:
            if isinstance(exc, NotFoundError):
                self.error_view.show_error("Game not found", "This game is no longer available on AnkerGames.",
                                           retry=False)
            else:
                self.error_view.show_error("Couldn't load this game", error_text(exc))
            self._stack.setCurrentWidget(self.error_view)
            return
        if not quiet or self._details is None:
            message = ("This game is no longer listed on AnkerGames." if isinstance(exc, NotFoundError)
                       else f"Couldn't refresh game details. {error_text(exc)}")
            self.banner.show_message(message, kind="warning" if self._details is not None else "error",
                                     action_text="" if isinstance(exc, NotFoundError) else "Retry")
        self._render_all()
        self._stack.setCurrentWidget(self.scroll)

    def _refresh_local_state(self) -> None:
        if self._state_handle is not None:
            self._state_handle.cancel()
        self._touched.clear()
        gen, slug, ctx = self._gen, self._slug, self._ctx

        def snapshot(*, token: CancelToken) -> _LocalState:
            state = _LocalState()
            state.installed = _attempt(state, "library", lambda: ctx.library.find_by_slug(slug), None)
            token.raise_if_cancelled()
            state.job = _attempt(state, "downloads", lambda: ctx.downloads.job_for(slug), None)
            if state.installed is not None:
                install_id = state.installed.install_id
                state.running = bool(_attempt(state, "launcher", lambda: ctx.launcher.is_running(install_id), False))
            state.wishlisted = bool(_attempt(state, "wishlist", lambda: ctx.catalog.is_wishlisted(slug), False))
            state.logged_in = bool(_attempt(state, "auth", lambda: ctx.auth.is_logged_in, False))
            return state

        def done(state: _LocalState) -> None:
            self._state_handle = None
            if gen != self._gen:
                return
            if "installed" not in self._touched:
                self._installed = state.installed
            if "job" not in self._touched:
                self._job = state.job
            if "running" not in self._touched:
                self._running = state.running
            if "wishlist" not in self._touched:
                self._wishlisted = state.wishlisted
            self._logged_in = state.logged_in
            if self._stack.currentWidget() is self.loading and self._installed is not None:
                self._stack.setCurrentWidget(self.scroll)
            self._render_all()
            stranded = self._stack.currentWidget() is self.error_view and self._installed is not None
            if stranded and self._details_error is not None:
                # installed games stay playable even when the site can't be reached
                self._show_details_error(self._details_error, quiet=False)

        def failed(exc: BaseException) -> None:
            self._state_handle = None
            log.warning("Could not read local state for %s: %s", slug, exc)

        self._state_handle = run_async(self, ctx.runner, snapshot, on_result=done, on_error=failed)

    def _reload_installed(self) -> None:
        if not self._slug:
            return
        # Lookups can finish out of order on the pool; only the newest may apply.
        if self._installed_handle is not None:
            self._installed_handle.cancel()
        gen, slug = self._gen, self._slug

        def done(game: InstalledGame | None) -> None:
            self._installed_handle = None
            if gen != self._gen:
                return
            self._installed = game
            self._touched.add("installed")
            if game is None:
                self._running = False
            self._render_actions()
            self._render_facts()

        self._installed_handle = self._track(run_async(
            self, self._ctx.runner, no_token(self._ctx.library.find_by_slug, slug),
            on_result=done, on_error=lambda exc: log.debug("Library lookup failed: %s", exc)))

    def _on_updates_found(self, _updates: object) -> None:
        self._reload_installed()

    # --- rendering ------------------------------------------------------------------------------
    def _title(self) -> str:
        for source in (self._details, self._summary, self._installed):
            if source is not None and source.title:
                return source.title
        return self._slug

    def _page_url(self) -> str:
        return GameSummary(slug=self._slug, title=self._title()).page_url

    def _render_all(self) -> None:
        self._render_hero()
        self._render_main()
        self._render_actions()
        self._render_facts()
        self._render_wishlist()

    def _render_hero(self) -> None:
        d, s, g = self._details, self._summary, self._installed
        year = ""
        if d is not None and d.release_date[:4].isdigit():
            year = d.release_date[:4]
        elif s is not None and s.year:
            year = str(s.year)
        size = (d.size_text if d is not None else "") or (s.size_text if s is not None else "")
        version = display_version(d.version) if d is not None else ""
        updated = f"Updated {relative_day(d.updated_date)}" if d is not None and d.updated_date else ""
        meta = " · ".join(p for p in (year, size, version, updated) if p)
        if d is not None and d.genres:
            genres = list(d.genres)
        elif s is not None and s.primary_genre:
            genres = [s.primary_genre]
        else:
            genres = list(g.genres) if g is not None else []
        background = ""
        if d is not None:
            background = d.hero_url or (d.screenshots[0] if d.screenshots else "")
        poster = (d.cover_url if d is not None else "") or (s.cover_url if s is not None else "") or \
            (g.cover_url if g is not None else "")
        self.hero.set_game(title=self._title(), meta=meta, genres=genres, background_url=background,
                           poster_url=poster)

    def _render_main(self) -> None:
        d = self._details
        shots = list(d.screenshots) if d is not None else []
        self.carousel.set_images(shots)
        self.screens_section.setVisible(bool(shots))
        description = d.description.strip() if d is not None else ""
        self.about.set_text(description)
        self.about.setVisible(bool(description))
        if description:
            self.about_placeholder.hide()
        else:
            if self._details_loading:
                placeholder = "Loading the description…"
            elif d is None:
                placeholder = "The description couldn't be loaded."
            else:
                placeholder = "No description is available for this game."
            self.about_placeholder.setText(placeholder)
            self.about_placeholder.show()
        self.requirements.set_requirements(d.requirements if d is not None else None)

    def _render_actions(self) -> None:
        state = derive_action_state(
            self._details, self._installed, self._job,
            running=self._running, details_loading=self._details_loading, failed_job=self._failed_job,
        )
        self.action_card.set_state(state)
        if self._launching and state.kind is ActionKind.PLAY:
            self.action_card.primary.setEnabled(False)
            self.action_card.primary.setText("Launching…")
        addons = [o for o in (self._details.download_options if self._details else [])
                  if o.kind is DownloadKind.ADDON]
        busy = self._job is not None and not self._job.state.is_finished
        self.addons.set_addons(addons, self._installed, busy=busy)

    def _render_facts(self) -> None:
        d, s, g = self._details, self._summary, self._installed
        facts: list[Fact] = []
        size = (d.size_text if d is not None else "") or (s.size_text if s is not None else "")
        if size:
            facts.append(Fact("Size", size))
        if d is not None and d.version:
            facts.append(Fact("Version", display_version(d.version)))
        if d is not None and d.release_date:
            facts.append(Fact("Released", format_date(d.release_date)))
        elif s is not None and s.year:
            facts.append(Fact("Released", str(s.year)))
        if d is not None and d.updated_date:
            updated = relative_day(d.updated_date)
            facts.append(Fact("Updated", updated[:1].upper() + updated[1:], tooltip=format_date(d.updated_date)))
        if d is not None and d.torrent_available:
            if self._logged_in:
                facts.append(Fact("Torrent", "Available"))
            else:
                facts.append(Fact("Torrent", "Requires sign-in", action_text="Sign in", action=self._nav.request_login))
        if g is not None:
            facts.append(Fact("Installed", display_version(g.version) or "Version unknown", tooltip=g.path))
            if g.playtime_seconds:
                facts.append(Fact("Playtime", format_playtime(g.playtime_seconds)))
        self.facts.set_facts(facts)

    def _render_wishlist(self) -> None:
        on = self._wishlisted
        self.wishlist_button.setChecked(on)
        self.wishlist_button.setText("On your wishlist" if on else "Add to wishlist")
        pal = palette.current()
        self.wishlist_button.setIcon(icons.icon("heart_filled" if on else "heart", pal.danger if on else None))
        self.wishlist_button.setToolTip("Remove from wishlist" if on else "Save this game for later")
        repolish(self.wishlist_button)

    def _on_theme_changed(self) -> None:
        """Icons and pixmaps bake palette colours in; rebuild them for the new theme."""
        retint_icons(self)
        self.error_view.refresh_icon()
        self.banner.refresh_icon()
        if self._slug:
            self._render_all()  # also rebuilds the requirement icons and the action card

    # --- responsive layout ------------------------------------------------------------------------
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        width = min(self.width(), self.MAX_CONTENT_WIDTH) - 2 * self.MARGIN
        self._apply_layout(narrow=width < self.NARROW_WIDTH)

    def _apply_layout(self, *, narrow: bool) -> None:
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            # side column above the main one, its two halves side by side
            self._columns_layout.setDirection(QBoxLayout.Direction.BottomToTop)
            self._side_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._side_layout.setStretch(0, 1)
            self._side_layout.setStretch(1, 1)
            self.side_column.setMinimumWidth(0)
            self.side_column.setMaximumWidth(16777215)
        else:
            # stacked: the action half keeps its natural height, the rest absorbs the slack
            self._columns_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._side_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self._side_layout.setStretch(0, 0)
            self._side_layout.setStretch(1, 1)
            self.side_column.setFixedWidth(self.SIDE_WIDTH)
        self.hero.set_compact(narrow)

    @property
    def is_narrow(self) -> bool:
        return bool(self._narrow)

    # --- actions ----------------------------------------------------------------------------------
    def _request_install(self, option: DownloadOption | None) -> None:
        if self._details is None:
            self._nav.toast("Game details are still loading. Try again in a moment.", "warning")
            return
        self._nav.request_install(self._details, option)

    def _launch(self) -> None:
        game = self._installed
        if game is None or self._launching:
            return
        install_id, gen = game.install_id, self._gen
        self._launching = True
        self._render_actions()

        def failed(exc: BaseException) -> None:
            if isinstance(exc, ExecutableNotSetError):
                self._nav.choose_executable(install_id)
            else:
                self._nav.toast(f"Couldn't start {game.title}: {error_text(exc)}", "error")

        def finished() -> None:
            if gen == self._gen:
                self._launching = False
                self._render_actions()

        run_async(self, self._ctx.runner, no_token(self._ctx.launcher.launch, install_id),
                  on_error=failed, on_finished=finished)

    def _stop(self) -> None:
        game = self._installed
        if game is None:
            return
        if not game_common.confirm(self, "Stop game", f"Stop {game.title}? Unsaved progress will be lost.",
                                   "Stop game", "Keep playing"):
            return
        run_async(self, self._ctx.runner, no_token(self._ctx.launcher.stop, game.install_id),
                  on_error=lambda exc: self._nav.toast(f"Couldn't stop {game.title}: {error_text(exc)}", "error"))

    def _choose_executable(self) -> None:
        if self._installed is not None:
            self._nav.choose_executable(self._installed.install_id)

    def _job_command(self, command: str, job_id: str) -> None:
        downloads = self._ctx.downloads
        fn = {"pause": downloads.pause, "resume": downloads.resume, "retry": downloads.retry}[command]
        if command == "retry":
            self._failed_job = None
            self._render_actions()
        run_async(self, self._ctx.runner, no_token(fn, job_id),
                  on_error=lambda exc: self._nav.toast(error_text(exc), "error"))

    def _cancel_job(self, job_id: str) -> None:
        title = self._title()
        if not game_common.confirm(self, "Cancel download",
                                   f"Cancel the download of {title}? Downloaded data will be deleted.",
                                   "Cancel download", "Keep downloading"):
            return
        run_async(self, self._ctx.runner, no_token(self._ctx.downloads.cancel, job_id),
                  on_error=lambda exc: self._nav.toast(error_text(exc), "error"))

    def _manage(self, action: str) -> None:
        game = self._installed
        if action == "reinstall":
            if self._details is not None:
                self._nav.request_install(self._details, self._details.primary_option)
            return
        if game is None:
            return
        if action == "library":
            self._nav.show_library(game.install_id)
        elif action == "executable":
            self._nav.choose_executable(game.install_id)
        elif action == "folder":
            run_async(self, self._ctx.runner, no_token(self._ctx.launcher.open_folder, game.install_id),
                      on_error=lambda exc: self._nav.toast(f"Couldn't open the folder: {error_text(exc)}", "error"))

    def _summary_for_wishlist(self) -> GameSummary:
        if self._details is not None:
            return self._details.to_summary()
        if self._summary is not None:
            return self._summary
        return GameSummary(slug=self._slug, title=self._title())

    def _toggle_wishlist(self) -> None:
        if not self._slug:
            return
        target = not self._wishlisted
        previous = self._wishlisted
        self._wishlisted = target
        self._touched.add("wishlist")
        self._render_wishlist()
        gen = self._gen

        def failed(exc: BaseException) -> None:
            if gen == self._gen:
                self._wishlisted = previous
                self._render_wishlist()
            self._nav.toast(f"Could not update your wishlist: {error_text(exc)}", "error")

        run_async(self, self._ctx.runner,
                  no_token(self._ctx.catalog.set_wishlisted, self._summary_for_wishlist(), target),
                  on_error=failed)

    def _open_website(self) -> None:
        if self._slug and not game_common.open_url(self._page_url()):
            self._nav.toast("Could not open your web browser.", "error")

    def _copy_link(self) -> None:
        if self._slug:
            game_common.copy_text(self._page_url())
            self._nav.toast("Link copied to clipboard", "success")

    def _open_lightbox(self, index: int) -> None:
        urls = self.carousel.urls()
        if not urls:
            return
        self._close_lightbox()
        dialog = LightboxDialog(self._loader, urls, index, self, title=self._title())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.index_changed.connect(self.carousel.set_current)
        dialog.finished.connect(lambda _result, d=dialog: self._lightbox_finished(d))
        self.last_lightbox = dialog
        dialog.open()

    def _lightbox_finished(self, dialog: LightboxDialog) -> None:
        if self.last_lightbox is dialog:
            self.last_lightbox = None  # deleted on close; never touch it again
        QTimer.singleShot(0, self.carousel.setFocus)

    def _close_lightbox(self) -> None:
        dialog = self.last_lightbox
        self.last_lightbox = None
        if dialog is not None and not sip.isdeleted(dialog) and dialog.isVisible():
            dialog.reject()

    # --- live updates -------------------------------------------------------------------------------
    def _on_job(self, job: DownloadJob) -> None:
        if not self._slug or job.slug != self._slug:
            return
        self._touched.add("job")
        if job.state.is_finished:
            shown = self._job is not None and self._job.id == job.id
            if shown:
                self._job = None
            if job.state is JobState.FAILED:
                self._failed_job = job
            elif job.state is JobState.COMPLETED:
                self._failed_job = None
                self._reload_installed()
            if shown:
                self._reload_job()  # another job for this game (e.g. an add-on) may be queued
        else:
            current = self._job
            # Several jobs can exist for one game (game + add-on): keep showing the most
            # relevant one (same order as DownloadManager.job_for) instead of flipping.
            if current is None or current.id == job.id or _job_rank(job) < _job_rank(current):
                self._job = job
            self._failed_job = None
        self._render_actions()

    def _on_job_removed(self, job_id: str) -> None:
        changed = False
        if self._job is not None and self._job.id == job_id:
            self._job = None
            self._touched.add("job")
            self._reload_job()
            changed = True
        if self._failed_job is not None and self._failed_job.id == job_id:
            self._failed_job = None
            changed = True
        if changed:
            self._render_actions()

    def _reload_job(self) -> None:
        gen, slug = self._gen, self._slug

        def done(job: DownloadJob | None) -> None:
            # An event that arrived meanwhile is newer than this lookup.
            if gen == self._gen and self._job is None and job is not None and not job.state.is_finished:
                self._job = job
                self._render_actions()

        self._track(run_async(self, self._ctx.runner, no_token(self._ctx.downloads.job_for, slug),
                              on_result=done, on_error=lambda exc: log.debug("Job lookup failed: %s", exc)))

    def _on_library_changed(self, ids: object) -> None:
        if not self._slug:
            return
        ids = ids if isinstance(ids, frozenset | set) else frozenset()
        mine = {self._slug} | ({self._installed.install_id} if self._installed is not None else set())
        if not ids or mine & set(ids):
            self._reload_installed()

    def _on_launched(self, event: GameLaunched) -> None:
        if self._installed is not None and event.install_id == self._installed.install_id:
            self._running = True
            self._launching = False
            self._touched.add("running")
            self._render_actions()
        else:
            self._recheck_running()

    def _on_exited(self, event: GameExited) -> None:
        if self._installed is not None and event.install_id == self._installed.install_id:
            self._running = False
            self._touched.add("running")
            self._render_actions()
        else:
            self._recheck_running()

    def _recheck_running(self) -> None:
        # The install record isn't known yet, so the event can't be matched to this game, and
        # the in-flight snapshot may have read ``is_running`` before it: read everything again.
        if self._installed is None and self._state_handle is not None:
            self._refresh_local_state()

    def _on_wishlist_changed(self, slug: str, wishlisted: bool) -> None:
        if slug == self._slug:
            self._wishlisted = wishlisted
            self._touched.add("wishlist")
            self._render_wishlist()

    def _on_auth_changed(self, user: object) -> None:
        self._logged_in = user is not None
        if self._slug:
            self._render_facts()

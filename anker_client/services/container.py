"""Composition root: builds every service once and hands them to the UI.

This is the only module that knows how services are wired together. The UI
receives an :class:`AppContext` and never constructs services itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anker_client import __version__
from anker_client.constants import BASE_URL
from anker_client.core.db import Database
from anker_client.core.events import EventBus, SettingsChanged
from anker_client.core.paths import AppPaths
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import TaskRunner
from anker_client.services.auth import AuthService
from anker_client.services.catalog import CatalogService
from anker_client.services.downloads.engine import HttpDownloader
from anker_client.services.downloads.manager import DownloadManager
from anker_client.services.downloads.ratelimit import RateLimiter
from anker_client.services.downloads.resolver import LinkResolver
from anker_client.services.images import ImageCache
from anker_client.services.install.extractor import Extractor
from anker_client.services.install.installer import Installer
from anker_client.services.install.shortcuts import ShortcutService
from anker_client.services.launcher import GameLauncher
from anker_client.services.library import LibraryService
from anker_client.services.updates import AppUpdateChecker, UpdateService
from anker_client.site.client import AnkerGamesClient
from anker_client.site.http import HttpClient
from anker_client.site.livewire import LivewireClient

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    paths: AppPaths
    settings: SettingsStore
    events: EventBus
    db: Database
    runner: TaskRunner
    http: HttpClient
    client: AnkerGamesClient
    livewire: LivewireClient
    auth: AuthService
    catalog: CatalogService
    images: ImageCache
    shortcuts: ShortcutService
    extractor: Extractor
    installer: Installer
    library: LibraryService
    launcher: GameLauncher
    rate_limiter: RateLimiter
    resolver: LinkResolver
    downloads: DownloadManager
    updates: UpdateService
    app_updates: AppUpdateChecker

    def start(self) -> None:
        """Start long-lived services (call once the UI is ready to receive events)."""
        self.downloads.start()
        self.launcher.start()

    def shutdown(self) -> None:
        """Stop everything in dependency order. Safe to call twice."""
        for name, action in (
            ("downloads", lambda: self.downloads.shutdown(timeout=10)),
            ("launcher", self.launcher.shutdown),
            ("auth", self.auth.save_session),
            ("runner", lambda: self.runner.shutdown(wait=False)),
            ("http", self.http.close),
            ("db", self.db.close),
        ):
            try:
                action()
            except Exception:
                log.exception("Error while shutting down %s", name)


def build_context(paths: AppPaths | None = None) -> AppContext:
    paths = (paths or AppPaths.default()).ensure()
    events = EventBus()
    settings = SettingsStore(paths.settings_file, events, legacy_path=paths.legacy_settings_file)
    db = Database(paths.database_file)
    runner = TaskRunner(max_workers=8)

    http = HttpClient()
    client = AnkerGamesClient(http, BASE_URL)
    livewire = LivewireClient(http, BASE_URL)
    auth = AuthService(client, settings, events, paths)
    catalog = CatalogService(db, client, events)
    images = ImageCache(http, paths)

    shortcuts = ShortcutService()
    extractor = Extractor(lambda: settings.get().seven_zip_path)
    installer = Installer(settings, extractor, shortcuts)
    library = LibraryService(db, settings, events, shortcuts, catalog)
    launcher = GameLauncher(library, events, settings)

    rate_limiter = RateLimiter(settings.get().speed_limit_bps)
    resolver = LinkResolver(
        client,
        None,  # the UI installs the browser-based verifier once QtWebEngine is up
        verification_timeout=lambda: float(settings.get().verification_timeout_seconds),
    )
    downloads = DownloadManager(
        db=db,
        settings=settings,
        events=events,
        paths=paths,
        resolver=resolver,
        downloader_factory=lambda connections: HttpDownloader(http, rate_limiter, connections=connections),
        rate_limiter=rate_limiter,
        installer=installer,
        library=library,
    )
    updates = UpdateService(db, library, catalog, events)
    app_updates = AppUpdateChecker(http, events, __version__)

    def on_settings_changed(event: SettingsChanged) -> None:
        if "speed_limit_kbps" in event.keys:
            rate_limiter.set_rate(settings.get().speed_limit_bps)

    events.subscribe(SettingsChanged, on_settings_changed)

    return AppContext(
        paths=paths,
        settings=settings,
        events=events,
        db=db,
        runner=runner,
        http=http,
        client=client,
        livewire=livewire,
        auth=auth,
        catalog=catalog,
        images=images,
        shortcuts=shortcuts,
        extractor=extractor,
        installer=installer,
        library=library,
        launcher=launcher,
        rate_limiter=rate_limiter,
        resolver=resolver,
        downloads=downloads,
        updates=updates,
        app_updates=app_updates,
    )

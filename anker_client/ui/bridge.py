"""Marshals Qt-free :class:`EventBus` events onto the GUI thread as Qt signals."""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from anker_client.core import events as ev


class QtEventBridge(QObject):
    """Subscribe once to the bus; widgets connect to these signals instead.

    Every signal is delivered on the GUI thread regardless of which worker
    thread published the event.
    """

    # raw stream (any Event)
    event = pyqtSignal(object)

    # downloads
    job_added = pyqtSignal(object)  # DownloadJob
    job_updated = pyqtSignal(object)  # DownloadJob
    job_removed = pyqtSignal(str)  # job id
    queue_changed = pyqtSignal()

    # library
    library_changed = pyqtSignal(object)  # frozenset[str] of install ids (empty = all)
    game_installed = pyqtSignal(object)  # events.GameInstalled
    game_uninstalled = pyqtSignal(object)  # events.GameUninstalled
    game_launched = pyqtSignal(object)  # events.GameLaunched
    game_exited = pyqtSignal(object)  # events.GameExited

    # catalog / updates
    catalog_sync_progress = pyqtSignal(int, object)  # pages done, total|None
    catalog_updated = pyqtSignal(object)  # events.CatalogUpdated
    updates_found = pyqtSignal(object)  # tuple[GameUpdate, ...]
    app_update_available = pyqtSignal(object)  # AppRelease

    # account / settings / misc
    auth_changed = pyqtSignal(object)  # UserInfo | None
    settings_changed = pyqtSignal(object)  # frozenset[str]
    wishlist_changed = pyqtSignal(str, bool)
    notification = pyqtSignal(object)  # events.Notification

    _relay = pyqtSignal(object)

    def __init__(self, bus: ev.EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._relay.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe = bus.subscribe(ev.Event, self._relay.emit)

    def close(self) -> None:
        self._unsubscribe()

    def _dispatch(self, e: ev.Event) -> None:  # runs on the GUI thread
        self.event.emit(e)
        match e:
            case ev.JobAdded(job=job):
                self.job_added.emit(job)
            case ev.JobUpdated(job=job):
                self.job_updated.emit(job)
            case ev.JobRemoved(job_id=job_id):
                self.job_removed.emit(job_id)
            case ev.QueueChanged():
                self.queue_changed.emit()
            case ev.LibraryChanged(install_ids=ids):
                self.library_changed.emit(ids)
            case ev.GameInstalled():
                self.game_installed.emit(e)
            case ev.GameUninstalled():
                self.game_uninstalled.emit(e)
            case ev.GameLaunched():
                self.game_launched.emit(e)
            case ev.GameExited():
                self.game_exited.emit(e)
            case ev.CatalogSyncProgress(pages_done=done, pages_total=total):
                self.catalog_sync_progress.emit(done, total)
            case ev.CatalogUpdated():
                self.catalog_updated.emit(e)
            case ev.UpdatesFound(updates=updates):
                self.updates_found.emit(updates)
            case ev.AppUpdateAvailable(release=release):
                self.app_update_available.emit(release)
            case ev.AuthChanged(user=user):
                self.auth_changed.emit(user)
            case ev.SettingsChanged(keys=keys):
                self.settings_changed.emit(keys)
            case ev.WishlistChanged(slug=slug, wishlisted=wishlisted):
                self.wishlist_changed.emit(slug, wishlisted)
            case ev.Notification():
                self.notification.emit(e)
            case _:
                pass

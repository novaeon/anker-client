"""Thread-safe, Qt-free publish/subscribe bus.

Services publish events from any thread. Subscribers are called synchronously
on the publishing thread, so they must be quick and must not touch Qt widgets —
the UI subscribes through :class:`anker_client.ui.bridge.QtEventBridge`, which
re-emits every event as a Qt signal delivered on the GUI thread.

Events are immutable snapshots: payload models are copies, never live objects
owned by a service.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anker_client.core.models import (
    AppRelease,
    DownloadJob,
    GameUpdate,
    UserInfo,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all events."""


# --- downloads ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobAdded(Event):
    job: DownloadJob


@dataclass(frozen=True, slots=True)
class JobUpdated(Event):
    """State or progress changed. Progress-only updates are throttled (~4/s per job)."""

    job: DownloadJob


@dataclass(frozen=True, slots=True)
class JobRemoved(Event):
    job_id: str


@dataclass(frozen=True, slots=True)
class QueueChanged(Event):
    """Order/membership changed (reorder, clear finished…). Re-read ``DownloadManager.jobs()``."""


# --- library ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryChanged(Event):
    """The set of installed games or their metadata changed; re-read ``LibraryService.games()``."""

    install_ids: frozenset[str] = frozenset()  # empty = everything may have changed


@dataclass(frozen=True, slots=True)
class GameInstalled(Event):
    install_id: str
    title: str
    needs_executable: bool = False  # executable could not be determined automatically
    is_update: bool = False


@dataclass(frozen=True, slots=True)
class GameUninstalled(Event):
    install_id: str
    title: str


@dataclass(frozen=True, slots=True)
class GameLaunched(Event):
    install_id: str
    title: str


@dataclass(frozen=True, slots=True)
class GameExited(Event):
    install_id: str
    title: str
    session_seconds: int


# --- catalog / updates ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogSyncProgress(Event):
    pages_done: int
    pages_total: int | None


@dataclass(frozen=True, slots=True)
class CatalogUpdated(Event):
    total_games: int
    new_games: int = 0


@dataclass(frozen=True, slots=True)
class UpdatesFound(Event):
    updates: tuple[GameUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class AppUpdateAvailable(Event):
    release: AppRelease


# --- account / settings / misc ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthChanged(Event):
    user: UserInfo | None


@dataclass(frozen=True, slots=True)
class SettingsChanged(Event):
    keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class WishlistChanged(Event):
    slug: str
    wishlisted: bool


@dataclass(frozen=True, slots=True)
class Notification(Event):
    """A user-facing message (toast + optional tray balloon)."""

    title: str
    message: str
    level: str = "info"  # info | success | warning | error
    tray: bool = False  # also show a system tray notification when the window is hidden


# --- bus ----------------------------------------------------------------------------

E = TypeVar("E", bound=Event)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[type[Event], list[Callable[[Any], None]]] = {}

    def subscribe(self, event_type: type[E], callback: Callable[[E], None]) -> Callable[[], None]:
        """Register ``callback`` for ``event_type`` (and its subclasses).

        Returns a function that removes the subscription.
        """
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._subscribers.get(event_type, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            targets: list[Callable[[Any], None]] = []
            for event_type, callbacks in self._subscribers.items():
                if isinstance(event, event_type):
                    targets.extend(callbacks)
        for callback in targets:
            try:
                callback(event)
            except Exception:  # a broken subscriber must never break the publisher
                log.exception("Event subscriber %r failed for %r", callback, type(event).__name__)

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()

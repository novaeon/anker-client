"""Interfaces between the main window and its pages.

``MainWindow`` implements :class:`Navigator`; every page receives
``(ctx: AppContext, bridge: QtEventBridge, nav: Navigator, parent)``.
Pages never import each other — cross-page actions go through ``nav``.
"""

from __future__ import annotations

from typing import Protocol

from anker_client.core.models import DownloadOption, GameDetails, GameSummary


class Navigator(Protocol):
    # --- navigation ------------------------------------------------------------------
    def show_store(self, *, query: str = "", genre: str = "") -> None: ...
    def show_game(self, slug: str, summary: GameSummary | None = None) -> None: ...
    def show_library(self, install_id: str = "") -> None: ...
    def show_downloads(self) -> None: ...
    def show_settings(self, section: str = "") -> None: ...
    def back(self) -> None: ...

    # --- shared flows (implemented once in MainWindow) ----------------------------------
    def request_install(self, details: GameDetails, option: DownloadOption | None = None) -> None:
        """Start the install flow: library-folder choice (if several), disk-space check,
        overlay target check for PATCH/ADDON, then ``ctx.downloads.enqueue``."""
        ...

    def request_login(self) -> None: ...

    def choose_executable(self, install_id: str) -> None:
        """Show the executable picker for an installed game (e.g. after an ambiguous install)."""
        ...

    def toast(self, message: str, level: str = "info") -> None:
        """Non-blocking in-window notification. level: info | success | warning | error."""
        ...


class Page(Protocol):
    """Optional lifecycle hooks the main window calls on its pages."""

    def on_activated(self) -> None: ...
    def on_deactivated(self) -> None: ...
    def shutdown(self) -> None: ...

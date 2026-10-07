"""Shared helpers for the store_ui GUI tests (no tests in here).

Each ``test_store_ui_*`` module declares its own ``ui`` fixture that delegates to
:func:`ui_session`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from PyQt6.QtWidgets import QApplication

from anker_client.core import events as ev
from anker_client.core.models import DownloadJob, DownloadKind, DownloadOption, GameSummary, JobState
from anker_client.ui.bridge import QtEventBridge
from anker_client.ui.image_loader import ImageLoader
from anker_client.ui.theme.manager import ThemeManager


@dataclass
class Call:
    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


class RecordingNav:
    """A Navigator that records every call."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(name, args, kwargs))

    def named(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def show_store(self, *, query: str = "", genre: str = "") -> None:
        self._record("show_store", query=query, genre=genre)

    def show_game(self, slug: str, summary: GameSummary | None = None) -> None:
        self._record("show_game", slug, summary)

    def show_library(self, install_id: str = "") -> None:
        self._record("show_library", install_id)

    def show_downloads(self) -> None:
        self._record("show_downloads")

    def show_settings(self, section: str = "") -> None:
        self._record("show_settings", section)

    def back(self) -> None:
        self._record("back")

    def request_install(self, details: Any, option: DownloadOption | None = None) -> None:
        self._record("request_install", details, option)

    def request_login(self) -> None:
        self._record("request_login")

    def choose_executable(self, install_id: str) -> None:
        self._record("choose_executable", install_id)

    def toast(self, message: str, level: str = "info") -> None:
        self._record("toast", message, level)


@dataclass
class UI:
    ctx: Any
    bridge: QtEventBridge
    loader: ImageLoader
    nav: RecordingNav
    theme: ThemeManager
    opened_urls: list[str] = field(default_factory=list)


def ui_session(fake_ctx: Any, monkeypatch: pytest.MonkeyPatch, *, images: bool = False) -> Iterator[UI]:
    """Body of the per-module ``ui`` fixtures: fast fakes, midnight theme, bridge, loader, nav.

    ``game_common.open_url`` is replaced so no test ever opens a real browser.
    Unless ``images`` is True, artwork "fails" instantly instead of being painted
    and PNG-encoded by the fake image cache (keeps the worker pool free and tests fast).
    """
    import tests.fakes
    from anker_client.ui.widgets import game_common

    monkeypatch.setattr(tests.fakes, "LATENCY", 0.01)
    if not images:
        def no_image(url: str, **_kwargs: Any) -> Any:
            raise FileNotFoundError(url)

        monkeypatch.setattr(fake_ctx.images, "fetch", no_image)
    opened: list[str] = []
    monkeypatch.setattr(game_common, "open_url", lambda url: opened.append(url) or True)
    theme = ThemeManager(QApplication.instance())
    theme.apply("midnight")
    bridge = QtEventBridge(fake_ctx.events)
    loader = ImageLoader(fake_ctx.images, fake_ctx.runner)
    session = UI(fake_ctx, bridge, loader, RecordingNav(), theme, opened)
    yield session
    bridge.close()


def game(ctx: Any, slug: str) -> GameSummary:
    return next(g.copy() for g in ctx.client.games if g.slug == slug)


def make_job(slug: str, title: str, state: JobState, *, done: int = 0, total: int = 1000,
             job_id: str = "job-1", kind: DownloadKind = DownloadKind.FULL) -> DownloadJob:
    return DownloadJob(id=job_id, slug=slug, title=title, option=DownloadOption(1, "Direct", kind),
                       library_root="C:/Games", state=state, bytes_done=done, bytes_total=total,
                       speed_bps=1024.0 if state is JobState.DOWNLOADING else 0.0)


def publish(ctx: Any, event: ev.Event) -> None:
    ctx.events.publish(event)

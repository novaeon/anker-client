"""Shared pytest configuration.

* Qt runs offscreen with real Windows fonts (screenshots are legible).
* ``ANKERCLIENT_HOME`` is pointed at a per-test temp dir so nothing touches the
  real ``%APPDATA%``.
* ``@pytest.mark.live`` tests (real network) run only with ``ANKER_LIVE_TESTS=1``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

FIXTURES = Path(__file__).parent / "fixtures"
SITE_FIXTURES = FIXTURES / "site"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ANKER_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live network test (set ANKER_LIVE_TESTS=1)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ankerclient-home"
    monkeypatch.setenv("ANKERCLIENT_HOME", str(home))
    return home


@pytest.fixture
def site_fixture() -> callable:
    """``site_fixture("game_hollow_knight.html")`` → file text."""

    def read(name: str) -> str:
        return (SITE_FIXTURES / name).read_text(encoding="utf-8")

    return read


@pytest.fixture
def fake_ctx(tmp_path: Path):
    """A fully working in-memory ``AppContext`` look-alike for UI tests (see tests/fakes.py)."""
    from tests.fakes import FakeContext

    ctx = FakeContext(tmp_path / "fake-home")
    yield ctx
    ctx.shutdown()

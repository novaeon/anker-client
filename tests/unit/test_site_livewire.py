"""LivewireClient against a local server serving a page with a search-component snapshot."""

from __future__ import annotations

import html as htmllib
import json
from collections.abc import Iterator

import pytest

from anker_client.core.errors import NetworkError, RateLimitedError, SiteChangedError
from anker_client.site.http import HttpClient
from anker_client.site.livewire import LivewireClient
from tests.conftest import SITE_FIXTURES
from tests.unit.test_site_server import LocalSite, Reply, Seen, serve_local_site

SNAPSHOT = json.dumps(
    {
        "data": {"q": "", "posts": [[], {"s": "arr"}], "errorMessage": "", "minSearchLength": 2},
        "memo": {"id": "abc123", "name": "search-component", "path": "games", "method": "GET"},
        "checksum": "deadbeef",
    }
)
SEARCH_RESPONSE = (SITE_FIXTURES / "livewire_search_response.json").read_text(encoding="utf-8")


@pytest.fixture
def local_site() -> Iterator[LocalSite]:
    yield from serve_local_site()


def _page(site: LocalSite, uri_path: str = "/livewire-abc/update") -> str:
    config = json.dumps({"csrf": "stale-from-cache", "uri": site.url(uri_path), "progressBar": ""})
    return f"""<html><head><script>window.livewireScriptConfig = {config};</script></head><body>
      <div wire:snapshot="{htmllib.escape(SNAPSHOT, quote=True)}" wire:id="abc123"></div>
      <div wire:snapshot="{htmllib.escape(json.dumps({"memo": {"name": "notify-component"}}), quote=True)}"></div>
    </body></html>"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(local_site: LocalSite) -> Iterator[tuple[LocalSite, LivewireClient, FakeClock]]:
    http = HttpClient(backoff_base=0.01)
    clock = FakeClock()
    client = LivewireClient(http, local_site.base_url, clock=clock)
    local_site.route("GET", "/games", Reply(200, _page(local_site)))
    local_site.route("GET", "/csrf-token", Reply.json({"token": "fresh-token"}))
    local_site.route("POST", "/livewire-abc/update", Reply(200, SEARCH_RESPONSE, content_type="application/json"))
    try:
        yield local_site, client, clock
    finally:
        http.close()


def test_quick_search_posts_a_livewire_update(env) -> None:
    site, client, _clock = env
    games = client.quick_search("  hollow ")
    assert [g.slug for g in games][:2] == ["hollow-knight", "hollow-knight-silksong"]
    assert len(games) == 5
    post = site.seen("POST", "/livewire-abc/update")[0]
    assert post.json == {
        "_token": "fresh-token",
        "components": [{"snapshot": SNAPSHOT, "updates": {"q": "hollow"}, "calls": []}],
    }
    assert post.headers["x-livewire"] == "1"
    assert post.headers["x-csrf-token"] == "fresh-token"
    assert post.headers["content-type"] == "application/json"
    assert post.headers["referer"] == site.url("/games")


def test_call_returns_parsed_snapshot_and_effects(env) -> None:
    site, client, _clock = env
    result = client.call(site.url("/games"), "search-component", updates={"q": "hollow"}, calls=[{"method": "x"}])
    assert result["snapshot"]["data"]["q"] == "hollow"
    assert result["snapshot"]["memo"]["name"] == "search-component"
    assert "html" in result["effects"]
    assert site.seen("POST", "/livewire-abc/update")[0].json["components"][0]["calls"] == [{"method": "x"}]


def test_page_and_token_are_cached_for_ten_minutes(env) -> None:
    site, client, clock = env
    client.quick_search("hollow")
    client.quick_search("knight")
    assert len(site.seen("GET", "/games")) == 1
    assert len(site.seen("GET", "/csrf-token")) == 1
    clock.now += 601
    client.quick_search("again")
    assert len(site.seen("GET", "/games")) == 2


def test_short_queries_do_not_touch_the_network(env) -> None:
    site, client, _clock = env
    assert client.quick_search(" a ") == []
    assert client.quick_search("") == []
    assert site.seen() == []


def test_stale_token_or_deploy_is_retried_once_with_fresh_state(env) -> None:
    site, client, _clock = env
    replies = [Reply(419, "Page Expired"), Reply(200, SEARCH_RESPONSE, content_type="application/json")]
    site.route("POST", "/livewire-abc/update", replies)
    assert len(client.quick_search("hollow")) == 5
    assert len(site.seen("POST", "/livewire-abc/update")) == 2
    assert len(site.seen("GET", "/games")) == 2
    assert len(site.seen("GET", "/csrf-token")) == 2


def test_second_failure_raises_site_changed(env) -> None:
    site, client, _clock = env
    site.route("POST", "/livewire-abc/update", Reply(404, "Not Found"))
    with pytest.raises(SiteChangedError):
        client.quick_search("hollow")
    assert len(site.seen("POST", "/livewire-abc/update")) == 2


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(429, "slow", {"Retry-After": "7"}), RateLimitedError),
        (Reply(503, "down"), NetworkError),
        (Reply(200, "<html>not json</html>"), SiteChangedError),
        (Reply.json({"components": []}), SiteChangedError),
        (Reply.json({"components": [{"snapshot": "{}", "effects": {}}]}), SiteChangedError),  # no html
    ],
)
def test_protocol_mismatches(env, reply: Reply, error: type[Exception]) -> None:
    site, client, _clock = env
    site.route("POST", "/livewire-abc/update", reply)
    with pytest.raises(error):
        client.quick_search("hollow")


def test_missing_component_or_config(env) -> None:
    site, client, _clock = env
    with pytest.raises(SiteChangedError):
        client.call(site.url("/games"), "no-such-component")
    site.route("GET", "/plain", Reply(200, "<html><body>no livewire</body></html>"))
    with pytest.raises(SiteChangedError):
        client.call(site.url("/plain"), "search-component")
    # A page that no longer exists is a protocol change too (callers fall back on SiteChangedError).
    with pytest.raises(SiteChangedError):
        client.call(site.url("/moved"), "search-component")


def test_snapshot_from_real_page_is_found(local_site: LocalSite) -> None:
    home = (SITE_FIXTURES / "home.html").read_text(encoding="utf-8")
    home = home.replace("https:\\/\\/ankergames.net\\/livewire-be923db6\\/update", local_site.url("/lw/update"))
    local_site.route("GET", "/games", Reply(200, home))
    local_site.route("GET", "/csrf-token", Reply.json({"token": "t"}))

    def update(seen: Seen) -> Reply:
        snapshot = json.loads(seen.json["components"][0]["snapshot"])
        assert snapshot["memo"]["name"] == "search-component"
        return Reply(200, SEARCH_RESPONSE, content_type="application/json")

    local_site.route("POST", "/lw/update", update)
    http = HttpClient()
    try:
        games = LivewireClient(http, local_site.base_url).quick_search("hollow")
    finally:
        http.close()
    assert games[0].title == "Hollow Knight"

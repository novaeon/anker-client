"""Opt-in checks against the real ankergames.net (``ANKER_LIVE_TESTS=1``).

Polite by design: a handful of paced GETs, at most ONE ``mint_download_ticket``
call, and never any game file transfer (the file URL is only touched when the
ticket demands browser verification, in which case the site answers with an
HTML page, not the file).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from anker_client.core.errors import VerificationError
from anker_client.core.models import DownloadKind, SortOrder
from anker_client.site import parsers
from anker_client.site.client import AnkerGamesClient
from anker_client.site.http import HttpClient
from anker_client.site.livewire import LivewireClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def http() -> Iterator[HttpClient]:
    client = HttpClient()
    yield client
    client.close()


@pytest.fixture(scope="module")
def client(http: HttpClient) -> AnkerGamesClient:
    return AnkerGamesClient(http)


def test_browse_first_page(client: AnkerGamesClient) -> None:
    listing = client.browse()
    assert len(listing.games) >= 40
    assert listing.has_next
    assert all(g.slug and g.title and g.cover_url for g in listing.games)
    assert sum(1 for g in listing.games if g.size_bytes) >= len(listing.games) // 2


def test_browse_sorted_genre_and_past_the_end(client: AnkerGamesClient) -> None:
    by_title = client.browse(sort=SortOrder.TITLE, genre="action")
    assert by_title.games
    assert client.browse(page=9999).games == []


def test_home_and_genres(client: AnkerGamesClient) -> None:
    titles = [s.title for s in client.home_sections()]
    assert "Trending Games" in titles and "Latest Games" in titles
    slugs = {g.slug for g in client.genres()}
    assert {"action", "adventure", "vr"} <= slugs


def test_search_and_top_games(client: AnkerGamesClient) -> None:
    result = client.search("call of duty")
    assert any("call-of-duty" in g.slug for g in result.games)
    # A no-results page must not turn its sidebar ("Top this week") into results.
    nothing = client.search("zzqxjvwq no such game")
    assert nothing.games == [] and not nothing.has_next
    assert len(client.top_games()) >= 10


def test_game_details(client: AnkerGamesClient) -> None:
    details = client.game_details("hollow-knight")
    assert details.title == "Hollow Knight"
    assert details.version
    assert details.screenshots and details.genres
    assert any(o.kind is DownloadKind.FULL for o in details.download_options)


def test_guest_session(client: AnkerGamesClient, http: HttpClient) -> None:
    assert client.current_user() is None
    fields = parsers.parse_login_form(http.get_text("https://ankergames.net/login"))
    assert fields.get("_token")


def test_quick_search(http: HttpClient) -> None:
    games = LivewireClient(http).quick_search("hollow knight")
    assert any(g.slug == "hollow-knight" for g in games)


def test_one_download_ticket(client: AnkerGamesClient) -> None:
    details = client.game_details("hollow-knight")
    option = details.primary_option
    assert option is not None
    ticket_url = client.mint_download_ticket(option.download_id, referer_slug=details.slug)
    assert "/download/" in ticket_url
    ticket = client.fetch_ticket_page(ticket_url)
    assert "/download-file/" in ticket.file_url
    assert ticket.wait_seconds >= 0
    if ticket.requires_verification:
        assert ticket.turnstile_sitekey
        with pytest.raises(VerificationError):
            client.resolve_file_url(ticket.file_url, ticket_url=ticket_url)

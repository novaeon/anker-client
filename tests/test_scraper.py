# tests/test_scraper.py
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock
import requests
from anker_client.core.scraper import (
    parse_search_results,
    parse_game_page,
    parse_livewire_results,
    livewire_search,
    get_download_url,
    _page_cache,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_search_results_count():
    html = (FIXTURES / "search_results.html").read_text()
    results = parse_search_results(html)
    assert len(results) == 2


def test_parse_search_result_fields():
    html = (FIXTURES / "search_results.html").read_text()
    results = parse_search_results(html)
    iron_lung = next(r for r in results if "iron-lung" in r["slug"])
    assert iron_lung["title"] == "Iron Lung"
    assert iron_lung["slug"] == "iron-lung"
    assert "ankergames.net" in iron_lung["cover_url"]


def test_parse_search_results_handles_absolute_game_links():
    html = """
    <article>
      <img src="/uploads/poster/game.jpg">
      <a href="https://ankergames.net/game/gravity-circuit"
         title="Gravity Circuit">Gravity Circuit</a>
    </article>
    """
    results = parse_search_results(html)
    assert results == [{
        "title": "Gravity Circuit",
        "slug": "gravity-circuit",
        "cover_url": "https://ankergames.net/uploads/poster/game.jpg",
    }]


def test_parse_game_page_csrf():
    html = (FIXTURES / "game_page.html").read_text()
    data = parse_game_page(html)
    assert data["csrf_token"] == "test-csrf-token-abc123"


def test_parse_game_page_download_id():
    html = (FIXTURES / "game_page.html").read_text()
    data = parse_game_page(html)
    assert data["download_id"] == 2282


def test_parse_game_page_title():
    html = (FIXTURES / "game_page.html").read_text()
    data = parse_game_page(html)
    assert data["title"] == "Iron Lung"


def test_parse_game_page_description():
    html = (FIXTURES / "game_page.html").read_text()
    data = parse_game_page(html)
    assert "submarine" in data["description"].lower()


def test_parse_game_page_file_size():
    """parse_game_page extracts file size from a bare <span>."""
    html = """<html><head>
    <meta name="csrf-token" content="tok">
    <script type="application/ld+json">
    {"@type":"VideoGame","name":"Iron Lung","description":"A horror game.","genre":["Horror"]}
    </script></head><body>
    <button @click.prevent="generateDownloadUrl(42)">Download</button>
    <span>161.7 MB</span>
    <span>2022</span>
    </body></html>"""
    data = parse_game_page(html)
    assert data["file_size"] == "161.7 MB"


def test_parse_game_page_file_size_gb():
    """parse_game_page parses GB sizes."""
    html = """<html><head><meta name="csrf-token" content="tok">
    <script type="application/ld+json">{"name":"Hades","description":"x","genre":[]}</script>
    </head><body><span>10.49 GB</span></body></html>"""
    data = parse_game_page(html)
    assert data["file_size"] == "10.49 GB"


def test_parse_game_page_file_size_missing():
    """parse_game_page returns None when no size span is present."""
    html = """<html><head><meta name="csrf-token" content="tok">
    <script type="application/ld+json">{"name":"Game","description":"x","genre":[]}</script>
    </head><body></body></html>"""
    data = parse_game_page(html)
    assert data["file_size"] is None


def test_parse_game_page_screenshots_from_jsonld():
    """parse_game_page extracts screenshot URLs from JSON-LD screenshot field."""
    html = """<html><head>
    <meta name="csrf-token" content="tok">
    <script type="application/ld+json">
    {"@type":"VideoGame","name":"Hades","description":"A roguelike.","genre":["Action"],
     "screenshot":["https://cdn.example.com/hades1.jpg","https://cdn.example.com/hades2.jpg"]}
    </script></head><body>
    <button @click.prevent="generateDownloadUrl(99)">Download</button>
    </body></html>"""
    data = parse_game_page(html)
    assert data["screenshots"] == [
        "https://cdn.example.com/hades1.jpg",
        "https://cdn.example.com/hades2.jpg",
    ]


def test_parse_game_page_screenshots_html_fallback():
    """HTML fallback finds screenshots via alt text even when class is 'object-cover'."""
    html = """<html><head><meta name="csrf-token" content="tok">
    <script type="application/ld+json">
    {"@type":"VideoGame","name":"Iron Lung","description":"A horror game.","genre":[]}
    </script></head><body>
    <img src="https://ankergames.net/uploads/screenshots/iron-lung-screenshot-1.jpg"
         alt="Iron Lung screenshot 1"
         class="w-full h-full object-cover transition-transform">
    <img src="https://ankergames.net/uploads/screenshots/iron-lung-screenshot-2.jpg"
         alt="Iron Lung screenshot 2"
         class="w-full h-full object-cover">
    <img src="https://ankergames.net/static/img/logo.svg" alt="Logo" class="w-8 h-8">
    </body></html>"""
    data = parse_game_page(html)
    assert len(data["screenshots"]) == 2
    assert "screenshot-1" in data["screenshots"][0]
    assert "screenshot-2" in data["screenshots"][1]


def test_parse_game_page_screenshots_imageobject():
    """parse_game_page handles screenshot as list of ImageObject dicts."""
    html = """<html><head>
    <meta name="csrf-token" content="tok">
    <script type="application/ld+json">
    {"@type":"VideoGame","name":"Game","description":"Desc.","genre":[],
     "screenshot":[{"@type":"ImageObject","url":"https://cdn.example.com/shot.jpg"}]}
    </script></head><body></body></html>"""
    data = parse_game_page(html)
    assert data["screenshots"] == ["https://cdn.example.com/shot.jpg"]


def test_parse_game_page_handles_list_jsonld():
    """parse_game_page works when JSON-LD is wrapped in an array."""
    html = """<html><head>
    <meta name="csrf-token" content="tok">
    <script type="application/ld+json">
    [{"@type":"VideoGame","name":"Iron Lung","description":"A horror game.","genre":["Horror"]}]
    </script></head><body>
    <button @click.prevent="generateDownloadUrl(42)">Download</button>
    </body></html>"""
    data = parse_game_page(html)
    assert data["title"] == "Iron Lung"
    assert data["description"] == "A horror game."
    assert data["download_id"] == 42


def test_parse_livewire_results_extracts_games():
    """parse_livewire_results reads title/slug/cover_url from the listing attribute."""
    listing = json.dumps({
        "type": "game",
        "title": "Iron Lung",
        "slug": "iron-lung",
        "imageurl": "https://ankergames.net/storage/iron-lung.jpg",
        "coverurl": "https://ankergames.net/storage/cover-iron.jpg",
    })
    html = f'<div listing=\'{listing}\'><img src="x.jpg" alt="Iron Lung"/></div>'
    results = parse_livewire_results(html)
    assert len(results) == 1
    assert results[0]["slug"] == "iron-lung"
    assert results[0]["title"] == "Iron Lung"
    assert results[0]["cover_url"] == "https://ankergames.net/storage/iron-lung.jpg"


def test_parse_livewire_results_excludes_non_games():
    """parse_livewire_results skips entries that are not type=game."""
    game = json.dumps({"type": "game", "title": "Game A", "slug": "game-a",
                        "imageurl": "https://example.com/a.jpg", "coverurl": ""})
    genre = json.dumps({"type": "genre", "title": "Action", "slug": "action",
                         "imageurl": "", "coverurl": ""})
    html = (
        f"<div listing='{game}'></div>"
        f"<div listing='{genre}'></div>"
    )
    results = parse_livewire_results(html)
    assert len(results) == 1
    assert results[0]["slug"] == "game-a"


def test_parse_livewire_results_metadata_fields():
    """parse_livewire_results extracts genres, size_gb, and release_date."""
    listing = json.dumps({
        "type": "game",
        "title": "Hades",
        "slug": "hades",
        "imageurl": "https://ankergames.net/storage/hades.jpg",
        "coverurl": "",
        "genres": [
            {"id": 1, "title": "Action", "slug": "action"},
            {"id": 2, "title": "Roguelike", "slug": "roguelike"},
        ],
        "size_gb": "2.50",
        "release_date": "2020-09-17",
    })
    html = f"<div listing='{listing}'></div>"
    results = parse_livewire_results(html)
    assert len(results) == 1
    r = results[0]
    assert r["genres"] == ["Action", "Roguelike"]
    assert r["size_gb"] == "2.50"
    assert r["release_date"] == "2020-09-17"


def test_parse_livewire_results_missing_metadata_defaults():
    """parse_livewire_results returns empty defaults when metadata fields are absent."""
    listing = json.dumps({
        "type": "game",
        "title": "Game B",
        "slug": "game-b",
        "imageurl": "https://example.com/b.jpg",
        "coverurl": "",
    })
    html = f"<div listing='{listing}'></div>"
    results = parse_livewire_results(html)
    assert results[0]["genres"] == []
    assert results[0]["size_gb"] == ""
    assert results[0]["release_date"] == ""


def _listing_html(*items: dict, next_url: str | None = None) -> str:
    listings = "".join(
        f"<article listing='{json.dumps(item)}'></article>" for item in items
    )
    next_link = f'<a href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body>{listings}{next_link}</body></html>"


def _response(text: str):
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def test_livewire_search_filters_paginated_listing_pages():
    """livewire_search follows public listing pages and filters locally."""
    _page_cache.clear()
    page1 = _listing_html(
        {"type": "game", "title": "House Flipper", "slug": "house-flipper"},
        next_url="https://ankergames.net/games?page=2",
    )
    page2 = _listing_html(
        {
            "type": "game",
            "title": "Iron Lung",
            "slug": "iron-lung",
            "imageurl": "https://ankergames.net/storage/iron-lung.jpg",
            "genres": [{"title": "Horror"}],
            "size_gb": "0.20",
            "release_date": "2022-03-10",
        }
    )

    session = MagicMock()
    session.get.side_effect = [_response(page1), _response(page2)]

    results = livewire_search(session, "iron lung")

    assert len(results) == 1
    assert results[0]["slug"] == "iron-lung"
    assert results[0]["genres"] == ["Horror"]
    assert session.post.call_count == 0


def test_livewire_search_returns_empty_when_no_listing_matches():
    _page_cache.clear()
    page = _listing_html({"type": "game", "title": "Hades", "slug": "hades"})
    session = MagicMock()
    session.get.return_value = _response(page)

    assert livewire_search(session, "zzzz") == []


def test_livewire_search_stops_between_pages_when_cancelled():
    _page_cache.clear()
    cancelled = threading.Event()
    first_page = _listing_html(
        {"type": "game", "title": "Hades", "slug": "hades"},
        next_url="https://ankergames.net/games?page=2",
    )
    session = MagicMock()

    def get_first_page(*_args, **_kwargs):
        cancelled.set()
        return _response(first_page)

    session.get.side_effect = get_first_page

    assert livewire_search(
        session,
        "iron lung",
        should_cancel=cancelled.is_set,
    ) == []
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# get_download_url tests
# ---------------------------------------------------------------------------

_TREASURE_BOX_HTML = """
<html><body>
<div x-data="downloadPage('https%3A%2F%2Fcdn.example.com%2Ffiles%2FHades.zip', null, false, null, null)">
</div>
</body></html>
"""


def test_get_download_url_resolves_cdn_url():
    """get_download_url follows the treasure-box page to extract the real CDN URL."""
    session = MagicMock()
    # POST response: treasure-box page URL
    session.post.return_value.json.return_value = {
        "download_url": "https://ankergames.net/download/token123/hash456"
    }
    session.post.return_value.raise_for_status = MagicMock()
    # GET response: treasure-box HTML with encoded CDN URL
    session.get.return_value.text = _TREASURE_BOX_HTML
    session.get.return_value.raise_for_status = MagicMock()

    url = get_download_url(session, 308, "csrf-tok")

    assert url == "https://cdn.example.com/files/Hades.zip"
    # GET must have been called with the treasure-box URL
    session.get.assert_called_once()
    call_url = session.get.call_args.args[0]
    assert "ankergames.net/download/token123" in call_url


def test_get_download_url_unescapes_json_slashes():
    """Literal JSON slash escapes must not reach the HTTP downloader."""
    session = MagicMock()
    session.post.return_value.json.return_value = {
        "download_url": "https://ankergames.net/download/token123/hash456"
    }
    session.post.return_value.raise_for_status = MagicMock()
    session.get.return_value.text = r"""
        <div x-data="downloadPage('https:\/\/ankergames.net\/download-file\/a5bd0bf3d3705aa3c5604eeec13dd14812dfc6c56ee62c1a', null)"></div>
    """
    session.get.return_value.raise_for_status = MagicMock()

    url = get_download_url(session, 308, "csrf-tok")

    assert url == (
        "https://ankergames.net/download-file/"
        "a5bd0bf3d3705aa3c5604eeec13dd14812dfc6c56ee62c1a"
    )


def test_get_download_url_raises_on_missing_cdn_url():
    """get_download_url raises RuntimeError if no downloadPage() found in page."""
    session = MagicMock()
    session.post.return_value.json.return_value = {
        "download_url": "https://ankergames.net/download/token/hash"
    }
    session.post.return_value.raise_for_status = MagicMock()
    session.get.return_value.text = "<html><body>No download here</body></html>"
    session.get.return_value.raise_for_status = MagicMock()

    import pytest
    with pytest.raises(RuntimeError, match="treasure-box"):
        get_download_url(session, 99, "csrf")


def test_get_download_url_retries_with_fresh_csrf_on_419():
    """Cached guest pages can contain stale CSRF tokens, so retry once."""
    stale_resp = MagicMock()
    stale_resp.status_code = 419
    stale_error = requests.HTTPError(response=stale_resp)
    stale_resp.raise_for_status.side_effect = stale_error

    post_resp = MagicMock()
    post_resp.json.return_value = {
        "download_url": "https://ankergames.net/download/token/hash"
    }
    post_resp.raise_for_status = MagicMock()

    csrf_resp = MagicMock()
    csrf_resp.json.return_value = {"token": "fresh-token"}
    csrf_resp.raise_for_status = MagicMock()

    treasure_resp = MagicMock()
    treasure_resp.text = _TREASURE_BOX_HTML
    treasure_resp.raise_for_status = MagicMock()

    session = MagicMock()
    session.post.side_effect = [stale_resp, post_resp]
    session.get.side_effect = [csrf_resp, treasure_resp]

    url = get_download_url(session, 308, "stale-token")

    assert url == "https://cdn.example.com/files/Hades.zip"
    assert session.post.call_args_list[1].kwargs["headers"]["X-CSRF-TOKEN"] == "fresh-token"
    assert session.get.call_args_list[0].args[0] == "https://ankergames.net/csrf-token"

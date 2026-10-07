"""Parsers against the saved live pages (tests/fixtures/site) and synthetic snippets."""

from __future__ import annotations

import json
import re

import pytest

from anker_client.core.errors import SiteChangedError
from anker_client.core.models import DownloadKind
from anker_client.site import _js, parsers
from anker_client.site._html import form_errors, has_page_auth_meta, make_soup


@pytest.fixture(scope="module")
def page():
    from tests.conftest import SITE_FIXTURES

    cache: dict[str, str] = {}

    def read(name: str) -> str:
        if name not in cache:
            cache[name] = (SITE_FIXTURES / name).read_text(encoding="utf-8")
        return cache[name]

    return read


# --- listings -------------------------------------------------------------------------


def test_games_page1_cards_and_pagination(page) -> None:
    listing = parsers.parse_listing(page("games_page1.html"), page=1, url="https://ankergames.net/games")
    assert 50 <= len(listing.games) <= 60
    assert listing.has_next is True
    assert listing.page == 1
    first = listing.games[0]
    assert first.slug == "gears-of-war-e-day"
    assert first.title == "Gears of War: E-Day"
    assert first.size_text == "104.60 GB"
    assert first.size_bytes == int(104.60 * 1024**3)
    assert first.year == 2026
    assert first.primary_genre == "Action"
    assert first.cover_url.startswith("https://ankergames.net/uploads/poster/")
    assert first.cover_url.endswith(".jpg")
    slugs = [g.slug for g in listing.games]
    assert len(slugs) == len(set(slugs))
    assert all(g.title and g.slug for g in listing.games)


def test_games_last_page_is_final(page) -> None:
    listing = parsers.parse_listing(page("games_last_page.html"), page=37, url="https://ankergames.net/games?page=37")
    assert listing.has_next is False
    assert listing.total_pages == 37
    assert len(listing.games) > 0


def test_page_number_is_inferred_from_url_or_canonical(page) -> None:
    assert parsers.parse_listing(page("games_last_page.html")).page == 37  # canonical link
    assert parsers.parse_listing(page("search_call_page2.html"), url="https://ankergames.net/search/call?page=2").page == 2


@pytest.mark.parametrize(
    ("name", "page_no", "first_slug"),
    [
        ("games_sort_title.html", 1, "drive-rally"),
        ("genre_action.html", 1, "gears-of-war-e-day"),
        ("games_vr.html", 1, "gunman-contracts-stand-alone"),
        ("search_call.html", 1, "call-of-duty-modern-warfare-2"),
        ("search_call_page2.html", 2, "final-fantasy-vii"),
    ],
)
def test_other_listings(page, name: str, page_no: int, first_slug: str) -> None:
    listing = parsers.parse_listing(page(name), page=page_no)
    assert listing.games[0].slug == first_slug
    assert len(listing.games) == 56
    assert listing.has_next is True


def test_titles_are_unescaped(page) -> None:
    titles = {g.title for g in parsers.parse_listing(page("genre_action.html")).games}
    assert "Mirror's Edge Catalyst" in titles
    assert not any("&#" in t or "&amp;" in t for t in titles)


def test_double_encoded_entities_are_unescaped() -> None:
    html = """<article x-data="uiPostCard('https:\\/\\/ankergames.net\\/game\\/baldis-basics')">
      <h3 title="Baldi&amp;#039;s Basics">Baldi&amp;#039;s Basics</h3></article>"""
    assert parsers.parse_cards(html)[0].title == "Baldi's Basics"


def test_top_games_podium_and_numbered_pagination(page) -> None:
    listing = parsers.parse_listing(page("top_games.html"))
    assert listing.total_pages == 86
    assert listing.has_next
    podium = listing.games[:3]
    assert [g.slug for g in podium] == ["grand-theft-auto-v", "grand-theft-auto-v-enhanced", "meccha-chameleon"]
    assert podium[0].title == "Grand Theft Auto V"
    assert podium[0].year == 2013
    assert podium[0].size_text == "118.4 GB"
    assert podium[0].primary_genre == "Action"
    assert podium[1].title == "Grand Theft Auto V Enhanced"
    assert podium[1].cover_url.endswith(".jpg")
    assert len(listing.games) == 27  # 3 podium + 24 cards


def test_cards_are_deduplicated_keeping_first() -> None:
    card = """<article x-data="uiPostCard('https:\\/\\/ankergames.net\\/game\\/{slug}')">
        <picture><source srcset="https://ankergames.net/p/{slug}.webp" type="image/webp">
        <img src="https://ankergames.net/p/{slug}.jpg" alt="{title}"></picture>
        <p title="RPG">RPG</p><h3 title="{title}">{title}</h3>
        <p><span>2020</span><span></span><span class="truncate">12 GB</span></p></article>"""
    html = (
        card.format(slug="a", title="First A")
        + card.format(slug="b", title="B")
        + card.format(slug="a", title="Second A")
    )
    games = parsers.parse_cards(html)
    assert [(g.slug, g.title) for g in games] == [("a", "First A"), ("b", "B")]
    assert games[0].primary_genre == "RPG"
    assert games[0].size_bytes == 12 * 1024**3


def test_card_with_missing_optional_fields() -> None:
    html = """<article x-data="uiPostCard('https:\\/\\/ankergames.net\\/game\\/bare')">
        <img src="data:image/svg+xml,%3Csvg/%3E" alt="Bare">
        <picture><source srcset="https://ankergames.net/p/bare.webp 1x" type="image/webp"></picture>
        <h3>Bare Game</h3><p><span class="truncate">日本</span></p></article>"""
    (game,) = parsers.parse_cards(html)
    assert game.title == "Bare Game"
    assert game.year is None
    assert game.size_text == "" and game.size_bytes is None
    assert game.primary_genre == ""
    assert game.cover_url == "https://ankergames.net/p/bare.webp"


def test_livewire_search_fragment_link_cards(page) -> None:
    payload = json.loads(page("livewire_search_response.json"))
    games = parsers.parse_cards(payload["components"][0]["effects"]["html"])
    assert [g.slug for g in games] == [
        "hollow-knight",
        "hollow-knight-silksong",
        "mina-the-hollower",
        "watch-dogs-2",
        "void-crew",
    ]
    assert games[1].title == "Hollow Knight: Silksong"
    assert games[0].cover_url.endswith("0PD0P2pAZs.jpg")


def test_livewire_post_filter_fragment(page) -> None:
    games = parsers.parse_cards(page("livewire_post_filter_fragment.html"))
    assert len(games) == 10
    assert games[0].slug == "hollow-knight"


def test_wrapper_articles_are_not_cards() -> None:
    html = """<article class="wrapper"><h2>Featured</h2>
      <article x-data="uiPostCard('https:\\/\\/ankergames.net\\/game\\/inner')"><h3 title="Inner">Inner</h3></article>
    </article>"""
    assert [g.slug for g in parsers.parse_cards(html)] == ["inner"]


def test_listing_without_cards_ignores_sidebar_game_links(page) -> None:
    # A search page whose cards are gone (no results) still has the "Top this week"
    # sidebar linking to unrelated games: those are not results, and there is no next page.
    html = re.sub(r"<article\b[^>]*uiPostCard.*?</article>", "", page("search_call.html"), flags=re.S)
    assert "/game/minecraft" in html and 'rel="next"' in html
    listing = parsers.parse_listing(html, url="https://ankergames.net/search/zzqx")
    assert listing.games == []
    assert listing.has_next is False
    # Fragments (the Livewire quick-search list) still use the link-card reader.
    assert [g.slug for g in parsers.parse_cards(html)][:2] == ["minecraft", "lethal-company"]


def test_empty_listing() -> None:
    listing = parsers.parse_listing("<html><body><p>No results</p></body></html>", page=1)
    assert listing.games == [] and listing.has_next is False


# --- home & genres --------------------------------------------------------------------


def test_home_sections(page) -> None:
    sections = parsers.parse_home_sections(page("home.html"))
    titles = [s.title for s in sections]
    assert titles[:3] == ["Trending Games", "Upcoming Games", "Latest Games"]
    assert "Epic Collections" not in titles  # collection hubs carry no game cards
    by_title = {s.title: s for s in sections}
    assert len(by_title["Trending Games"].games) == 16
    assert by_title["Latest Games"].games[0].slug == "gears-of-war-e-day"
    upcoming = by_title["Upcoming Games"].games
    assert [g.title for g in upcoming] == ["Far Cry", "Manifold Garden", "7 Days to End with You"]
    assert upcoming[0].slug == "far-cry"
    assert upcoming[0].year == 2008
    assert upcoming[0].primary_genre == "Action"
    gotd = by_title["Game of the Day"].games
    assert [g.slug for g in gotd] == ["i-know-a-guy-shady-life-simulator"]
    assert gotd[0].cover_url.endswith(".jpg") and "poster" in gotd[0].cover_url
    assert gotd[0].size_text == "1.2 GB"
    assert all(s.games for s in sections)


def test_home_page_helper_returns_sections_and_genres(page) -> None:
    sections, genres = parsers._parse_home_page(page("home.html"))
    assert sections and len(genres) == 18


def test_genres(page) -> None:
    genres = parsers.parse_genres(page("home.html"))
    assert [g.slug for g in genres] == [
        "action", "adventure", "anime", "classic", "fighting", "history", "horror", "indie", "multiplayer",
        "nsfw", "open-world", "puzzle", "racing", "rpg", "simulation", "sports", "survival", "vr",
    ]  # fmt: skip
    names = {g.slug: g.name for g in genres}
    assert names["open-world"] == "Open World"
    assert names["nsfw"] == "NSFW"
    assert names["rpg"] == "RPG"


def test_genres_fallback_without_nav_strips_counts() -> None:
    html = """<a href="https://ankergames.net/genre/action">Action 1229 Games</a>
              <a href="/genre/horror">Horror <span>262 Games</span></a>
              <a href="https://ankergames.net/genre/action">Action</a>"""
    genres = parsers.parse_genres(html)
    assert [(g.slug, g.name) for g in genres] == [("action", "Action"), ("horror", "Horror")]


# --- game page ------------------------------------------------------------------------


def test_hollow_knight_details(page) -> None:
    details = parsers.parse_game_page(page("game_hollow_knight.html"), slug="hollow-knight")
    assert details.title == "Hollow Knight"
    assert details.version == "v1.5.12620"
    assert details.size_text == "1.1 GB"
    assert details.size_bytes == int(1.1 * 1024**3)
    assert details.release_date == "2017-02-24"
    assert details.updated_date == "2026-09-06"
    assert len(details.screenshots) == 4
    assert details.screenshots[0].startswith("https://ankergames.net/uploads/screenshots/")
    assert "%20" in details.screenshots[0] and " " not in details.screenshots[0]
    assert len(details.genres) == 12
    assert "Action" in details.genres
    assert details.cover_url.endswith("/0PD0P2pAZs.jpg")  # 2:3 poster
    assert details.hero_url.endswith("/cover-nAX9bMGrhU.jpg")  # wide
    assert details.description.startswith("Hollow Knight is a hand-drawn")
    assert details.torrent_available is True
    assert details.fetched_at
    (option,) = details.download_options
    assert option.download_id == 232
    assert option.label == "Direct"
    assert option.kind is DownloadKind.FULL
    assert details.primary_option == option
    req = details.requirements
    assert req.os == "Windows 10"
    assert req.processor == "Intel Core i5"
    assert req.memory == "8 GB RAM"
    assert req.graphics == "GeForce GTX 560"
    assert req.directx == "Version 11"
    assert req.storage == "9 GB available space"
    assert req.raw.splitlines()[0] == "Requires 64-bit of operating system"
    summary = details.to_summary()
    assert summary.year == 2017 and summary.primary_genre == "Action"


def test_gears_of_war_details(page) -> None:
    details = parsers.parse_game_page(page("game_gears_of_war.html"), slug="gears-of-war-e-day")
    assert details.title == "Gears of War: E-Day"
    assert details.version == "Build 25724756"
    assert details.cover_url.endswith(".jpg") and "poster" in details.cover_url
    assert "cover" in details.hero_url
    assert [o.download_id for o in details.download_options] == [6348]
    assert details.requirements.directx == "Version 12"
    assert details.requirements.raw.startswith("Minimum:")


def _game_page(options_html: str, extra: str = "", ld: dict | None = None) -> str:
    data = ld if ld is not None else {"@type": "VideoGame", "name": "Some Game", "softwareVersion": "v4.1"}
    return f"""<html><head><script type="application/ld+json">{json.dumps([data])}</script></head><body>
      <template x-if="downloadOpen"><div><ul>{options_html}</ul></div></template>{extra}
      <script>function x() {{ generateTorrentUrl(downloadId) }}</script></body></html>"""


def _option(label: str, download_id: int) -> str:
    return f"""<li class="py-4 font-medium"><div class="flex"><div>{label}</div><div>
      <a href="#" @click.prevent="generateDownloadUrl({download_id})" x-bind:disabled="isLoading">
      <span x-text="isLoading ? 'Loading...' : 'Download'">Download</span></a></div></div></li>"""


def test_download_option_labels_are_classified() -> None:
    html = _game_page(
        _option("Direct V 4.1.1.7631656", 11)
        + _option("Language Pack (61.2 GB)", 12)
        + _option("Launcher", 13)
        + _option("Update Only From V 4.1.1.7398727 To V 4.1.1.7631656 (124 MB)", 14)
        + _option("Direct V 4.1.1.7631656", 11)  # duplicate id
    )
    details = parsers.parse_game_page(html, slug="some-game")
    options = {o.download_id: o for o in details.download_options}
    assert list(options) == [11, 12, 13, 14]
    assert options[11].kind is DownloadKind.FULL and options[11].to_version == "4.1.1.7631656"
    assert options[11].label == "Direct V 4.1.1.7631656"
    assert options[12].kind is DownloadKind.ADDON and options[12].size_text == "61.2 GB"
    assert options[13].kind is DownloadKind.ADDON and options[13].size_text == ""
    patch = options[14]
    assert patch.kind is DownloadKind.PATCH
    assert (patch.from_version, patch.to_version, patch.size_text) == ("4.1.1.7398727", "4.1.1.7631656", "124 MB")
    # The JS function *definition* is not a torrent marker.
    assert details.torrent_available is False


def test_torrent_available_for_signed_in_markup() -> None:
    extra = """<button @click.prevent="generateTorrentUrl(77)">Download .torrent File</button>"""
    details = parsers.parse_game_page(_game_page(_option("Direct", 1), extra), slug="x")
    assert details.torrent_available is True


def test_game_page_without_json_ld_falls_back_to_heading() -> None:
    html = f"""<html><body><h1> Fallback   Title </h1>
        <template x-if="downloadOpen"><ul>{_option("Direct", 5)}</ul></template></body></html>"""
    details = parsers.parse_game_page(html, slug="fallback")
    assert details.title == "Fallback Title"
    assert details.version == "" and details.genres == [] and details.screenshots == []


def test_game_page_without_title_raises_site_changed() -> None:
    with pytest.raises(SiteChangedError):
        parsers.parse_game_page("<html><body><h1>Free Games</h1></body></html>", slug="gone")


def test_game_page_json_ld_variants() -> None:
    ld = {
        "@graph": [
            {"@type": ["VideoGame", "SoftwareApplication"], "name": "Graph &amp; Co", "genre": "Action, RPG",
             "image": "https://ankergames.net/uploads/poster/a-poster_1.jpg",
             "screenshot": [{"@type": "ImageObject", "url": "/uploads/s 1.jpg"}],
             "datePublished": "24 Feb, 2017", "dateModified": "2026-09-06T10:00:00+00:00",
             "description": "Line one.\r\n\r\nLine   two."},
        ]
    }  # fmt: skip
    details = parsers.parse_game_page(_game_page("", ld=ld), slug="graph")
    assert details.title == "Graph & Co"
    assert details.genres == ["Action", "RPG"]
    assert details.cover_url == "https://ankergames.net/uploads/poster/a-poster_1.jpg"
    assert details.screenshots == ["https://ankergames.net/uploads/s%201.jpg"]
    assert details.release_date == "2017-02-24"
    assert details.updated_date == "2026-09-06"
    assert details.description == "Line one.\n\nLine two."
    assert details.download_options == []


def test_parse_csrf_token(page) -> None:
    assert parsers.parse_csrf_token(page("game_hollow_knight.html")) == "IhOK3lsoeauPXHG2TGLfNV14bEnwOs1PYYF6iVF1"
    assert parsers.parse_csrf_token("<html></html>") == ""


# --- ticket page ----------------------------------------------------------------------


def test_ticket_page(page) -> None:
    ticket_url = "https://ankergames.net/download/eyJabc/123"
    ticket = parsers.parse_ticket_page(page("ticket_page.html"), ticket_url=ticket_url)
    assert ticket.ticket_url == ticket_url
    assert ticket.file_url == (
        "https://ankergames.net/download-file/1893ac5d4b8106b7d614afe604fcd175ec630a3dde768f6e"
    )
    assert ticket.wait_seconds == 5
    assert ticket.requires_verification is True
    assert ticket.turnstile_sitekey == "0x4AAAAAABL9QVQPEjEo2nnS"
    assert ticket.external_provider == ""
    assert ticket.is_torrent is False
    assert ticket.version == "V 1.5.12620"
    assert ticket.size_text == "1.1 GB"


def _ticket(args: str, extra: str = "") -> str:
    return f"""<html><body><main><div x-data="downloadPage({args})"></div>{extra}</main></body></html>"""


def test_ticket_page_external_provider_and_torrent_variants() -> None:
    external = _ticket(
        "'https:\\/\\/ankergames.net\\/download-file\\/t1', {&quot;name&quot;:&quot;Mega&quot;,&quot;cta&quot;:&quot;Go&quot;}, false, null, null, 0"
    )
    ticket = parsers.parse_ticket_page(external, ticket_url="https://ankergames.net/download/a/b")
    assert ticket.external_provider == "Mega"
    assert ticket.wait_seconds == 0
    assert ticket.requires_verification is False

    relaxed = _ticket("'/download-file/t2', {name: 'Mediafire'}, true, 'processing', '/status/1', 12.2")
    ticket = parsers.parse_ticket_page(relaxed, ticket_url="https://ankergames.net/download/a/b")
    assert ticket.external_provider == "Mediafire"
    assert ticket.is_torrent is True
    assert ticket.wait_seconds == 13
    assert ticket.file_url == "https://ankergames.net/download-file/t2"


def test_ticket_page_percent_encoded_url_and_default_wait() -> None:
    html = _ticket("'https%3A%2F%2Fankergames.net%2Fdownload-file%2Ft3'")
    ticket = parsers.parse_ticket_page(html, ticket_url="https://ankergames.net/download/a/b")
    assert ticket.file_url == "https://ankergames.net/download-file/t3"
    assert ticket.wait_seconds == 5  # the page script's default


def test_ticket_page_missing_component_raises() -> None:
    with pytest.raises(SiteChangedError):
        parsers.parse_ticket_page("<html><body>Expired</body></html>", ticket_url="https://ankergames.net/download/x/y")


# --- login & account ------------------------------------------------------------------


def test_login_form(page) -> None:
    fields = parsers.parse_login_form(page("login.html"))
    assert fields["_token"] == "lq9DmhqYzwTxh0bF9DL3nO9BVMItHFPLZP98SS6V"
    assert "remember" in fields
    assert "email" not in fields and "password" not in fields


def test_login_form_missing_raises() -> None:
    with pytest.raises(SiteChangedError):
        parsers.parse_login_form("<html><form action='/search'><input name='q'></form></html>")


@pytest.mark.parametrize(
    "name", ["home.html", "login.html", "game_hollow_knight.html", "ticket_page.html", "games_page1.html"]
)
def test_guest_pages_are_not_logged_in(page, name: str) -> None:
    assert parsers.parse_logged_in_user(page(name)) is None


SIGNED_IN_HEADER = """<html><head><meta name="page-auth" content="1"></head><body>
<header><nav><div class="account-menu">
  <button><img class="rounded-full" src="/storage/avatars/42.png" alt="neo avatar"><span>neo</span></button>
  <div class="dropdown">
    <a href="https://ankergames.net/profile/neo">neo</a>
    <a href="https://ankergames.net/settings">Settings</a>
    <span class="badge">Premium</span>
    <form method="POST" action="https://ankergames.net/logout"><input type="hidden" name="_token" value="t">
      <button type="submit">Log out</button></form>
  </div></div></nav></header>
<main><div class="comment"><a href="https://ankergames.net/profile/someone-else">someone-else</a></div></main>
</body></html>"""


def test_logged_in_user_from_account_menu() -> None:
    user = parsers.parse_logged_in_user(SIGNED_IN_HEADER)
    assert user is not None
    assert user.display_name == "neo"
    assert user.profile_url == "https://ankergames.net/profile/neo"
    assert user.avatar_url == "https://ankergames.net/storage/avatars/42.png"
    assert user.is_subscriber is True


def test_logged_in_user_generic_profile_label_uses_avatar_alt_or_url() -> None:
    html = """<header><div><img src="/a.png" alt="Trinity's avatar">
      <a href="/profile/trinity99">Profile</a><a href="/logout">Log out</a>
      <a href="/subscribe">Upgrade to Premium</a></div></header>"""
    user = parsers.parse_logged_in_user(html)
    assert user is not None
    assert user.display_name == "Trinity"
    assert user.is_subscriber is False  # an upsell is not a badge

    no_avatar = """<header><div><a href="/profile/morpheus">My Profile</a>
      <form action="/logout" method="post"></form></div></header>"""
    user = parsers.parse_logged_in_user(no_avatar)
    assert user is not None and user.display_name == "morpheus"


def test_logged_in_user_minimal_markers() -> None:
    # A bare logout link with nothing else: signed in, name unknown (client falls back to email).
    user = parsers.parse_logged_in_user('<body><a href="https://ankergames.net/logout">Log out</a></body>')
    assert user is not None and user.display_name == ""
    # page-auth alone counts, unless a visible login link contradicts it.
    assert parsers.parse_logged_in_user('<head><meta name="page-auth" content="1"></head>') is not None
    contradicted = '<head><meta name="page-auth"></head><body><a href="/login">Log in</a></body>'
    assert parsers.parse_logged_in_user(contradicted) is None
    assert parsers.parse_logged_in_user('<head><meta name="page-auth" content="guest"></head>') is None


def test_logged_in_markers_vs_guest_only_login_links() -> None:
    hidden_login = """<body><li class="cf-guest-only hidden"><a href="/login">Log in</a></li>
        <form action="/logout" method="POST"></form></body>"""
    assert parsers.parse_logged_in_user(hidden_login) is not None
    visible_login = """<body><a href="/login">Log in</a><form action="/logout" method="POST"></form></body>"""
    assert parsers.parse_logged_in_user(visible_login) is None


def test_page_auth_meta_detection() -> None:
    assert has_page_auth_meta(SIGNED_IN_HEADER)
    assert not has_page_auth_meta("<meta name='csrf-token' content='x'>")


def test_form_errors() -> None:
    html = """<form><ul class="text-sm text-red-600 space-y-1"><li>These credentials do not match our records.</li></ul>
      <p class="text-red-600">These credentials do not match our records.</p></form>"""
    assert form_errors(html) == ["These credentials do not match our records."]


# --- livewire bits --------------------------------------------------------------------


def test_livewire_config_and_snapshots(page) -> None:
    config = parsers.parse_livewire_config(page("home.html"))
    assert config["uri"].endswith("/update")
    assert config["uri"].startswith("https://ankergames.net/livewire-")
    assert config["csrf"]
    snapshots = parsers.parse_livewire_snapshots(page("home.html"))
    assert "search-component" in snapshots
    snapshot = json.loads(snapshots["search-component"])
    assert snapshot["memo"]["name"] == "search-component"
    assert snapshot["data"]["q"] == ""
    assert parsers.parse_livewire_config("<html></html>") == {}
    assert parsers.parse_livewire_snapshots("<div wire:snapshot='not json'></div>") == {}


# --- helpers --------------------------------------------------------------------------


def test_js_call_arguments_and_literals() -> None:
    args = _js.call_arguments("downloadPage('a,b', {x: [1, 2], y: 'q)'}, true, null, \"s\\\"q\", 5)", "downloadPage")
    assert args == ["'a,b'", "{x: [1, 2], y: 'q)'}", "true", "null", '"s\\"q"', "5"]
    values = [_js.literal(a) for a in args]
    assert values == ["a,b", {"x": [1, 2], "y": "q)"}, True, None, 's"q', 5]
    assert _js.literal("2.5") == 2.5
    assert _js.literal("'\\u00e9\\x41\\/\\n'") == "éA/\n"
    assert _js.literal("'\\ud83d\\ude00'") == "😀"
    assert _js.call_arguments("other()", "downloadPage") is None
    assert _js.call_arguments("downloadPage(", "downloadPage") is None
    assert _js.call_arguments("downloadPage()", "downloadPage") == []
    assert _js.call_arguments("x.downloadPage(1)", "downloadPage") is None  # a method, not the call


def test_make_soup_keeps_alpine_event_attributes() -> None:
    soup = make_soup('<a @click.prevent="go(1)" href="#">x</a>', keep_alpine_events=True)
    assert soup.a["x-on:click.prevent"] == "go(1)"


@pytest.mark.parametrize(
    ("args", "wait", "is_torrent"),
    [
        # downloadPage(url, provider, isTorrent, status, statusUrl, waitSeconds = 5):
        # Math.max(0, Math.ceil(waitSeconds)) and JS truthiness for isTorrent.
        ("'/download-file/t', null, false, null, null, '30'", 30, False),
        ("'/download-file/t', null, 1, null, null, ' 2.1 '", 3, True),
        ("'/download-file/t', null, 0, null, null, null", 0, False),
        ("'/download-file/t', null, '', null, null, 'soon'", 0, False),
        ("'/download-file/t', null, 'yes', null, null, -4", 0, True),
        ("'/download-file/t', null, false, null, null, Infinity", 0, False),
        ("'/download-file/t', null, true", 5, True),
    ],
)
def test_ticket_page_values_follow_the_page_script(args: str, wait: int, is_torrent: bool) -> None:
    ticket = parsers.parse_ticket_page(_ticket(args), ticket_url="https://ankergames.net/download/a/b")
    assert (ticket.wait_seconds, ticket.is_torrent) == (wait, is_torrent)


def test_ticket_file_url_is_always_uri_decoded() -> None:
    # The page script runs decodeURIComponent() on the URL, encoded scheme or not.
    html = _ticket(r"'\/download-file\/ab%2Dcd%3Fx%3D1'")
    ticket = parsers.parse_ticket_page(html, ticket_url="https://ankergames.net/download/a/b")
    assert ticket.file_url == "https://ankergames.net/download-file/ab-cd?x=1"


def test_mirror_named_full_download_is_not_an_addon(site_fixture) -> None:
    """Regression: L4D2 lists its only download under the host "DataNodes" with MD5/SHA-256 rows."""
    from anker_client.core.models import DownloadKind
    from anker_client.site.parsers import parse_game_page

    details = parse_game_page(site_fixture("game_left_4_dead_2.html"), slug="left-4-dead-2")
    assert [(o.download_id, o.label, o.kind) for o in details.download_options] == [
        (4150, "DataNodes", DownloadKind.FULL)
    ]
    assert details.primary_option is not None and details.primary_option.download_id == 4150


def test_download_label_classification() -> None:
    from anker_client.core.models import DownloadKind

    assert DownloadKind.classify("Direct") is DownloadKind.FULL
    assert DownloadKind.classify("Direct V 4.1.1.7631656") is DownloadKind.FULL
    assert DownloadKind.classify("DataNodes") is DownloadKind.FULL
    assert DownloadKind.classify("Mirror 2") is DownloadKind.FULL
    assert DownloadKind.classify("Language Pack (61.2 GB)") is DownloadKind.ADDON
    assert DownloadKind.classify("Launcher") is DownloadKind.ADDON
    assert DownloadKind.classify("Update Only From V 4.1.1.7398727 To V 4.1.1.7631656 (124 MB)") is DownloadKind.PATCH

"""AnkerGamesClient driven against a local HTTP server that mimics ankergames.net."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator

import pytest

from anker_client.core.errors import (
    AccessDeniedError,
    AnkerError,
    AuthError,
    GeoBlockedError,
    LinkExpiredError,
    LoginFailedError,
    NetworkError,
    NotFoundError,
    NotLoggedInError,
    QuotaExceededError,
    RateLimitedError,
    SiteChangedError,
    VerificationError,
)
from anker_client.core.models import SortOrder
from anker_client.site.client import (
    AnkerGamesClient,
    _append_query_param,
    _disposition_filename,
    _is_dot_segment,
    _safe_filename,
)
from anker_client.site.http import HttpClient
from tests.conftest import SITE_FIXTURES
from tests.unit.test_site_server import LocalSite, Reply, Seen, serve_local_site


@pytest.fixture
def local_site() -> Iterator[LocalSite]:
    yield from serve_local_site()

SIGNED_IN_PAGE = """<html><head><meta name="page-auth" content="1"></head><body><header><nav>
  <div class="menu"><img class="avatar" src="/avatars/neo.png" alt="neo avatar">
  <a href="/profile/neo">neo</a>
  <form method="POST" action="/logout"><input type="hidden" name="_token" value="x"><button>Log out</button></form>
  </div></nav></header></body></html>"""

GUEST_PAGE = """<html><body><header><ul><li class="cf-guest-only"><a href="https://ankergames.net/login">Log in</a></li>
  </ul></header><nav aria-label="Genres"><a href="https://ankergames.net/genre/action">Action</a></nav></body></html>"""

LOGIN_ERROR = '<ul class="text-sm text-red-600"><li>{}</li></ul>'


def fixture(name: str) -> str:
    return (SITE_FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def env(local_site: LocalSite) -> Iterator[tuple[LocalSite, AnkerGamesClient]]:
    http = HttpClient(backoff_base=0.01)
    client = AnkerGamesClient(http, local_site.base_url + "/")
    local_site.route("GET", "/csrf-token", Reply.json({"token": "T1"}))
    try:
        yield local_site, client
    finally:
        http.close()


def html_route(text: str) -> Reply:
    return Reply(200, text)


# --- browsing -------------------------------------------------------------------------


def test_browse_default_and_sorted_pages(env) -> None:
    site, client = env
    site.route("GET", "/games", html_route(fixture("games_page1.html")))
    first = client.browse()
    assert first.games[0].slug == "gears-of-war-e-day"
    assert first.has_next and first.page == 1
    second = client.browse(page=2, sort=SortOrder.TITLE)
    assert second.page == 2
    queries = [r.query for r in site.seen("GET", "/games")]
    assert queries == [{}, {"page": ["2"], "sort": ["title"]}]


def test_browse_genre_and_vr_paths(env) -> None:
    site, client = env
    site.route("GET", "/genre/open-world", html_route(fixture("genre_action.html")))
    site.route("GET", "/games/vr", html_route(fixture("games_vr.html")))
    assert client.browse(genre="Open-World").games
    assert client.browse(genre="vr", sort=SortOrder.MOST_VIEWED).games[0].slug == "gunman-contracts-stand-alone"
    assert site.seen("GET", "/games/vr")[0].query == {"sort": ["view"]}


def test_browse_past_the_end_is_an_empty_final_page(env) -> None:
    _site, client = env
    listing = client.browse(page=38)  # the local site has no /games route → 404
    assert listing.games == [] and listing.has_next is False and listing.page == 38
    with pytest.raises(NotFoundError):
        client.browse(page=1, genre="does-not-exist")


def test_browse_redirect_back_to_first_page_is_the_end(env) -> None:
    site, client = env

    def games(seen: Seen) -> Reply:
        return Reply.redirect("/games") if seen.query.get("page") else html_route(fixture("games_page1.html"))

    site.route("GET", "/games", games)
    assert client.browse(page=99).games == []


def test_search_quotes_query_and_paginates(env) -> None:
    site, client = env
    site.route("GET", "/search/call%20of%20duty", html_route(fixture("search_call.html")))
    site.route("GET", "/search/a%20b", html_route(fixture("search_call_page2.html")))
    result = client.search("  call   of duty ")
    assert result.games[0].slug == "call-of-duty-modern-warfare-2"
    page2 = client.search("a/b", page=2)
    assert page2.page == 2 and page2.has_next
    assert site.seen("GET", "/search/a%20b")[0].query == {"page": ["2"]}
    assert client.search("   ").games == []
    assert client.search("zzz", page=3).games == []  # 404 past the end


def test_dot_only_queries_and_genres_never_reach_the_home_page(env) -> None:
    site, client = env
    # "/search/.." is normalised to "/" (urllib3 / Cloudflare): never send dot-only segments.
    site.route("GET", "/", html_route(fixture("home.html")))
    for query in ("..", ".", " ... "):
        assert client.search(query).games == []
    with pytest.raises(NotFoundError):
        client.browse(genre="..")
    assert site.seen() == []


def test_search_answered_from_another_page_has_no_results(env) -> None:
    site, client = env
    site.route("GET", "/search/odd", Reply.redirect("/"))
    site.route("GET", "/", html_route(fixture("home.html")))
    assert client.search("odd").games == []
    assert [r.path for r in site.seen()] == ["/search/odd", "/"]


def test_search_without_results_page_is_empty_not_an_error(env) -> None:
    site, client = env
    # Page 1 answering 404 means "nothing found" for a search (a browse 404 is a real error).
    assert client.search("nothing-here").games == []
    # A results page whose only game links are the "Top this week" sidebar has no results.
    page = re.sub(r"<article\b[^>]*uiPostCard.*?</article>", "", fixture("search_call.html"), flags=re.S)
    assert "Top this week" in page and "/game/minecraft" in page
    site.route("GET", "/search/zzqx", html_route(page))
    listing = client.search("zzqx")
    assert listing.games == [] and listing.has_next is False


def test_is_dot_segment() -> None:
    assert _is_dot_segment(".") and _is_dot_segment("..") and _is_dot_segment("...")
    assert not _is_dot_segment("") and not _is_dot_segment("v1.5") and not _is_dot_segment(". a")


def test_top_games(env) -> None:
    site, client = env
    site.route("GET", "/top-games", html_route(fixture("top_games.html")))
    games = client.top_games()
    assert games[0].slug == "grand-theft-auto-v"
    assert len(games) == 27


def test_home_sections_also_caches_genres(env) -> None:
    site, client = env
    site.route("GET", "/", html_route(fixture("home.html")))
    sections = client.home_sections()
    assert [s.title for s in sections][:3] == ["Trending Games", "Upcoming Games", "Latest Games"]
    assert len(client.genres()) == 18
    assert len(site.seen("GET", "/")) == 1


def test_genres_are_cached_after_first_success(env) -> None:
    site, client = env
    site.route("GET", "/", [html_route("<html><body>maintenance</body></html>"), html_route(fixture("home.html"))])
    with pytest.raises(SiteChangedError):
        client.genres()
    assert [g.slug for g in client.genres()][:2] == ["action", "adventure"]
    client.genres()
    assert len(site.seen("GET", "/")) == 2


def test_game_details(env) -> None:
    site, client = env
    site.route("GET", "/game/hollow-knight", html_route(fixture("game_hollow_knight.html")))
    details = client.game_details("hollow-knight")
    assert details.title == "Hollow Knight"
    assert details.download_options[0].download_id == 232


def test_game_details_missing_or_redirected(env) -> None:
    site, client = env
    site.route("GET", "/game/removed", Reply.redirect("/games"))
    site.route("GET", "/games", html_route(fixture("games_page1.html")))
    with pytest.raises(NotFoundError):
        client.game_details("gone")
    with pytest.raises(NotFoundError):
        client.game_details("removed")
    with pytest.raises(NotFoundError):
        client.game_details("../etc")


# --- minting tickets ------------------------------------------------------------------


def test_mint_download_ticket_sends_what_the_site_sends(env) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/232", Reply.json({"success": True, "download_url": "/download/a/b"}))
    url = client.mint_download_ticket(232, referer_slug="hollow-knight")
    assert url == site.url("/download/a/b")
    post = site.seen("POST", "/generate-download-url/232")[0]
    assert post.json == {}
    assert post.headers["x-csrf-token"] == "T1"
    assert post.headers["x-requested-with"] == "XMLHttpRequest"
    assert "application/json" in post.headers["accept"]
    assert post.headers["content-type"] == "application/json"
    assert post.headers["origin"] == site.base_url
    assert post.headers["referer"] == site.url("/game/hollow-knight")
    # The CSRF token is cached between calls.
    client.mint_download_ticket(232)
    assert len(site.seen("GET", "/csrf-token")) == 1


@pytest.mark.parametrize(
    ("payload", "error", "check"),
    [
        ({"geo_blocked": True}, GeoBlockedError, None),
        ({"geo_blocked": True, "modal_type": "subscribe"}, AccessDeniedError, None),
        ({"modal_type": "subscribe"}, AccessDeniedError, None),
        ({"show_upgrade": True, "details": {"limit": 3, "used": 3}}, QuotaExceededError, "limit: 3"),
        ({"show_upgrade": True, "details": "Daily limit reached"}, QuotaExceededError, "Daily limit reached"),
        ({"show_upgrade": True}, QuotaExceededError, "download limit"),
        ({"error": "Too many requests. Please wait 42 seconds."}, RateLimitedError, 42),
        ({"error": "Please wait 2 minutes before trying again"}, RateLimitedError, 120),
        ({"error": "Please sign in to download this game."}, AccessDeniedError, "sign in"),
        ({"error": "This requires an active subscription"}, AccessDeniedError, None),
        ({"success": True}, SiteChangedError, None),
        ({"weird": 1}, SiteChangedError, None),
    ],
)
def test_mint_download_ticket_json_branches(env, payload: dict, error: type[Exception], check) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/7", Reply.json(payload))
    with pytest.raises(error) as info:
        client.mint_download_ticket(7)
    if isinstance(check, int):
        assert info.value.retry_after == check
    elif isinstance(check, str):
        assert check in info.value.user_message


def test_mint_generic_error_is_plain_anker_error(env) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/7", Reply.json({"error": "Download server is offline."}))
    with pytest.raises(AnkerError) as info:
        client.mint_download_ticket(7)
    assert type(info.value) is AnkerError
    assert info.value.user_message == "Download server is offline."


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(429, "slow down", {"Retry-After": "17"}), RateLimitedError),
        (Reply.redirect("/login"), NotLoggedInError),
        (Reply(401, "no"), NotLoggedInError),
        (Reply(403, "<html>forbidden</html>"), AccessDeniedError),
        (Reply(404, "<html>missing</html>"), NotFoundError),
        (Reply(500, "<html>oops</html>"), NetworkError),
        (Reply(200, "<html>not json</html>"), SiteChangedError),
        (Reply.json({"message": "Unauthenticated."}, status=401), NotLoggedInError),
        (Reply(403, "<html><title>Just a moment...</title></html>"), NetworkError),
    ],
)
def test_mint_non_json_answers(env, reply: Reply, error: type[Exception]) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/9", reply)
    with pytest.raises(error) as info:
        client.mint_download_ticket(9)
    if error is RateLimitedError:
        assert info.value.retry_after == 17
    assert len(site.seen("POST", "/generate-download-url/9")) == 1  # POST is never retried


def test_mint_refreshes_csrf_once_on_419(env) -> None:
    site, client = env
    tokens = iter(["T1", "T2"])
    site.route("GET", "/csrf-token", lambda seen: Reply.json({"token": next(tokens)}))

    def mint(seen: Seen) -> Reply:
        if seen.headers.get("x-csrf-token") != "T2":
            return Reply(419, "Page Expired")
        return Reply.json({"success": True, "download_url": "https://ankergames.net/download/x/y"})

    site.route("POST", "/generate-download-url/5", mint)
    assert client.mint_download_ticket(5) == "https://ankergames.net/download/x/y"
    assert len(site.seen("GET", "/csrf-token")) == 2
    assert [p.headers["x-csrf-token"] for p in site.seen("POST", "/generate-download-url/5")] == ["T1", "T2"]


def test_mint_gives_up_after_second_419(env) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/5", Reply(419, "Page Expired"))
    with pytest.raises(AuthError):
        client.mint_download_ticket(5)
    assert len(site.seen("POST", "/generate-download-url/5")) == 2


def test_mint_falls_back_to_xsrf_cookie_without_csrf_endpoint(env) -> None:
    site, client = env
    site.route("GET", "/csrf-token", Reply(404, "missing"))
    site.route("GET", "/", Reply(200, GUEST_PAGE, {"Set-Cookie": "XSRF-TOKEN=abc%3D%3D; Path=/"}))
    site.route("POST", "/generate-download-url/3", Reply.json({"success": True, "download_url": "/download/1/2"}))
    assert client.current_user() is None
    client.mint_download_ticket(3)
    post = site.seen("POST", "/generate-download-url/3")[0]
    assert post.headers["x-xsrf-token"] == "abc=="
    assert "x-csrf-token" not in post.headers


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        # Laravel answers JSON requests with {"message": …} and the meaning in the status.
        (Reply.json({"message": "Unauthenticated."}, 401), NotLoggedInError, "sign in"),
        (Reply.json({"message": "Too Many Attempts."}, 429, {"Retry-After": "33"}), RateLimitedError, "33 seconds"),
        (Reply.json({"message": "Server Error"}, 500), NetworkError, "HTTP 500"),
        (Reply.json({"message": "Not Found"}, 404), NotFoundError, "no longer available"),
        (Reply.json({"message": "This action is unauthorized."}, 403), AccessDeniedError, "unauthorized"),
        (Reply.json({"message": "The download is being prepared."}, 422), AnkerError, "being prepared"),
        (Reply.json({"success": False, "message": "Torrents are disabled."}), AnkerError, "Torrents are disabled."),
        (Reply.json({"success": False, "message": "Please wait 9 seconds."}), RateLimitedError, "9 seconds"),
        (Reply.json({"error": "Please wait 2 hours before downloading again."}), RateLimitedError, "7200 seconds"),
    ],
)
def test_mint_laravel_json_errors_are_typed_by_status(env, reply: Reply, error: type[Exception], message: str) -> None:
    site, client = env
    site.route("POST", "/generate-torrent-url/8", reply)
    with pytest.raises(error) as info:
        client.mint_torrent_url(8)
    assert message in info.value.user_message
    if error is NetworkError:
        assert info.value.retryable and info.value.status == 500
    if error is AnkerError:
        assert type(info.value) is AnkerError


def test_mint_json_419_twice_is_a_session_error(env) -> None:
    site, client = env
    site.route("POST", "/generate-download-url/5", Reply.json({"message": "CSRF token mismatch."}, 419))
    with pytest.raises(AuthError) as info:
        client.mint_download_ticket(5)
    assert "session expired" in info.value.user_message
    assert len(site.seen("POST", "/generate-download-url/5")) == 2
    assert len(site.seen("GET", "/csrf-token")) == 2


def test_mint_validates_download_id(env) -> None:
    _site, client = env
    with pytest.raises(ValueError):
        client.mint_download_ticket(0)


def test_mint_torrent_url(env) -> None:
    site, client = env
    site.route("POST", "/generate-torrent-url/232", Reply.json({"success": True, "torrent_url": "/torrents/hk.torrent"}))
    assert client.mint_torrent_url(232, referer_slug="hollow-knight") == site.url("/torrents/hk.torrent")
    site.route("POST", "/generate-torrent-url/233", Reply.json({"error": "Please log in to download torrents."}))
    with pytest.raises(AccessDeniedError):
        client.mint_torrent_url(233)


# --- ticket page & resolution ---------------------------------------------------------


def test_fetch_ticket_page(env) -> None:
    site, client = env
    site.route("GET", "/download/abc/def", html_route(fixture("ticket_page.html")))
    ticket = client.fetch_ticket_page(site.url("/download/abc/def"))
    assert ticket.ticket_url == site.url("/download/abc/def")
    assert ticket.file_url.endswith("/download-file/1893ac5d4b8106b7d614afe604fcd175ec630a3dde768f6e")
    assert ticket.requires_verification and ticket.wait_seconds == 5


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(403, "Invalid signature."), LinkExpiredError),
        (Reply(410, "gone"), LinkExpiredError),
        (Reply.redirect("/"), LinkExpiredError),
        (Reply(429, "slow", {"Retry-After": "45"}), RateLimitedError),
        (Reply(403, "<script>window._cf_chl_opt={}</script>"), NetworkError),
    ],
)
def test_fetch_ticket_page_errors(env, reply: Reply, error: type[Exception]) -> None:
    site, client = env
    site.route("GET", "/", html_route(GUEST_PAGE))
    site.route("GET", "/download/old/ticket", reply)
    with pytest.raises(error):
        client.fetch_ticket_page(site.url("/download/old/ticket"))


def _file_reply(size: int = 123_456_789, **headers: str) -> Reply:
    base = {
        "Content-Range": f"bytes 0-0/{size}",
        "Accept-Ranges": "bytes",
        "ETag": '"etag-1"',
        "Last-Modified": "Tue, 06 Oct 2026 10:00:00 GMT",
        "Content-Disposition": "attachment; filename*=UTF-8''Game%20Name%20%E2%84%A2.zip; filename=\"fallback.zip\"",
    }
    base.update(headers)
    return Reply(206, b"P", base, content_type="application/zip")


def test_resolve_file_url_with_and_without_verification(env) -> None:
    site, client = env

    def download_file(seen: Seen) -> Reply:
        if seen.query.get("cf-turnstile-response") == ["tok-123"]:
            return Reply.redirect("/cdn/Game%20Name.zip")
        return html_route("<html><div data-ag-turnstile></div>Complete the check</html>")

    site.route("GET", "/download-file/t1", download_file)
    site.route("GET", "/cdn/Game%20Name.zip", _file_reply())
    ticket_url = site.url("/download/a/b")

    with pytest.raises(VerificationError) as info:
        client.resolve_file_url(site.url("/download-file/t1"), ticket_url=ticket_url)
    assert info.value.ticket_url == ticket_url

    link = client.resolve_file_url(site.url("/download-file/t1"), verification_token="tok-123")
    assert link.url == site.url("/cdn/Game%20Name.zip")
    assert link.size == 123_456_789
    assert link.accept_ranges is True
    assert link.etag == '"etag-1"'
    assert link.last_modified == "Tue, 06 Oct 2026 10:00:00 GMT"
    assert link.filename == "Game Name ™.zip"
    assert link.content_type == "application/zip"
    request = site.seen("GET", "/download-file/t1")[-1]
    assert request.headers["range"] == "bytes=0-0"
    assert site.seen("GET", "/cdn/Game%20Name.zip")[0].headers["range"] == "bytes=0-0"


def test_turnstile_token_is_appended_without_touching_a_signed_query(env) -> None:
    site, client = env
    site.route("GET", "/download-file/t9", _file_reply())
    signed = site.url("/download-file/t9?expires=1700000000&flag&signature=a%2Bb%3D")
    client.resolve_file_url(signed, verification_token="0.tok+en/=")
    seen = site.seen("GET", "/download-file/t9")[0]
    assert seen.raw_query == "expires=1700000000&flag&signature=a%2Bb%3D&cf-turnstile-response=0.tok%2Ben%2F%3D"


def test_append_query_param_mirrors_the_page_script() -> None:
    assert _append_query_param("https://x/f", "k", "v w") == "https://x/f?k=v%20w"
    assert _append_query_param("https://x/f?a=1", "k", "v") == "https://x/f?a=1&k=v"
    assert _append_query_param("https://x/f?", "k", "v") == "https://x/f?k=v"
    assert _append_query_param("https://x/f?a=%2B#frag", "k", "(x)!") == "https://x/f?a=%2B&k=(x)!#frag"


def test_resolve_file_url_without_ticket_url_reports_file_url(env) -> None:
    site, client = env
    site.route("GET", "/download-file/t2", html_route("<html>challenge</html>"))
    with pytest.raises(VerificationError) as info:
        client.resolve_file_url(site.url("/download-file/t2"))
    assert info.value.ticket_url == site.url("/download-file/t2")


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply.json({"error": "Please complete the verification challenge and try again."}, 403), VerificationError),
        (Reply(410, "gone"), LinkExpiredError),
        (Reply(404, "missing"), LinkExpiredError),
        (Reply(200, "<html>This download link has expired.</html>"), LinkExpiredError),
        (Reply(429, "slow", {"Retry-After": "45"}), RateLimitedError),
    ],
)
def test_resolve_file_url_errors(env, reply: Reply, error: type[Exception]) -> None:
    site, client = env
    site.route("GET", "/download-file/t3", reply)
    with pytest.raises(error) as info:
        client.resolve_file_url(site.url("/download-file/t3"))
    if error is RateLimitedError:
        assert info.value.retry_after == 45


def test_probe_partial_content(env) -> None:
    site, client = env
    site.route("GET", "/files/a.zip", _file_reply(size=5000))
    link = client.probe(site.url("/files/a.zip"))
    assert (link.size, link.accept_ranges, link.filename) == (5000, True, "Game Name ™.zip")
    assert site.seen("GET", "/files/a.zip")[0].headers["range"] == "bytes=0-0"


def test_probe_full_response_and_url_filename(env) -> None:
    site, client = env
    site.route("GET", "/files/My%20Game.rar", Reply(200, b"x" * 50, {"Accept-Ranges": "bytes"}, content_type="application/x-rar"))
    site.route("GET", "/files/plain.bin", Reply(200, b"x" * 20, content_type="application/octet-stream"))
    link = client.probe(site.url("/files/My%20Game.rar"))
    assert (link.size, link.accept_ranges, link.filename) == (50, True, "My Game.rar")
    plain = client.probe(site.url("/files/plain.bin"))
    assert (plain.size, plain.accept_ranges, plain.etag) == (20, False, "")


def test_probe_unknown_total_size(env) -> None:
    site, client = env
    site.route("GET", "/files/stream.zip", _file_reply(**{"Content-Range": "bytes 0-0/*"}))
    assert client.probe(site.url("/files/stream.zip")).size is None


def test_probe_falls_back_to_head(env) -> None:
    site, client = env
    site.route(
        "GET",
        "/files/nohead.zip",
        lambda seen: Reply(405, "no ranges")
        if seen.method == "GET"
        else Reply(200, b"x" * 1000, {"Accept-Ranges": "bytes"}, content_type="application/zip"),
    )
    link = client.probe(site.url("/files/nohead.zip"))
    assert link.size == 1000 and link.accept_ranges
    assert [r.method for r in site.seen(path="/files/nohead.zip")] == ["GET", "HEAD"]


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_probe_expired_links(env, status: int) -> None:
    site, client = env
    site.route("GET", "/files/old.zip", Reply(status, "expired"))
    with pytest.raises(LinkExpiredError):
        client.probe(site.url("/files/old.zip"))


def test_probe_server_error(env) -> None:
    site, client = env
    site.route("GET", "/files/broken.zip", Reply(503, "down"))
    with pytest.raises(NetworkError):
        client.probe(site.url("/files/broken.zip"))


def test_content_disposition_parsing_and_sanitising() -> None:
    assert _disposition_filename("attachment; filename*=UTF-8''na%C3%AFve%20file.zip") == "naïve file.zip"
    assert _disposition_filename("attachment; filename*=iso-8859-1'en'caf%E9.rar") == "café.rar"
    assert _disposition_filename("attachment; filename*=bogus-charset''x%20y.zip") == "x y.zip"
    assert _disposition_filename('attachment; filename="quoted \\"name\\".7z"') == 'quoted _name_.7z'
    assert _disposition_filename("attachment; filename=plain.zip") == "plain.zip"
    assert _disposition_filename('attachment; filename="..\\..\\evil.exe"') == "evil.exe"
    assert _disposition_filename('attachment; filename="C:\\Games\\setup.zip"') == "setup.zip"
    assert _disposition_filename("attachment; filename*=UTF-8''..%2F..%2Fx.zip") == "x.zip"
    assert _disposition_filename("inline") == ""
    assert _safe_filename("a:b?.zip. ") == "a_b_.zip"
    assert _safe_filename("..") == ""


# --- account --------------------------------------------------------------------------


def _login_routes(site: LocalSite, post: Callable[[Seen], Reply] | list[Reply], error: str = "") -> None:
    page = fixture("login.html")
    if error:
        page = page.replace("</form>", LOGIN_ERROR.format(error) + "</form>", 1)
    site.route("GET", "/login", html_route(page))
    site.route("POST", "/login", post)


def test_login_success(env) -> None:
    site, client = env
    _login_routes(site, lambda seen: Reply.redirect("/", headers={"Set-Cookie": "is_logged_in=1; Path=/"}))
    site.route("GET", "/", html_route(SIGNED_IN_PAGE))
    user = client.login("user@example.com", "hunter2")
    assert user.display_name == "neo"
    assert user.email == "user@example.com"
    assert user.profile_url == "https://ankergames.net/profile/neo"  # parsers resolve against the site
    assert user.avatar_url == "https://ankergames.net/avatars/neo.png"
    form = site.seen("POST", "/login")[0].form
    assert form == {
        "_token": "lq9DmhqYzwTxh0bF9DL3nO9BVMItHFPLZP98SS6V",
        "email": "user@example.com",
        "password": "hunter2",
        "remember": "on",
    }
    assert site.seen("POST", "/login")[0].headers["referer"] == site.url("/login")
    assert client.http.cookie("is_logged_in") == "1"


def test_login_without_remember(env) -> None:
    site, client = env
    _login_routes(site, lambda seen: Reply.redirect("/"))
    site.route("GET", "/", html_route(SIGNED_IN_PAGE))
    client.login("user@example.com", "pw", remember=False)
    assert "remember" not in site.seen("POST", "/login")[0].form


def test_login_bad_credentials(env) -> None:
    site, client = env
    _login_routes(site, lambda seen: Reply.redirect("/login"), error="These credentials do not match our records.")
    with pytest.raises(LoginFailedError) as info:
        client.login("user@example.com", "wrong")
    assert info.value.user_message == "These credentials do not match our records."


def test_login_throttled(env) -> None:
    site, client = env
    _login_routes(
        site, lambda seen: Reply.redirect("/login"), error="Too many login attempts. Please try again in 37 seconds."
    )
    with pytest.raises(RateLimitedError) as info:
        client.login("user@example.com", "wrong")
    assert info.value.retry_after == 37


def test_login_retries_once_on_419(env) -> None:
    site, client = env
    replies = [Reply(419, "Page Expired"), Reply.redirect("/")]
    _login_routes(site, replies)
    site.route("GET", "/", html_route(SIGNED_IN_PAGE))
    assert client.login("user@example.com", "pw").display_name == "neo"
    assert len(site.seen("POST", "/login")) == 2
    assert len(site.seen("GET", "/login")) == 2


def test_login_without_account_markup_trusts_the_redirect(env) -> None:
    site, client = env
    _login_routes(site, lambda seen: Reply.redirect("/", headers={"Set-Cookie": "is_logged_in=1; Path=/"}))
    site.route("GET", "/", [html_route(GUEST_PAGE), html_route("<html><body>origin page, unknown markup</body></html>")])
    user = client.login("user@example.com", "pw")
    assert user.display_name == "user@example.com"
    assert user.email == "user@example.com"
    # The cached guest copy was retried with the site's cache-buster.
    assert site.seen("GET", "/")[-1].query == {"_auth": ["1"]}


def test_login_signs_out_an_existing_session_first(env) -> None:
    site, client = env
    gets = [Reply.redirect("/"), html_route(fixture("login.html"))]
    site.route("GET", "/login", gets)
    site.route("POST", "/login", Reply.redirect("/"))
    site.route("POST", "/logout", Reply.redirect("/"))
    site.route("GET", "/", html_route(SIGNED_IN_PAGE))
    client.login("user@example.com", "pw")
    assert len(site.seen("POST", "/logout")) == 1
    assert [r.method + " " + r.path for r in site.seen()][:4] == ["GET /login", "GET /", "GET /csrf-token", "POST /logout"]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply.redirect("/two-factor-challenge"), AuthError),
        (Reply.redirect("/verify-email"), AuthError),
        (Reply(403, "<title>Just a moment...</title>", {"cf-mitigated": "challenge"}), AuthError),
        (Reply(500, "boom"), NetworkError),
        (Reply(429, "slow", {"Retry-After": "30"}), RateLimitedError),
    ],
)
def test_login_other_outcomes(env, reply: Reply, error: type[Exception]) -> None:
    site, client = env
    _login_routes(site, lambda seen: reply)
    site.route("GET", "/two-factor-challenge", html_route("<html>2fa</html>"))
    site.route("GET", "/verify-email", html_route("<html>verify</html>"))
    with pytest.raises(error):
        client.login("user@example.com", "pw")


def test_login_requires_credentials(env) -> None:
    site, client = env
    with pytest.raises(LoginFailedError):
        client.login("  ", "pw")
    with pytest.raises(LoginFailedError):
        client.login("a@b.c", "")
    assert site.seen() == []


def test_logout_posts_token_and_clears_cookies(env) -> None:
    site, client = env
    site.route("POST", "/logout", Reply.redirect("/"))
    client.http.import_cookies([{"name": "ankergames_session", "value": "s", "domain": "127.0.0.1", "path": "/"}])
    client.logout()
    post = site.seen("POST", "/logout")[0]
    assert post.form == {"_token": "T1"}
    assert post.headers["x-csrf-token"] == "T1"
    assert post.cookies == {"ankergames_session": "s"}
    assert client.http.export_cookies() == []


def test_logout_clears_cookies_even_when_the_site_fails(env) -> None:
    site, client = env
    site.route("GET", "/csrf-token", Reply(500, "down"))
    client.http.import_cookies([{"name": "a", "value": "1", "domain": "127.0.0.1", "path": "/"}])
    client.logout()  # does not raise
    assert client.http.export_cookies() == []


def test_current_user_guest_and_signed_in(env) -> None:
    site, client = env
    site.route("GET", "/", [html_route(GUEST_PAGE), html_route(SIGNED_IN_PAGE)])
    assert client.current_user() is None
    user = client.current_user()
    assert user is not None and user.display_name == "neo"


def test_current_user_busts_the_cloudflare_cache_when_signed_in(env) -> None:
    site, client = env

    def home(seen: Seen) -> Reply:
        return html_route(SIGNED_IN_PAGE if seen.query.get("_auth") == ["1"] else GUEST_PAGE)

    site.route("GET", "/", home)
    client.http.import_cookies([{"name": "is_logged_in", "value": "1", "domain": "127.0.0.1", "path": "/"}])
    user = client.current_user()
    assert user is not None and user.display_name == "neo"
    assert [r.query for r in site.seen("GET", "/")] == [{}, {"_auth": ["1"]}]


def test_current_user_with_stale_cookie_is_signed_out(env) -> None:
    site, client = env
    site.route("GET", "/", html_route(GUEST_PAGE))
    client.http.import_cookies([{"name": "is_logged_in", "value": "1", "domain": "127.0.0.1", "path": "/"}])
    assert client.current_user() is None
    assert len(site.seen("GET", "/")) == 2


def test_http_property_exposes_the_shared_client(env) -> None:
    _site, client = env
    assert isinstance(client.http, HttpClient)
    assert json.loads(json.dumps(client.http.export_cookies())) == []

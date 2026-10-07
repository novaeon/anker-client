"""HttpClient against a local ThreadingHTTPServer: retries, errors, cancellation, cookies, pacing."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from anker_client.core.errors import NetworkError, NotFoundError, OperationCancelled, SiteChangedError
from anker_client.core.tasks import CancelToken
from anker_client.site._common import decode_text, is_cloudflare_challenge, retry_after_seconds
from anker_client.site.http import HttpClient, _TokenBucket
from tests.unit.test_site_server import LocalSite, Reply, Seen, serve_local_site


@pytest.fixture
def local_site() -> Iterator[LocalSite]:
    yield from serve_local_site()


@pytest.fixture
def http():
    client = HttpClient(backoff_base=0.01)
    yield client
    client.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- headers & basics -----------------------------------------------------------------


def test_default_headers_are_browser_like_without_brotli(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/", Reply(200, "ok"))
    http.get(local_site.url("/")).close()
    headers = local_site.seen("GET", "/")[0].headers
    assert headers["accept-encoding"] == "gzip, deflate"
    assert "br" not in headers["accept-encoding"]
    assert headers["accept-language"].startswith("en-US")
    assert "text/html" in headers["accept"]
    assert headers["user-agent"] == http.user_agent
    assert "Chrome/" in http.user_agent


def test_user_agent_can_be_replaced_at_runtime(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/", Reply(200, "ok"))
    http.get(local_site.url("/")).close()
    http.set_user_agent("Mozilla/5.0 TestBrowser/1.0")
    http.set_user_agent("   ")  # ignored
    http.get(local_site.url("/")).close()
    agents = [r.headers["user-agent"] for r in local_site.seen("GET", "/")]
    assert agents[1] == "Mozilla/5.0 TestBrowser/1.0"
    assert agents[0] != agents[1]
    assert http.user_agent == "Mozilla/5.0 TestBrowser/1.0"


def test_custom_headers_and_params_are_sent(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/q", Reply(200, "ok"))
    http.get(local_site.url("/q"), params={"page": 2, "sort": "title"}, headers={"X-Test": "1"}).close()
    seen = local_site.seen("GET", "/q")[0]
    assert seen.query == {"page": ["2"], "sort": ["title"]}
    assert seen.headers["x-test"] == "1"


def test_get_text_falls_back_to_utf8_and_honours_declared_charset(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/utf8", Reply(200, "Café ☕".encode(), content_type="text/html"))
    local_site.route("GET", "/latin", Reply(200, "Café".encode("latin-1"), content_type="text/html; charset=ISO-8859-1"))
    assert http.get_text(local_site.url("/utf8")) == "Café ☕"
    assert http.get_text(local_site.url("/latin")) == "Café"


def test_get_json_and_malformed_json(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/ok", Reply.json({"token": "abc"}))
    local_site.route("GET", "/bad", Reply(200, "<html>nope</html>"))
    assert http.get_json(local_site.url("/ok")) == {"token": "abc"}
    assert "application/json" in local_site.seen("GET", "/ok")[0].headers["accept"]
    with pytest.raises(SiteChangedError):
        http.get_json(local_site.url("/bad"))


def test_head_follows_redirects(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/old", Reply.redirect("/new"))
    local_site.route("GET", "/new", Reply(200, "x" * 10))
    response = http.head(local_site.url("/old"))
    response.close()
    assert response.url.endswith("/new")
    assert [r.method for r in local_site.seen()] == ["HEAD", "HEAD"]


def test_stream_response_is_left_open_for_the_caller(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/file", Reply(200, b"0123456789" * 1000, content_type="application/octet-stream"))
    response = http.get(local_site.url("/file"), stream=True)
    try:
        assert response.raw.read(10) == b"0123456789"
    finally:
        response.close()


# --- retries --------------------------------------------------------------------------


def test_retries_503_honouring_retry_after(local_site: LocalSite) -> None:
    http = HttpClient(backoff_base=5.0)  # a large backoff proves Retry-After is what we waited for
    local_site.route("GET", "/busy", [Reply(503, "busy", {"Retry-After": "1"}), Reply(200, "ok")])
    started = time.monotonic()
    try:
        assert http.get_text(local_site.url("/busy")) == "ok"
    finally:
        http.close()
    elapsed = time.monotonic() - started
    assert len(local_site.seen("GET", "/busy")) == 2
    assert 0.9 <= elapsed < 4


def test_retries_connection_level_statuses_then_maps_error(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/down", Reply(502, "bad gateway"))
    with pytest.raises(NetworkError) as info:
        http.get(local_site.url("/down"))
    assert info.value.status == 502
    assert len(local_site.seen("GET", "/down")) == 3  # max attempts


def test_no_raise_returns_last_error_response(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/down", Reply(500, "boom"))
    response = http.get(local_site.url("/down"), raise_for_status=False)
    response.close()
    assert response.status_code == 500
    assert len(local_site.seen("GET", "/down")) == 3


def test_retry_false_makes_a_single_attempt(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/down", Reply(503, "busy"))
    with pytest.raises(NetworkError):
        http.get(local_site.url("/down"), retry=False)
    assert len(local_site.seen("GET", "/down")) == 1


def test_retry_after_longer_than_limit_is_not_waited_for(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/later", Reply(429, "slow down", {"Retry-After": "3600"}))
    started = time.monotonic()
    with pytest.raises(NetworkError) as info:
        http.get(local_site.url("/later"))
    assert time.monotonic() - started < 2
    assert info.value.status == 429
    assert len(local_site.seen("GET", "/later")) == 1


def test_post_is_never_retried(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("POST", "/submit", Reply(503, "busy", {"Retry-After": "0"}))
    response = http.post(local_site.url("/submit"), json={"a": 1}, raise_for_status=False)
    response.close()
    assert response.status_code == 503
    assert len(local_site.seen("POST", "/submit")) == 1
    assert local_site.seen("POST", "/submit")[0].json == {"a": 1}


# --- error mapping --------------------------------------------------------------------


def test_404_maps_to_not_found(local_site: LocalSite, http: HttpClient) -> None:
    with pytest.raises(NotFoundError):
        http.get(local_site.url("/nothing-here"))
    assert len(local_site.seen()) == 1  # 404 is not retried


def test_403_maps_to_network_error_with_status(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/secret", Reply(403, "no"))
    with pytest.raises(NetworkError) as info:
        http.get(local_site.url("/secret"))
    assert info.value.status == 403


def test_cloudflare_challenge_gets_a_specific_message(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/cf", Reply(403, "<title>Just a moment...</title>", {"cf-mitigated": "challenge"}))
    with pytest.raises(NetworkError) as info:
        http.get(local_site.url("/cf"))
    assert "browser check" in info.value.user_message


def test_connection_refused_maps_to_network_error() -> None:
    # Windows retries refused SYNs for ~2 s per attempt; one attempt keeps the test fast.
    http = HttpClient(max_attempts=1)
    try:
        with pytest.raises(NetworkError) as info:
            http.get(f"http://127.0.0.1:{_free_port()}/", timeout=(3, 3))
    finally:
        http.close()
    assert info.value.status is None
    assert info.value.retryable


def test_read_timeout_maps_to_network_error(local_site: LocalSite) -> None:
    http = HttpClient(max_attempts=1)
    local_site.route("GET", "/slow", Reply(200, "late", delay=1.5))
    try:
        with pytest.raises(NetworkError) as info:
            http.get(local_site.url("/slow"), timeout=(2, 0.3))
    finally:
        http.close()
    assert "in time" in info.value.user_message


# --- cancellation ---------------------------------------------------------------------


def test_cancellation_mid_request_is_immediate(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/slow", Reply(200, "late", delay=2.0))
    token = CancelToken()
    threading.Timer(0.2, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelled):
        http.get(local_site.url("/slow"), token=token)
    assert time.monotonic() - started < 1.0


def test_already_cancelled_token_sends_nothing(local_site: LocalSite, http: HttpClient) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        http.get(local_site.url("/"), token=token)
    assert local_site.seen() == []


def test_cancellation_during_retry_wait(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/busy", Reply(503, "busy", {"Retry-After": "10"}))
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelled):
        http.get(local_site.url("/busy"), token=token)
    assert time.monotonic() - started < 2


def test_token_callbacks_are_unregistered_after_requests(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/", Reply(200, "ok"))
    token = CancelToken()
    for _ in range(5):
        http.get(local_site.url("/"), token=token).close()
    assert token._callbacks == []


def test_requests_after_close_are_refused(local_site: LocalSite) -> None:
    http = HttpClient()
    http.close()
    with pytest.raises(OperationCancelled):
        http.get(local_site.url("/"))


# --- cookies --------------------------------------------------------------------------


def _set_cookie_route(local_site: LocalSite) -> None:
    def set_cookie(seen: Seen) -> Reply:
        name, value = seen.query["name"][0], seen.query["value"][0]
        return Reply(200, "set", {"Set-Cookie": f"{name}={value}; Path=/"})

    local_site.route("GET", "/set", set_cookie)
    local_site.route("GET", "/echo", lambda seen: Reply.json(seen.cookies))


def test_cookies_learned_on_a_worker_thread_are_visible_everywhere(local_site: LocalSite, http: HttpClient) -> None:
    _set_cookie_route(local_site)
    worker = threading.Thread(target=lambda: http.get(local_site.url("/set?name=sid&value=abc")).close())
    worker.start()
    worker.join(10)
    assert http.cookie("sid") == "abc"
    assert http.get_json(local_site.url("/echo")) == {"sid": "abc"}


def test_concurrent_cookie_learning_merges_everything(local_site: LocalSite, http: HttpClient) -> None:
    _set_cookie_route(local_site)
    errors: list[BaseException] = []

    def fetch(i: int) -> None:
        try:
            http.get(local_site.url(f"/set?name=c{i}&value=v{i}"), token=CancelToken()).close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=fetch, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not errors
    assert {f"c{i}": f"v{i}" for i in range(16)} == http.get_json(local_site.url("/echo"))


def test_server_side_cookie_deletion_propagates(local_site: LocalSite, http: HttpClient) -> None:
    _set_cookie_route(local_site)
    local_site.route(
        "GET", "/forget", Reply(200, "bye", {"Set-Cookie": "sid=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"})
    )
    http.get(local_site.url("/set?name=sid&value=abc")).close()
    http.get(local_site.url("/forget")).close()
    assert http.cookie("sid") is None
    assert http.get_json(local_site.url("/echo")) == {}


def test_export_import_and_clear_cookies(local_site: LocalSite, http: HttpClient) -> None:
    _set_cookie_route(local_site)
    http.get(local_site.url("/set?name=sid&value=abc")).close()
    exported = http.export_cookies()
    assert exported == [
        {"name": "sid", "value": "abc", "domain": "127.0.0.1", "path": "/", "secure": False, "expires": None}
    ]

    http.clear_cookies()
    assert http.export_cookies() == []
    assert http.get_json(local_site.url("/echo")) == {}

    future = int(time.time()) + 3600
    http.import_cookies(
        [
            *exported,
            {"name": "remember", "value": "r1", "domain": "127.0.0.1", "path": "/", "secure": False, "expires": future},
            {"name": "old", "value": "x", "domain": "127.0.0.1", "path": "/", "expires": 1000},  # expired
            {"name": "nodomain", "value": "x"},  # would leak to every host
            {"value": "no name", "domain": "127.0.0.1"},
        ]
    )
    assert http.get_json(local_site.url("/echo")) == {"sid": "abc", "remember": "r1"}
    assert http.cookie("remember", "127.0.0.1") == "r1"
    assert http.cookie("remember", "example.com") is None


def test_imported_site_cookies_are_not_sent_to_other_hosts(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/echo", lambda seen: Reply.json(seen.cookies))
    http.import_cookies([{"name": "ankergames_session", "value": "s", "domain": ".ankergames.net", "path": "/"}])
    assert http.get_json(local_site.url("/echo")) == {}
    assert http.cookie("ankergames_session", "ankergames.net") == "s"


def test_cookie_changes_reach_idle_worker_sessions(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/echo", lambda seen: Reply.json(seen.cookies))
    assert http.get_json(local_site.url("/echo")) == {}  # creates & pools a worker session
    http.import_cookies([{"name": "a", "value": "1", "domain": "127.0.0.1", "path": "/"}])
    assert http.get_json(local_site.url("/echo")) == {"a": "1"}
    http.clear_cookies()
    assert http.get_json(local_site.url("/echo")) == {}


# --- pacing ---------------------------------------------------------------------------


def test_site_hosts_are_paced_and_others_are_not() -> None:
    http = HttpClient()
    assert http._is_paced("https://ankergames.net/games")
    assert http._is_paced("https://www.ankergames.net/")
    assert http._is_paced("https://ANKERGAMES.NET/uploads/x.jpg")
    assert not http._is_paced("https://tunnel3.dlproxy.uk/file.zip")
    assert not http._is_paced("https://notankergames.net/")
    assert not http._is_paced("not a url")
    assert HttpClient(paced_hosts={"127.0.0.1"})._is_paced("http://127.0.0.1:8080/")


def test_pacing_limits_request_rate_for_paced_hosts(local_site: LocalSite) -> None:
    local_site.route("GET", "/", Reply(200, "ok"))
    paced = HttpClient(site_rate_per_second=10, paced_hosts={"127.0.0.1"})
    unpaced = HttpClient(site_rate_per_second=10)  # default: only ankergames.net is paced
    try:
        started = time.monotonic()
        for _ in range(20):
            unpaced.get(local_site.url("/")).close()
        unpaced_elapsed = time.monotonic() - started

        started = time.monotonic()
        for _ in range(20):
            paced.get(local_site.url("/")).close()
        paced_elapsed = time.monotonic() - started
    finally:
        paced.close()
        unpaced.close()
    # 10 burst tokens, then 10 more at 0.1 s intervals.
    assert paced_elapsed >= 0.85
    assert unpaced_elapsed < paced_elapsed


def test_static_assets_use_their_own_faster_bucket(local_site: LocalSite) -> None:
    local_site.route("GET", "/uploads/poster/a.jpg", Reply(200, b"img", content_type="image/jpeg"))
    local_site.route("GET", "/page", Reply(200, "ok"))
    http = HttpClient(site_rate_per_second=2, asset_rate_per_second=1000, paced_hosts={"127.0.0.1"})
    try:
        started = time.monotonic()
        for _ in range(20):
            http.get(local_site.url("/uploads/poster/a.jpg")).close()
        assets_elapsed = time.monotonic() - started
        started = time.monotonic()
        for _ in range(3):  # 2 burst + 1 at 0.5 s
            http.get(local_site.url("/page")).close()
        pages_elapsed = time.monotonic() - started
    finally:
        http.close()
    assert pages_elapsed >= 0.4
    assert assets_elapsed < 3.0  # through the 2/s page bucket these 20 requests would take ~9 s


def test_token_bucket_reservations_queue_up() -> None:
    now = [100.0]
    bucket = _TokenBucket(2.0, clock=lambda: now[0])
    assert [bucket.reserve() for _ in range(2)] == [0.0, 0.0]  # burst = rate
    assert bucket.reserve() == pytest.approx(0.5)
    assert bucket.reserve() == pytest.approx(1.0)
    now[0] += 10
    assert bucket.reserve() == 0.0
    assert _TokenBucket(0, clock=lambda: 0.0).reserve() == 0.0


# --- shared helpers ---------------------------------------------------------------------


def test_retry_after_parsing() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert retry_after_seconds({"Retry-After": "42"}) == 42
    assert retry_after_seconds({"Retry-After": format_datetime(now + timedelta(seconds=30), usegmt=True)}, now=now) == 30
    assert retry_after_seconds({"Retry-After": format_datetime(now - timedelta(seconds=30), usegmt=True)}, now=now) == 0
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({}) is None


def test_cloudflare_challenge_detection() -> None:
    assert is_cloudflare_challenge(403, {"cf-mitigated": "challenge"})
    assert is_cloudflare_challenge(503, {}, "<html><title>Just a moment...</title>")
    assert is_cloudflare_challenge(403, {}, "<script>window._cf_chl_opt={}</script>")
    # Cloudflare injects /cdn-cgi/challenge-platform/ into ordinary pages — not a challenge.
    assert not is_cloudflare_challenge(403, {}, '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">')
    assert not is_cloudflare_challenge(200, {}, "<title>Just a moment</title>")


def test_decode_text_unknown_charset_falls_back(local_site: LocalSite, http: HttpClient) -> None:
    local_site.route("GET", "/odd", Reply(200, "héllo".encode(), content_type="text/html; charset=x-unknown"))
    response = http.get(local_site.url("/odd"))
    try:
        assert decode_text(response) == "héllo"
    finally:
        response.close()


# --- review regressions -----------------------------------------------------------------


def test_empty_paced_host_set_disables_pacing() -> None:
    assert not HttpClient(paced_hosts=set())._is_paced("https://ankergames.net/games")
    assert HttpClient(paced_hosts=None)._is_paced("https://ankergames.net/games")
    assert HttpClient(paced_hosts=[" .Example.COM "])._is_paced("https://cdn.example.com/x")


def test_token_cancelled_after_pacing_never_sends_the_request(
    local_site: LocalSite, http: HttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The window between the pre-checks and the helper thread: a POST (which may
    # consume a download quota) must not leave the machine once cancelled.
    local_site.route("POST", "/generate-download-url/1", Reply.json({"success": True}))
    token = CancelToken()
    monkeypatch.setattr(http, "_pace", lambda url, tok: token.cancel())
    with pytest.raises(OperationCancelled):
        http.post(local_site.url("/generate-download-url/1"), json={}, token=token)
    time.sleep(0.2)  # a wrongly started helper thread would have reached the server by now
    assert local_site.seen() == []
    assert token._callbacks == []


def test_json_with_a_byte_order_mark_is_decoded(local_site: LocalSite, http: HttpClient) -> None:
    body = "﻿" + '{"token": "abc"}'
    local_site.route("GET", "/bom", Reply(200, body.encode("utf-8"), content_type="application/json"))
    local_site.route("GET", "/bom8", Reply(200, body.encode("utf-8"), content_type="application/json; charset=utf-8"))
    assert http.get_json(local_site.url("/bom")) == {"token": "abc"}
    assert http.get_json(local_site.url("/bom8")) == {"token": "abc"}

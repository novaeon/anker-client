"""LinkResolver: ticket → (countdown | browser verification) → final ResolvedLink."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from anker_client.core.errors import (
    ExternalHostError,
    OperationCancelled,
    RateLimitedError,
    SiteChangedError,
    VerificationCancelled,
    VerificationError,
    VerificationTimeout,
    VerificationUnavailable,
)
from anker_client.core.models import DownloadOption, JobState, ResolvedLink, TicketPage
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads.resolver import (
    MAX_WAIT_SECONDS,
    LinkResolver,
    VerificationRequest,
    VerificationResult,
    merge_verification_result,
)

TICKET = "https://ankergames.net/download/signed/hash"
FILE_URL = "https://ankergames.net/download-file/ticket123"
CDN = "https://tunnel1.dlproxy.example/files/game.zip?sig=abc"
OPTION = DownloadOption(4242, "Direct")


class RecordingToken(CancelToken):
    """Records ``sleep`` calls instead of sleeping."""

    def __init__(self) -> None:
        super().__init__()
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        super().sleep(0)


class FakeClient:
    def __init__(self, page: TicketPage | None = None) -> None:
        self.page = page or TicketPage(ticket_url=TICKET, file_url=FILE_URL)
        self.calls: list[tuple[str, Any]] = []
        self.mint_error: BaseException | None = None
        self.resolve_results: list[Any] = []
        self.direct_link = ResolvedLink(url=CDN, filename="game.zip", size=1000, accept_ranges=True, etag='"e"')
        self.probe_link = ResolvedLink(url=CDN, filename="from-url.zip", size=2048, accept_ranges=True,
                                       etag='"p"', last_modified="Mon", content_type="")

    def mint_download_ticket(self, download_id: int, *, referer_slug: str = "", token: CancelToken | None = None) -> str:
        self.calls.append(("mint", (download_id, referer_slug)))
        if self.mint_error is not None:
            raise self.mint_error
        return TICKET

    def fetch_ticket_page(self, ticket_url: str, *, token: CancelToken | None = None) -> TicketPage:
        self.calls.append(("page", ticket_url))
        return self.page

    def resolve_file_url(self, file_url: str, *, verification_token: str = "",
                         token: CancelToken | None = None) -> ResolvedLink:
        self.calls.append(("resolve", file_url))
        item = self.resolve_results.pop(0) if self.resolve_results else self.direct_link
        if isinstance(item, BaseException):
            raise item
        return item

    def probe(self, url: str, *, token: CancelToken | None = None) -> ResolvedLink:
        self.calls.append(("probe", url))
        return self.probe_link

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeVerifier:
    def __init__(self, result: VerificationResult | None = None, *, available: bool = True,
                 error: BaseException | None = None) -> None:
        self.result = result or VerificationResult(url=CDN, filename="Real Name.zip", size=4096,
                                                   mime_type="application/zip")
        self._available = available
        self.error = error
        self.requests: list[VerificationRequest] = []

    @property
    def available(self) -> bool:
        return self._available

    def verify(self, request: VerificationRequest, *, token: CancelToken) -> VerificationResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def _resolve(resolver: LinkResolver, token: CancelToken | None = None, states: list | None = None) -> ResolvedLink:
    return resolver.resolve(
        OPTION,
        slug="hollow-knight",
        title="Hollow Knight",
        job_id="job1",
        token=token or RecordingToken(),
        on_state=(lambda s, t: states.append((s, t))) if states is not None else None,
    )


# --- direct (no verification) -----------------------------------------------------------------


def test_direct_path_respects_countdown_and_resolves_file_url() -> None:
    client = FakeClient(TicketPage(ticket_url=TICKET, file_url=FILE_URL, wait_seconds=12))
    token = RecordingToken()
    states: list = []
    link = _resolve(LinkResolver(client), token, states)
    assert link == client.direct_link
    assert token.slept == [12]
    assert client.calls[0] == ("mint", (4242, "hollow-knight"))
    assert client.calls[1] == ("page", TICKET)
    assert client.calls[2] == ("resolve", FILE_URL)
    assert all(state is JobState.RESOLVING for state, _ in states)
    assert any("Waiting 12s" in text for _, text in states)


def test_direct_path_without_countdown_does_not_sleep() -> None:
    token = RecordingToken()
    _resolve(LinkResolver(FakeClient()), token)
    assert token.slept == []


def test_countdown_is_capped() -> None:
    token = RecordingToken()
    client = FakeClient(TicketPage(ticket_url=TICKET, file_url=FILE_URL, wait_seconds=10**7))
    _resolve(LinkResolver(client), token)
    assert token.slept == [MAX_WAIT_SECONDS]


def test_cancel_during_countdown_is_prompt() -> None:
    client = FakeClient(TicketPage(ticket_url=TICKET, file_url=FILE_URL, wait_seconds=30))
    token = CancelToken()
    threading.Timer(0.05, token.cancel, args=("pause",)).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelled):
        _resolve(LinkResolver(client), token)
    assert time.monotonic() - started < 2
    assert "resolve" not in client.names()


def test_already_cancelled_token_does_nothing() -> None:
    client = FakeClient()
    token = CancelToken()
    token.cancel("cancel")
    with pytest.raises(OperationCancelled):
        _resolve(LinkResolver(client), token)
    assert client.calls == []


def test_mint_errors_propagate() -> None:
    client = FakeClient()
    client.mint_error = RateLimitedError(42)
    with pytest.raises(RateLimitedError):
        _resolve(LinkResolver(client))


def test_external_provider_on_ticket_page() -> None:
    client = FakeClient(TicketPage(ticket_url=TICKET, file_url="", external_provider="MegaUp"))
    with pytest.raises(ExternalHostError) as info:
        _resolve(LinkResolver(client, FakeVerifier()))
    assert info.value.url == TICKET and info.value.provider == "MegaUp"
    assert "resolve" not in client.names()


def test_server_demanding_challenge_falls_back_to_browser() -> None:
    client = FakeClient()
    client.resolve_results = [VerificationError(ticket_url=FILE_URL)]
    verifier = FakeVerifier()
    link = _resolve(LinkResolver(client, verifier))
    assert verifier.requests[0].ticket_url == TICKET
    assert link.filename == "Real Name.zip"


def test_server_demanding_challenge_without_browser() -> None:
    client = FakeClient()
    client.resolve_results = [VerificationError(ticket_url=FILE_URL)]
    with pytest.raises(VerificationUnavailable) as info:
        _resolve(LinkResolver(client))
    assert info.value.ticket_url == TICKET


def test_empty_url_means_site_changed() -> None:
    client = FakeClient()
    client.direct_link = ResolvedLink(url="")
    with pytest.raises(SiteChangedError):
        _resolve(LinkResolver(client))


# --- verification ---------------------------------------------------------------------------------


def _verified_client() -> FakeClient:
    return FakeClient(TicketPage(ticket_url=TICKET, file_url=FILE_URL, requires_verification=True,
                                 turnstile_sitekey="0x4AAA", wait_seconds=10))


def test_verification_path_uses_verifier_and_probe() -> None:
    client = _verified_client()
    verifier = FakeVerifier()
    token = RecordingToken()
    states: list = []
    resolver = LinkResolver(client, verifier, verification_timeout=lambda: 240.0)
    link = _resolve(resolver, token, states)

    assert verifier.requests == [VerificationRequest(ticket_url=TICKET, title="Hollow Knight", job_id="job1",
                                                     timeout_seconds=240.0)]
    assert ("probe", CDN) in client.calls and "resolve" not in client.names()
    assert token.slept == []  # the browser page runs its own countdown
    assert link.url == CDN
    assert link.filename == "Real Name.zip"  # browser's name wins
    assert link.size == 2048  # probe's exact size wins
    assert link.etag == '"p"' and link.accept_ranges
    assert link.content_type == "application/zip"
    assert (JobState.VERIFYING, "Waiting for browser verification…") in states
    assert states[-1][0] is JobState.RESOLVING


def test_merge_falls_back_to_browser_values() -> None:
    probed = ResolvedLink(url="", filename="url.zip", size=None, content_type="")
    result = VerificationResult(url=CDN, filename="", size=77, mime_type="application/x-7z-compressed")
    merged = merge_verification_result(probed, result)
    assert merged.url == CDN and merged.filename == "url.zip" and merged.size == 77
    assert merged.content_type == "application/x-7z-compressed"


def test_no_verifier_means_unavailable() -> None:
    with pytest.raises(VerificationUnavailable) as info:
        _resolve(LinkResolver(_verified_client()))
    assert info.value.ticket_url == TICKET


def test_unavailable_verifier_means_unavailable() -> None:
    verifier = FakeVerifier(available=False)
    with pytest.raises(VerificationUnavailable):
        _resolve(LinkResolver(_verified_client(), verifier))
    assert verifier.requests == []


def test_set_verifier_at_runtime() -> None:
    resolver = LinkResolver(_verified_client())
    assert resolver.verifier is None
    verifier = FakeVerifier()
    resolver.set_verifier(verifier)
    assert resolver.verifier is verifier
    _resolve(resolver)
    resolver.set_verifier(None)
    with pytest.raises(VerificationUnavailable):
        _resolve(resolver)


def test_browser_external_handoff() -> None:
    verifier = FakeVerifier(VerificationResult(external_url="https://mega.example/file/xyz"))
    with pytest.raises(ExternalHostError) as info:
        _resolve(LinkResolver(_verified_client(), verifier))
    assert info.value.url == "https://mega.example/file/xyz"
    assert info.value.provider == "mega.example"


def test_browser_without_url_is_verification_error() -> None:
    verifier = FakeVerifier(VerificationResult())
    with pytest.raises(VerificationError) as info:
        _resolve(LinkResolver(_verified_client(), verifier))
    assert info.value.ticket_url == TICKET


@pytest.mark.parametrize("error", [VerificationTimeout(), VerificationCancelled()])
def test_verifier_errors_get_the_ticket_url(error: VerificationError) -> None:
    with pytest.raises(type(error)) as info:
        _resolve(LinkResolver(_verified_client(), FakeVerifier(error=error)))
    assert info.value.ticket_url == TICKET


def test_verifier_cancellation_propagates() -> None:
    with pytest.raises(OperationCancelled):
        _resolve(LinkResolver(_verified_client(), FakeVerifier(error=OperationCancelled())))


def test_unexpected_verifier_crash_is_wrapped() -> None:
    with pytest.raises(VerificationError) as info:
        _resolve(LinkResolver(_verified_client(), FakeVerifier(error=RuntimeError("qt died"))))
    assert info.value.ticket_url == TICKET
    assert "qt died" in info.value.detail


def test_broken_timeout_callable_uses_default() -> None:
    def broken() -> float:
        raise ValueError("bad setting")

    verifier = FakeVerifier()
    _resolve(LinkResolver(_verified_client(), verifier, verification_timeout=broken))
    assert verifier.requests[0].timeout_seconds == 180.0


def test_on_state_errors_do_not_break_resolution() -> None:
    def explode(state: JobState, text: str) -> None:
        raise RuntimeError("ui gone")

    link = LinkResolver(FakeClient()).resolve(
        OPTION, slug="s", title="T", token=RecordingToken(), on_state=explode
    )
    assert link.url == CDN


# --- review regressions ------------------------------------------------------------------------


def test_probe_size_zero_falls_back_to_browser_size() -> None:
    probed = ResolvedLink(url=CDN, filename="x.zip", size=0)
    merged = merge_verification_result(probed, VerificationResult(url=CDN, size=4096))
    assert merged.size == 4096
    assert merge_verification_result(probed, VerificationResult(url=CDN, size=0)).size is None


def test_verifier_returning_nothing_is_a_verification_error() -> None:
    verifier = FakeVerifier()
    verifier.result = None  # type: ignore[assignment]
    with pytest.raises(VerificationError) as info:
        _resolve(LinkResolver(_verified_client(), verifier))
    assert info.value.ticket_url == TICKET


@pytest.mark.parametrize("url", [TICKET, "https://ankergames.net/game/hollow-knight", "https://cdn.ankergames.net/x"])
def test_open_in_browser_instead_is_not_called_an_external_host(url: str) -> None:
    verifier = FakeVerifier(VerificationResult(external_url=url))
    with pytest.raises(ExternalHostError) as info:
        _resolve(LinkResolver(_verified_client(), verifier))
    assert info.value.url == url
    assert "ankergames.net" not in info.value.user_message
    assert "browser" in info.value.user_message and "import" in info.value.user_message


def test_lookalike_host_is_still_external() -> None:
    verifier = FakeVerifier(VerificationResult(external_url="https://evilankergames.net/f"))
    with pytest.raises(ExternalHostError) as info:
        _resolve(LinkResolver(_verified_client(), verifier))
    assert info.value.provider == "evilankergames.net"


def test_ticket_page_without_file_url_is_site_changed() -> None:
    client = FakeClient(TicketPage(ticket_url=TICKET, file_url=""))
    with pytest.raises(SiteChangedError):
        _resolve(LinkResolver(client))
    assert "resolve" not in client.names()


def test_cancel_during_verification_propagates_without_probe() -> None:
    token = CancelToken()

    class CancellingVerifier(FakeVerifier):
        def verify(self, request: VerificationRequest, *, token: CancelToken) -> VerificationResult:
            token.cancel("pause")
            return super().verify(request, token=token)

    client = _verified_client()
    with pytest.raises(OperationCancelled):
        _resolve(LinkResolver(client, CancellingVerifier()), token)
    assert "probe" not in client.names()


def test_verifier_swapped_while_resolving_is_used_next_time() -> None:
    resolver = LinkResolver(_verified_client(), FakeVerifier(available=False))
    with pytest.raises(VerificationUnavailable):
        _resolve(resolver)
    good = FakeVerifier()
    threads = [threading.Thread(target=resolver.set_verifier, args=(good,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _resolve(resolver).url == CDN


def test_ticket_page_is_passed_as_referer_when_the_client_supports_it() -> None:
    class RefererClient(FakeClient):
        def resolve_file_url(self, file_url: str, *, verification_token: str = "",
                             token: CancelToken | None = None, ticket_url: str = "") -> ResolvedLink:
            self.calls.append(("referer", ticket_url))
            return super().resolve_file_url(file_url, verification_token=verification_token, token=token)

    client = RefererClient()
    _resolve(LinkResolver(client))
    assert ("referer", TICKET) in client.calls


def test_real_site_client_signature_takes_ticket_url() -> None:
    from anker_client.services.downloads.resolver import _accepts_keyword
    from anker_client.site.client import AnkerGamesClient

    assert _accepts_keyword(AnkerGamesClient.resolve_file_url, "ticket_url")
    assert not _accepts_keyword(FakeClient().resolve_file_url, "ticket_url")

"""Resume decisions: which saved state may be continued, and which validators ``If-Range`` carries.

Regression focus: a resume must never splice bytes of a changed file onto
the partial data of the old one. ``If-Range`` therefore has to carry the
validators saved with the partial data, and a Last-Modified change must block
the resume whenever no strong ETag settles the question.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anker_client.core.models import ResolvedLink
from anker_client.core.tasks import CancelToken
from anker_client.services.downloads import _engine_sidecar as sidecars
from anker_client.services.downloads._engine_http import same_last_modified, validator_conflict
from anker_client.services.downloads.engine import HttpDownloader, _resume_blocker, _resume_validators
from anker_client.services.downloads.ratelimit import RateLimiter
from tests.unit.test_engine_support import (
    KIB,
    MIB,
    FileServer,
    SessionHttp,
    file_sha256,
    make_downloader,
    make_partial,
    part_files,
    random_bytes,
    sha256,
)

OLD_DATE = "Tue, 06 Oct 2026 10:00:00 GMT"
NEW_DATE = "Wed, 07 Oct 2026 10:00:00 GMT"


@pytest.fixture
def http():
    client = SessionHttp()
    yield client
    client.close()


@pytest.fixture
def make_server():
    servers: list[FileServer] = []

    def make(data: bytes, **kwargs: Any) -> FileServer:
        server = FileServer(data, **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


# --- validator rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved", "new", "conflict"),
    [
        (('"a"', OLD_DATE), ('"a"', OLD_DATE), ""),
        (('"a"', OLD_DATE), ('"a"', NEW_DATE), ""),  # a matching strong ETag is authoritative
        (('"a"', OLD_DATE), ('"b"', OLD_DATE), "ETag"),
        (('W/"a"', OLD_DATE), ('W/"a"', NEW_DATE), "Last-Modified"),  # weak ETags do not vouch for bytes
        (('W/"a"', OLD_DATE), ('"a"', OLD_DATE), ""),
        (("", OLD_DATE), ("", NEW_DATE), "Last-Modified"),
        (("", OLD_DATE), ('"b"', NEW_DATE), "Last-Modified"),  # ETag only on one side: dates decide
        (('"a"', OLD_DATE), ("", NEW_DATE), "Last-Modified"),
        (("", ""), ('"b"', NEW_DATE), ""),  # nothing saved to compare with
        (("", OLD_DATE), ("", ""), ""),
    ],
)
def test_validator_conflict(saved: tuple[str, str], new: tuple[str, str], conflict: str) -> None:
    assert validator_conflict(*saved, *new) == conflict


def test_same_last_modified_tolerates_formatting() -> None:
    assert same_last_modified(OLD_DATE, f"  {OLD_DATE} ")
    assert same_last_modified("Tue, 06 Oct 2026 10:00:00 GMT", "Tue, 06 Oct 2026 10:00:00 +0000")
    assert not same_last_modified(OLD_DATE, NEW_DATE)
    assert not same_last_modified(OLD_DATE, "garbage")


def _state(etag: str = "", last_modified: str = "", *, done: int = 10, size: int = 100) -> sidecars.Sidecar:
    return sidecars.Sidecar(
        url="https://cdn/x", size=size, etag=etag, last_modified=last_modified, segments=[(0, size - 1, done)]
    )


def _link(etag: str = "", last_modified: str = "", *, size: int = 100, ranges: bool = True) -> ResolvedLink:
    return ResolvedLink(url="https://cdn/y", size=size, etag=etag, last_modified=last_modified, accept_ranges=ranges)


def test_if_range_validators_come_from_the_saved_state() -> None:
    # The new link's validators describe the *current* file; sending them would make the
    # server confirm the new file and the engine would append it to the old bytes.
    assert _resume_validators(_state("", OLD_DATE), _link('"new"', NEW_DATE)) == ('"new"', OLD_DATE)
    assert _resume_validators(_state('"a"', OLD_DATE), _link("", NEW_DATE)) == ('"a"', OLD_DATE)
    # a strong current ETag that equals a weak saved one is the better If-Range validator
    assert _resume_validators(_state('W/"a"', OLD_DATE), _link('"a"', OLD_DATE)) == ('"a"', OLD_DATE)
    # a weak link ETag never replaces the saved one
    assert _resume_validators(_state('"a"', ""), _link('W/"a"', OLD_DATE)) == ('"a"', OLD_DATE)
    assert _resume_validators(_state(), _link('"b"', NEW_DATE)) == ('"b"', NEW_DATE)


def test_resume_blocker_rules(tmp_path: Path) -> None:
    part = tmp_path / "x.part"
    part.write_bytes(b"\0" * 100)
    assert _resume_blocker(_state('"a"', OLD_DATE), _link('"a"', NEW_DATE), str(part)) == ""
    assert "Last-Modified" in _resume_blocker(_state("", OLD_DATE), _link("", NEW_DATE), str(part))
    assert "ETag" in _resume_blocker(_state('"a"'), _link('"b"'), str(part))
    assert "size" in _resume_blocker(_state(), _link(size=101), str(part))
    assert "resuming" in _resume_blocker(_state(), _link(ranges=False), str(part))
    # every byte already there: only the rename is left, no request needed
    assert _resume_blocker(_state(done=100), _link(ranges=False), str(part)) == ""
    part.write_bytes(b"\0" * 50)
    assert "truncated" in _resume_blocker(_state(), _link(), str(part))


# --- end to end ---------------------------------------------------------------------------


def _assert_resumed(state: dict, server: FileServer) -> None:
    """The run continued every unfinished segment from its saved offset and re-fetched nothing."""
    starts = set(server.ranged_starts())
    for seg in state["segments"]:
        length = seg["end"] - seg["start"] + 1
        if seg["done"] < length:
            assert seg["start"] + seg["done"] in starts
        if seg["done"]:
            assert seg["start"] not in starts


def _pause_then_replace(server: FileServer, downloader: HttpDownloader, dest: Path, new: bytes, **changes: str) -> dict:
    """Make a partial download of the current file, then swap in ``new`` (same size) on the server."""
    state = make_partial(downloader, server.link(), dest, at_least=512 * KIB)
    with server.lock:
        server.data = new
        for name, value in changes.items():
            setattr(server, name, value)
    server.reset_records()
    return state


def test_file_changed_with_same_size_and_new_date_restarts_instead_of_splicing(tmp_path: Path, http, make_server):
    old, new = random_bytes(2 * MIB, seed=1), random_bytes(2 * MIB, seed=2)
    server = make_server(old, etag="")  # an origin that only sends Last-Modified
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    dest = tmp_path / "Game.rar"
    state = _pause_then_replace(server, downloader, dest, new, last_modified=NEW_DATE)
    assert state["etag"] == "" and state["last_modified"] == OLD_DATE

    limiter.set_rate(0)
    downloader.download(server.link(), str(dest), token=CancelToken())  # fresh link: new date, same size

    assert file_sha256(dest) == sha256(new)
    assert 0 in server.ranged_starts()


def test_stale_link_after_a_same_size_change_is_caught_by_if_range(tmp_path: Path, http, make_server) -> None:
    old, new = random_bytes(2 * MIB, seed=1), random_bytes(2 * MIB, seed=3)
    server = make_server(old, etag="")
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    dest = tmp_path / "Game.rar"
    stale_link = server.link()
    _pause_then_replace(server, downloader, dest, new, last_modified=NEW_DATE)

    limiter.set_rate(0)
    downloader.download(stale_link, str(dest), token=CancelToken())  # link still claims the old date

    assert file_sha256(dest) == sha256(new)
    assert server.records[0].if_range == OLD_DATE  # the saved validator, so the server could refuse
    assert server.records[0].status == 200


def test_resume_sends_the_saved_date_when_the_saved_state_has_no_etag(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data, etag="")
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    dest = tmp_path / "Game.rar"
    state = make_partial(downloader, server.link(), dest, at_least=512 * KIB)
    server.reset_records()

    limiter.set_rate(0)
    downloader.download(server.link(url=server.url + "?sig=new"), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert server.records and all(r.status == 206 and r.if_range == OLD_DATE for r in server.records)
    _assert_resumed(state, server)


def test_strong_etag_match_resumes_even_if_the_date_moved(tmp_path: Path, http, make_server) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data)
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    dest = tmp_path / "Game.rar"
    state = make_partial(downloader, server.link(), dest, at_least=512 * KIB)
    server.reset_records()

    limiter.set_rate(0)
    # another CDN edge reports a different Last-Modified for the same (strong-ETag) file
    downloader.download(server.link(last_modified=NEW_DATE), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    _assert_resumed(state, server)
    assert all(r.if_range == '"v1"' for r in server.records)


def test_complete_state_is_finished_without_requests_even_without_range_support(
    tmp_path: Path, http, make_server
) -> None:
    data = random_bytes(300 * KIB)
    server = make_server(data)
    dest = tmp_path / "Game.rar"
    part, sidecar = part_files(dest)
    part.write_bytes(data)
    sidecars.save(
        str(sidecar),
        sidecars.Sidecar(url=server.url, size=len(data), etag='"v1"', segments=[(0, len(data) - 1, len(data))]),
    )

    make_downloader(http).download(server.link(accept_ranges=False), str(dest), token=CancelToken())

    assert file_sha256(dest) == sha256(data)
    assert server.records == []
    assert not part.exists() and not sidecar.exists()


def test_equal_date_spelled_differently_still_resumes_and_keeps_the_saved_value(
    tmp_path: Path, http, make_server
) -> None:
    data = random_bytes(2 * MIB)
    server = make_server(data, etag="")
    limiter = RateLimiter(2 * MIB)
    downloader = make_downloader(http, limiter)
    dest = tmp_path / "Game.rar"
    first = make_partial(downloader, server.link(), dest, at_least=256 * KIB)
    server.reset_records()

    equal_date = "Tue, 06 Oct 2026 10:00:00 +0000"
    second = make_partial(downloader, server.link(last_modified=equal_date), dest, at_least=768 * KIB)

    _assert_resumed(first, server)
    assert second["last_modified"] == OLD_DATE
    assert all(r.if_range == OLD_DATE for r in server.records)

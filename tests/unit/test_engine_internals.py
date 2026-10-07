"""Pure-logic pieces of the download engine: segments, sidecar, HTTP helpers, speed, part file."""

from __future__ import annotations

import errno
import json
import os
import threading
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests.structures import CaseInsensitiveDict

from anker_client.core.errors import DiskSpaceError, DownloadError
from anker_client.services.downloads import _engine_partfile as partfile
from anker_client.services.downloads import _engine_sidecar as sidecars
from anker_client.services.downloads._engine_http import (
    RemoteInfo,
    if_range_value,
    normalize_etag,
    parse_content_range,
    redact_url,
    retry_after_seconds,
    same_etag,
)
from anker_client.services.downloads._engine_progress import SpeedMeter
from anker_client.services.downloads._engine_segments import Segment, SegmentTable, coalesce, split_evenly

# --- segments -----------------------------------------------------------------------------


def _assert_tiles(table: SegmentTable, size: int) -> None:
    ranges = sorted((s.start, s.end) for s in table._segments)
    assert ranges[0][0] == 0
    for (_, prev_end), (start, _) in pairwise(ranges):
        assert start == prev_end + 1
    assert ranges[-1][1] == size - 1


def test_split_evenly_covers_the_file_exactly() -> None:
    segments = split_evenly(10, 3)
    assert [(s.start, s.end) for s in segments] == [(0, 2), (3, 5), (6, 9)]
    assert split_evenly(0, 4) == []
    assert [(s.start, s.end) for s in split_evenly(2, 8)] == [(0, 0), (1, 1)]


def test_claim_hands_out_idle_segments_in_order() -> None:
    table = SegmentTable(split_evenly(400, 4), steal_min=10, allow_split=True)
    claimed = [table.claim(i) for i in range(4)]
    assert [s.start for s in claimed if s] == [0, 100, 200, 300]


def test_claim_steals_the_second_half_of_the_busiest_segment() -> None:
    table = SegmentTable([Segment(0, 999), Segment(1000, 1099)], steal_min=10, allow_split=True)
    first = table.claim(0)
    second = table.claim(1)
    assert first is not None and second is not None
    table.commit(first, 100)  # first: 900 left; second: 100 left
    stolen = table.claim(2)
    assert stolen is not None
    assert (stolen.start, stolen.end) == (550, 999)
    assert first.end == 549
    assert len(table) == 3
    _assert_tiles(table, 1100)
    assert table.steals == 1


def test_steal_never_splits_inside_an_in_flight_write() -> None:
    table = SegmentTable([Segment(0, 99)], steal_min=10, allow_split=True)
    seg = table.claim(0)
    assert seg is not None
    offset, allowed = table.reserve(seg, 60)
    assert (offset, allowed) == (0, 60)
    stolen = table.claim(1)
    assert stolen is not None
    assert stolen.start >= 60
    # the owner's in-flight bytes still fit in its (shrunk) range
    assert seg.end >= 59
    assert table.commit(seg, 60) is False


def test_reserve_truncates_after_a_steal_and_commit_reports_completion() -> None:
    table = SegmentTable([Segment(0, 99)], steal_min=10, allow_split=True)
    seg = table.claim(0)
    assert seg is not None
    stolen = table.claim(1)
    assert stolen is not None and seg.end == 49
    table.commit(seg, *table.reserve(seg, 40)[1:])
    offset, allowed = table.reserve(seg, 40)
    assert (offset, allowed) == (40, 10)
    assert table.commit(seg, allowed) is True


def test_no_steal_below_threshold_or_when_splitting_disabled() -> None:
    table = SegmentTable([Segment(0, 29)], steal_min=16, allow_split=True)
    assert table.claim(0) is not None
    assert table.claim(1) is None  # 30 < 2 * 16
    single = SegmentTable([Segment(0, 9999)], steal_min=1, allow_split=False)
    assert single.claim(0) is not None
    assert single.claim(1) is None


def test_released_segment_is_claimed_again_before_stealing() -> None:
    table = SegmentTable([Segment(0, 999)], steal_min=10, allow_split=True)
    seg = table.claim(0)
    assert seg is not None
    table.commit(seg, 100)
    table.release(seg)
    again = table.claim(5)
    assert again is seg and again.owner == 5 and again.position == 100


def test_concurrent_claims_keep_segments_disjoint() -> None:
    size = 1_000_000
    table = SegmentTable(split_evenly(size, 4), steal_min=1000, allow_split=True)
    written = bytearray(size)
    lock = threading.Lock()

    def worker(owner: int) -> None:
        while (seg := table.claim(owner)) is not None:
            while True:
                offset, allowed = table.reserve(seg, 777)
                with lock:
                    for i in range(offset, offset + allowed):
                        written[i] += 1
                if table.commit(seg, allowed):
                    break
            table.release(seg)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert table.all_complete()
    assert table.bytes_done() == size
    assert written.count(1) == size  # every byte written exactly once
    _assert_tiles(table, size)


def test_snapshot_coalesces_finished_neighbours() -> None:
    assert coalesce([(0, 9, 10), (10, 19, 10), (20, 29, 3), (30, 39, 0)]) == [(0, 29, 23), (30, 39, 0)]
    table = SegmentTable([Segment(0, 9, done=10), Segment(10, 19, done=4)], steal_min=1, allow_split=False)
    assert table.snapshot() == [(0, 19, 14)]
    open_ended = SegmentTable([Segment(0, None, done=7)], steal_min=1, allow_split=False)
    assert open_ended.snapshot() == [(0, None, 7)]


def test_unknown_end_segment_completes_when_end_is_set() -> None:
    table = SegmentTable([Segment(0, None)], steal_min=1, allow_split=True)
    seg = table.claim(0)
    assert seg is not None
    assert table.reserve(seg, 500) == (0, 500)
    assert table.commit(seg, 500) is False
    table.set_end(seg, 499)
    assert seg.complete and table.all_complete()
    empty = Segment(0, None)
    SegmentTable([empty], steal_min=1, allow_split=False).set_end(empty, -1)
    assert empty.complete and empty.length == 0


# --- sidecar ------------------------------------------------------------------------------


def test_sidecar_round_trip_uses_the_documented_format(tmp_path) -> None:
    path = str(tmp_path / "game.rar.part.json")
    state = sidecars.Sidecar(
        url="https://cdn/x", size=100, etag='"e"', last_modified="lm", segments=[(0, 49, 49), (50, 99, 3)]
    )
    sidecars.save(path, state)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert raw == {
        "version": 1,
        "url": "https://cdn/x",
        "size": 100,
        "etag": '"e"',
        "last_modified": "lm",
        "segments": [{"start": 0, "end": 49, "done": 49}, {"start": 50, "end": 99, "done": 3}],
    }
    loaded = sidecars.load(path)
    assert loaded == state
    assert loaded.bytes_done == 52
    assert not os.path.exists(sidecars.temp_path(path))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.update(size=-1),
        lambda d: d.update(size="100"),
        lambda d: d.update(segments=[{"start": 0, "end": 49, "done": 0}]),  # does not cover size
        lambda d: d.update(segments=[{"start": 0, "end": 49, "done": 0}, {"start": 49, "end": 99, "done": 0}]),
        lambda d: d.update(segments=[{"start": 0, "end": 49, "done": 51}, {"start": 50, "end": 99, "done": 0}]),
        lambda d: d.update(segments=[{"start": 0, "end": 99, "done": True}]),
        lambda d: d.update(segments=None),
        lambda d: d.update(size=None),  # unknown size needs a single open segment
    ],
)
def test_invalid_sidecars_are_ignored(tmp_path, mutate) -> None:
    data = {"version": 1, "url": "u", "size": 100, "etag": "", "last_modified": "",
            "segments": [{"start": 0, "end": 49, "done": 10}, {"start": 50, "end": 99, "done": 0}]}
    mutate(data)
    path = tmp_path / "x.part.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert sidecars.load(str(path)) is None


def test_corrupt_or_missing_sidecar_loads_as_none(tmp_path) -> None:
    assert sidecars.load(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sidecars.load(str(bad)) is None


def test_unknown_size_sidecar_is_valid(tmp_path) -> None:
    path = str(tmp_path / "x.part.json")
    sidecars.save(path, sidecars.Sidecar(url="u", size=None, segments=[(0, None, 123)]))
    loaded = sidecars.load(path)
    assert loaded is not None and loaded.size is None and loaded.bytes_done == 123


# --- http helpers -------------------------------------------------------------------------


def test_parse_content_range() -> None:
    cr = parse_content_range("bytes 100-199/1000")
    assert (cr.start, cr.end, cr.total) == (100, 199, 1000)
    assert parse_content_range("bytes 0-0/*").total is None
    assert parse_content_range("bytes */1000") is None
    assert parse_content_range("bytes 5-1/10") is None
    assert parse_content_range("") is None
    assert parse_content_range(None) is None


def test_etag_helpers() -> None:
    assert normalize_etag('W/"abc"') == "abc"
    assert same_etag('"abc"', "abc")
    assert same_etag('W/"abc"', '"abc"')
    assert not same_etag('"abc"', '"abd"')
    assert if_range_value('"abc"', "date") == '"abc"'
    assert if_range_value("abc", "") == '"abc"'  # unquoted ETags are quoted for the header
    assert if_range_value('W/"abc"', "date") == "date"  # weak ETags are not allowed in If-Range
    assert if_range_value("", "") == ""


def _response(status: int, **headers: str) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, headers=CaseInsensitiveDict(headers))


def test_remote_info_from_responses() -> None:
    ranged = RemoteInfo.from_response(_response(206, **{"Content-Range": "bytes 0-9/500", "ETag": '"a"'}))
    assert (ranged.size, ranged.accept_ranges, ranged.etag) == (500, True, '"a"')
    full = RemoteInfo.from_response(_response(200, **{"Content-Length": "500", "Accept-Ranges": "bytes"}))
    assert (full.size, full.accept_ranges) == (500, True)
    encoded = RemoteInfo.from_response(_response(200, **{"Content-Length": "120", "Content-Encoding": "gzip"}))
    assert encoded.size is None
    unsatisfiable = RemoteInfo.from_response(_response(416, **{"Content-Range": "bytes */77"}))
    assert unsatisfiable.size == 77


def test_retry_after_parsing() -> None:
    assert retry_after_seconds(_response(503, **{"Retry-After": "7"})) == 7.0
    assert retry_after_seconds(_response(503, **{"Retry-After": "100000"})) == 120.0
    assert retry_after_seconds(_response(503)) == 0.0
    assert retry_after_seconds(_response(503, **{"Retry-After": "garbage"})) == 0.0


def test_redact_url_drops_query_and_path() -> None:
    assert redact_url("https://tunnel3.dlproxy.uk/a/b/Game.rar?sig=SECRET") == "https://tunnel3.dlproxy.uk/.../Game.rar"
    assert "SECRET" not in redact_url("https://h/?sig=SECRET")


# --- speed meter --------------------------------------------------------------------------


def test_speed_meter_is_accurate_from_the_first_samples() -> None:
    meter = SpeedMeter()
    meter.update(5000, 10.0)  # baseline (e.g. resumed bytes) does not count as speed
    assert meter.update(5000 + 250_000, 10.25) == pytest.approx(1_000_000)
    assert meter.update(5000 + 500_000, 10.5) == pytest.approx(1_000_000)


def test_speed_meter_smooths_over_seconds() -> None:
    meter = SpeedMeter()
    meter.update(0, 0.0)
    total, now = 0, 0.0
    for _ in range(80):  # 20 s at 1 MB/s
        now += 0.25
        total += 250_000
        meter.update(total, now)
    for _ in range(4):  # 1 s stall
        now += 0.25
        speed = meter.update(total, now)
    assert 0.7e6 < speed < 0.9e6  # decays gradually (~5 s time constant), not instantly
    assert SpeedMeter.eta(1_000_000, 500_000) == 2.0
    assert SpeedMeter.eta(None, 5) is None
    assert SpeedMeter.eta(10, 0) is None


def test_speed_meter_rebaselines_after_restart() -> None:
    meter = SpeedMeter()
    meter.update(0, 0.0)
    meter.update(1_000_000, 1.0)
    meter.update(0, 1.25)  # restarted from zero
    assert meter.update(500_000, 1.75) == pytest.approx(1_000_000)


# --- part file / disk errors --------------------------------------------------------------


def test_part_file_positional_writes_and_finish(tmp_path) -> None:
    path = str(tmp_path / "f.part")
    part = partfile.PartFile.create(path, 10)
    assert os.path.getsize(path) == 10
    part.write_at(5, b"WORLD")
    part.write_at(0, memoryview(b"HELLO"))
    part.finish(10)
    assert Path(path).read_bytes() == b"HELLOWORLD"
    with pytest.raises(partfile.PartFileClosed):
        part.write_at(0, b"x")
    part.close()  # idempotent


def test_part_file_finish_trims_unknown_size_downloads(tmp_path) -> None:
    path = str(tmp_path / "f.part")
    part = partfile.PartFile.create(path, None)
    part.write_at(0, b"abcdef")
    part.finish(3)
    assert Path(path).read_bytes() == b"abc"


@pytest.mark.skipif(os.name != "nt", reason="sparse flag is Windows-specific")
def test_part_file_is_sparse_on_ntfs(tmp_path) -> None:
    path = str(tmp_path / "big.part")
    part = partfile.PartFile.create(path, 8 * 1024**3)  # 8 GiB logical size, ~0 bytes allocated
    try:
        part.write_at(8 * 1024**3 - 4, b"tail")
        assert os.stat(path).st_file_attributes & 0x200  # FILE_ATTRIBUTE_SPARSE_FILE
    finally:
        part.close()
        os.remove(path)


def test_disk_full_maps_to_disk_space_error(tmp_path) -> None:
    error = partfile.disk_error(OSError(errno.ENOSPC, "No space left"), str(tmp_path / "x"), required=100)
    assert isinstance(error, DiskSpaceError)
    win = OSError(None, "disk full")
    win.winerror = 112  # type: ignore[attr-defined]
    assert isinstance(partfile.disk_error(win, str(tmp_path / "x"), required=1), DiskSpaceError)


def test_other_disk_errors_map_to_download_error(tmp_path) -> None:
    error = partfile.disk_error(OSError(errno.EACCES, "Access is denied"), str(tmp_path / "x"), required=1)
    assert isinstance(error, DownloadError) and not isinstance(error, DiskSpaceError)
    assert "Access is denied" in error.user_message


def test_free_bytes_walks_up_to_an_existing_parent(tmp_path) -> None:
    assert partfile.free_bytes(str(tmp_path / "does" / "not" / "exist")) is not None

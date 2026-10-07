"""Pure presentation rules of the library UI package (filters, sorts, badges, job texts)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anker_client.core.models import DownloadJob, DownloadKind, DownloadOption, ErrorKind, InstalledGame, JobState
from anker_client.ui.dialogs.game_dialogs import _guess_title, _relative_inside
from anker_client.ui.pages.downloads import format_limit, in_active_section, leftover_bytes
from anker_client.ui.widgets.job_row import (
    job_actions,
    job_badge,
    job_detail_text,
    job_option_text,
    job_progress_state,
    percent_text,
    retry_text,
    waiting_text,
)
from anker_client.ui.widgets.library_model import (
    filter_counts,
    filter_games,
    game_badges,
    matches_query,
    sort_games,
    status_info,
)

GB = 1024**3


def game(install_id: str, title: str, **kw) -> InstalledGame:
    data = {"path": rf"C:\Games\{title}", "library_root": r"C:\Games", "executable": "game.exe"}
    data.update(kw)
    return InstalledGame(install_id=install_id, title=title, **data)


@pytest.fixture
def games() -> list[InstalledGame]:
    return [
        game("a", "Alpha", playtime_seconds=50, last_played="2026-10-01T10:00:00+00:00",
             installed_at="2026-01-01T00:00:00+00:00", size_bytes=5 * GB),
        game("b", "bravo", favorite=True, playtime_seconds=500, last_played="2026-10-05T10:00:00+00:00",
             installed_at="2026-03-01T00:00:00+00:00", size_bytes=1 * GB),
        game("c", "Charlie", update_available=True, installed_at="2026-05-01T00:00:00+00:00"),
        game("d", "Delta", executable="", hidden=True, size_bytes=9 * GB),
        game("e", "Echo: Remastered", managed=False),
    ]


def ids(items: list[InstalledGame]) -> list[str]:
    return [g.install_id for g in items]


# --- library ------------------------------------------------------------------------------------


def test_filters_respect_hidden_flag(games):
    assert ids(filter_games(games)) == ["a", "b", "c", "e"]
    assert ids(filter_games(games, show_hidden=True)) == ["a", "b", "c", "d", "e"]
    assert ids(filter_games(games, filter_key="hidden")) == ["d"]
    assert ids(filter_games(games, filter_key="favorites")) == ["b"]
    assert ids(filter_games(games, filter_key="updates")) == ["c"]
    assert ids(filter_games(games, filter_key="unmanaged")) == ["e"]
    # "needs setup" is hidden unless hidden games are shown
    assert ids(filter_games(games, filter_key="needs_setup")) == []
    assert ids(filter_games(games, filter_key="needs_setup", show_hidden=True)) == ["d"]


def test_query_matches_words_ignoring_punctuation_and_case(games):
    assert ids(filter_games(games, query="echo remastered")) == ["e"]
    assert ids(filter_games(games, query="BRAVO")) == ["b"]
    assert matches_query(games[0], "")
    assert not matches_query(games[0], "zzz")


def test_filter_counts(games):
    counts = filter_counts(games)
    assert counts == {"all": 4, "favorites": 1, "updates": 1, "needs_setup": 0, "unmanaged": 1, "hidden": 1}


@pytest.mark.parametrize(("key", "expected"), [
    ("title", ["a", "b", "c", "d", "e"]),
    ("playtime", ["b", "a", "c", "d", "e"]),
    ("last_played", ["b", "a", "c", "d", "e"]),
    ("installed", ["c", "b", "a", "d", "e"]),
    ("size", ["d", "a", "b", "c", "e"]),
])
def test_sorts(games, key, expected):
    assert ids(sort_games(games, key)) == expected


def test_badges_and_status(games):
    assert [b.text for b in game_badges(games[2], running=False)] == ["Update"]
    assert [(b.text, b.kind) for b in game_badges(games[3], running=True)] == [
        ("Running", "accent"), ("Needs setup", "danger")]
    assert [(b.text, b.kind) for b in game_badges(games[4], running=False)] == [("Unmanaged", "")]
    assert status_info(games[0], running=True)[0] == "Running"
    assert status_info(games[3], running=False)[0] == "Needs setup"
    assert status_info(games[2], running=False)[0] == "Update available"
    assert status_info(games[0], running=False)[0] == "Ready to play"


# --- downloads ----------------------------------------------------------------------------------


def job(state: JobState, **kw) -> DownloadJob:
    data = {"id": "j1", "slug": "alpha", "title": "Alpha", "option": DownloadOption(1, "Direct"),
            "library_root": r"C:\Games", "state": state}
    data.update(kw)
    return DownloadJob(**data)


@pytest.mark.parametrize(("state", "extra", "expected"), [
    (JobState.QUEUED, {}, ["pause", "move_up", "move_down", "cancel"]),
    (JobState.DOWNLOADING, {}, ["pause", "cancel"]),
    (JobState.VERIFYING, {}, ["pause", "cancel"]),
    (JobState.PAUSED, {}, ["resume", "cancel"]),
    (JobState.WAITING, {}, ["retry", "pause", "cancel"]),
    (JobState.EXTRACTING, {}, ["cancel"]),
    (JobState.COMPLETED, {}, ["install", "remove"]),
    (JobState.COMPLETED, {"install_path": r"C:\Games\Alpha"}, ["play", "show_in_library", "remove"]),
    (JobState.FAILED, {"error_kind": ErrorKind.NETWORK}, ["retry", "remove"]),
    (JobState.FAILED, {"error_kind": ErrorKind.EXTERNAL_HOST, "error_url": "https://x"},
     ["retry", "open_browser", "import_archive", "remove"]),
    (JobState.FAILED, {"error_kind": ErrorKind.VERIFICATION}, ["retry", "import_archive", "remove"]),
    # an install failure retries too: retry() re-installs from the archive, or downloads again when it is gone
    (JobState.FAILED, {"error_kind": ErrorKind.EXTRACTION, "archive_path": r"C:\a.zip"}, ["retry", "remove"]),
    (JobState.CANCELLED, {}, ["retry", "remove"]),
])
def test_actions_per_state(state, extra, expected):
    assert job_actions(job(state, **extra)) == expected


def test_retry_button_caption():
    install_failure = job(JobState.FAILED, error_kind=ErrorKind.INSTALL, archive_path=r"C:\a.zip")
    assert retry_text(install_failure) == "Retry install"
    assert retry_text(job(JobState.FAILED, error_kind=ErrorKind.NETWORK)) == "Retry"
    assert retry_text(job(JobState.WAITING)) == "Retry now"


def test_leftover_bytes_only_for_failed_downloads_with_data():
    assert leftover_bytes(job(JobState.FAILED, bytes_done=3 * GB)) == 3 * GB
    assert leftover_bytes(job(JobState.FAILED, archive_path=r"C:\a.zip")) == 0  # data of unknown size
    assert leftover_bytes(job(JobState.FAILED)) is None
    assert leftover_bytes(job(JobState.FAILED, bytes_done=GB, imported_archive=True)) is None  # the user's file
    assert leftover_bytes(job(JobState.CANCELLED, bytes_done=GB)) is None  # cancel already deleted it
    assert leftover_bytes(job(JobState.COMPLETED, bytes_done=GB)) is None  # the archive is kept


def test_detail_text_downloading():
    j = job(JobState.DOWNLOADING, bytes_done=int(1.2 * GB), bytes_total=int(4.5 * GB),
            speed_bps=12.3 * 1024**2, eta_seconds=249)
    assert job_detail_text(j) == "1.20 GB of 4.50 GB · 12.3 MB/s · 4m 09s left"
    assert percent_text(j) == "26%"
    unknown_total = job(JobState.DOWNLOADING, bytes_done=1024**2, speed_bps=0)
    assert job_detail_text(unknown_total) == "1.00 MB downloaded"


def test_detail_text_other_states():
    assert job_detail_text(job(JobState.EXTRACTING, phase_progress=0.45)) == "Extracting 45%"
    assert job_detail_text(job(JobState.INSTALLING, phase_progress=1.0)) == "Installing 100%"
    assert job_detail_text(job(JobState.PAUSED, bytes_done=GB, bytes_total=4 * GB)) == "Paused · 1.00 GB of 4.00 GB"
    assert job_detail_text(job(JobState.FAILED, error="Boom")) == "Boom"
    assert job_detail_text(job(JobState.CANCELLED)) == "Cancelled"
    assert job_detail_text(job(JobState.COMPLETED, bytes_total=GB)) == "Download finished · ready to install"
    assert job_detail_text(job(JobState.COMPLETED, install_path="x")) == "Installed"
    assert job_detail_text(job(JobState.RESOLVING, status_text="Getting link")) == "Getting link"


def test_waiting_countdown_text():
    j = job(JobState.WAITING, retry_at=1000.0, error_kind=ErrorKind.RATE_LIMITED, error="Too many requests")
    assert waiting_text(j, now=958.0) == "Retrying in 42s · the site asked us to slow down"
    assert waiting_text(j, now=1000.0).startswith("Retrying now")
    assert job_detail_text(j, now=995.0).startswith("Retrying in 5s")
    no_deadline = job(JobState.WAITING, status_text="Waiting 42s (rate limited)")
    assert waiting_text(no_deadline, now=0) == "Waiting 42s (rate limited)"


def test_badges_and_progress_states():
    assert job_badge(job(JobState.DOWNLOADING)) == ("Downloading", "accent")
    assert job_badge(job(JobState.COMPLETED)) == ("Downloaded", "accent")
    assert job_badge(job(JobState.COMPLETED, install_path="x")) == ("Installed", "success")
    assert job_badge(job(JobState.FAILED)) == ("Failed", "danger")
    assert job_progress_state(job(JobState.DOWNLOADING)) == ""
    assert job_progress_state(job(JobState.PAUSED)) == "paused"
    assert job_progress_state(job(JobState.FAILED)) == "error"
    assert job_progress_state(job(JobState.EXTRACTING)) == "install"
    assert job_progress_state(job(JobState.COMPLETED)) == "success"


def test_option_text():
    assert job_option_text(job(JobState.QUEUED, bytes_total=2 * GB)) == "Direct · 2.00 GB"
    imported = job(JobState.EXTRACTING, imported_archive=True, option=DownloadOption(0, "Local", DownloadKind.PATCH))
    assert job_option_text(imported) == "Imported update"


def test_active_section_and_limits():
    assert in_active_section(job(JobState.PAUSED))
    assert in_active_section(job(JobState.COMPLETED))  # downloaded, not installed
    assert not in_active_section(job(JobState.COMPLETED, install_path="x"))
    assert not in_active_section(job(JobState.FAILED))
    assert format_limit(0) == "Unlimited"
    assert format_limit(5 * 1024) == "5 MB/s"
    assert format_limit(7680) == "7.5 MB/s"
    assert format_limit(512) == "512 KB/s"


# --- dialogs helpers ---------------------------------------------------------------------------


def test_guess_title_from_archive_name():
    assert _guess_title(r"D:\dl\Hollow-Knight_v1.5.78.zip") == "Hollow Knight"
    assert _guess_title("Elden.Ring.7z") == "Elden Ring"


def test_relative_inside(tmp_path: Path):
    root = tmp_path / "Game"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "game.exe"
    exe.write_bytes(b"MZ")
    outside = tmp_path / "other.exe"
    outside.write_bytes(b"MZ")
    assert _relative_inside(str(root), str(exe)) == str(Path("bin") / "game.exe")
    assert _relative_inside(str(root), str(outside)) is None
    assert _relative_inside(str(root), str(root)) is None

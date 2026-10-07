"""Download-queue summary used by the sidebar, status strip, tray and quit confirmation."""

from __future__ import annotations

from typing import Any

import pytest

from anker_client.core.models import DownloadJob, DownloadOption, JobState
from anker_client.ui.shell_summary import DownloadSummary, summarize_jobs, tray_tooltip

MB = 1024**2


def job(state: JobState, title: str = "Celeste", **kw: Any) -> DownloadJob:
    return DownloadJob(id=f"{title}-{state.value}-{kw.get('bytes_done', 0)}", slug=title.lower(), title=title,
                       option=DownloadOption(1, "Direct"), library_root="C:/Games", state=state, **kw)


def test_idle() -> None:
    summary = summarize_jobs([])
    assert summary == DownloadSummary()
    assert summary.idle and summary.headline == "No downloads in progress" and summary.progress is None
    assert summarize_jobs([job(JobState.COMPLETED), job(JobState.CANCELLED)]).idle


def test_single_download_headline() -> None:
    summary = summarize_jobs([job(JobState.DOWNLOADING, bytes_done=25 * MB, bytes_total=100 * MB,
                                  speed_bps=5 * MB, eta_seconds=15)])
    assert summary.headline == "Downloading Celeste · 25% · 5.00 MB/s · 15s left"
    assert summary.focus_title == "Celeste"
    assert summary.progress == pytest.approx(0.25)
    assert summary.active == 1 and summary.in_progress == 1 and summary.unfinished == 1


def test_multiple_downloads_aggregate() -> None:
    jobs = [
        job(JobState.DOWNLOADING, "A", bytes_done=50, bytes_total=100, speed_bps=2 * MB, eta_seconds=60),
        job(JobState.DOWNLOADING, "B", bytes_done=0, bytes_total=300, speed_bps=1 * MB, eta_seconds=240),
        job(JobState.EXTRACTING, "C", phase_progress=0.5, bytes_total=100),
        job(JobState.QUEUED, "D"),
        job(JobState.PAUSED, "E"),
        job(JobState.FAILED, "F"),
    ]
    summary = summarize_jobs(jobs)
    assert summary.headline == "2 downloading · 3.00 MB/s · 4m 00s left · 1 installing"
    assert summary.speed_bps == 3 * MB
    assert summary.eta_seconds == 240
    assert (summary.downloading, summary.installing, summary.queued, summary.paused, summary.failed) == (2, 1, 1, 1, 1)
    assert summary.in_progress == 4 and summary.unfinished == 5
    # byte-weighted over in-flight jobs: (50 + 0 + 50) / 500
    assert summary.progress == pytest.approx(0.2)


def test_progress_falls_back_to_mean_without_sizes() -> None:
    summary = summarize_jobs([job(JobState.DOWNLOADING, "A", bytes_done=10), job(JobState.EXTRACTING, "B",
                                                                                    phase_progress=0.5)])
    assert summary.progress == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("jobs", "headline"),
    [
        ([job(JobState.EXTRACTING, phase_progress=0.45)], "Extracting Celeste · 45%"),
        ([job(JobState.INSTALLING, "A"), job(JobState.EXTRACTING, "B")], "2 installing"),
        ([job(JobState.VERIFYING)], "Verification needed for Celeste"),
        ([job(JobState.RESOLVING)], "Preparing Celeste…"),
        ([job(JobState.WAITING, status_text="Waiting 42s (rate limited)")], "Waiting 42s (rate limited)"),
        ([job(JobState.WAITING)], "Waiting to retry Celeste"),
        ([job(JobState.QUEUED, "A"), job(JobState.QUEUED, "B")], "2 downloads queued"),
        ([job(JobState.PAUSED)], "Paused · 1 download"),
    ],
)
def test_headlines(jobs: list[DownloadJob], headline: str) -> None:
    assert summarize_jobs(jobs).headline == headline


def test_unknown_speed_and_eta() -> None:
    summary = summarize_jobs([job(JobState.DOWNLOADING)])
    assert summary.headline == "Downloading Celeste · —"
    assert summary.eta_seconds is None


def test_tray_tooltip() -> None:
    assert tray_tooltip(summarize_jobs([])) == "AnkerClient"
    tip = tray_tooltip(summarize_jobs([job(JobState.DOWNLOADING, bytes_done=1, bytes_total=4, speed_bps=MB)]))
    assert tip == "AnkerClient\n1 download · 1.00 MB/s · 25%"
    assert tray_tooltip(summarize_jobs([job(JobState.PAUSED)])) == "AnkerClient\nPaused · 1 download"
    assert tray_tooltip(summarize_jobs([job(JobState.QUEUED)])) == "AnkerClient\n1 download queued"
    long_title = "X" * 300
    assert len(tray_tooltip(summarize_jobs([job(JobState.VERIFYING, long_title)]))) <= 127

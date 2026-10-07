"""Aggregate download-queue figures for the shell chrome. Qt-free.

The sidebar badge + mini progress bar, the status strip, the tray tooltip and
the quit confirmation all describe the queue the same way; this module is the
single place that turns a list of :class:`DownloadJob` snapshots into those
figures and texts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from anker_client.core.formatting import format_duration, format_speed, pluralize
from anker_client.core.models import DownloadJob, JobState

_INSTALL_STATES = frozenset({JobState.EXTRACTING, JobState.INSTALLING})


@dataclass(frozen=True, slots=True)
class DownloadSummary:
    downloading: int = 0
    preparing: int = 0  # RESOLVING
    verifying: int = 0
    installing: int = 0  # EXTRACTING / INSTALLING
    queued: int = 0
    waiting: int = 0
    paused: int = 0
    failed: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    progress: float | None = None  # 0..1 over in-flight jobs; None when nothing is in flight
    headline: str = "No downloads in progress"
    focus_title: str = ""  # title of the job the headline talks about

    @property
    def active(self) -> int:
        """Jobs doing work right now (network or disk)."""
        return self.downloading + self.preparing + self.verifying + self.installing

    @property
    def in_progress(self) -> int:
        """Jobs that quitting would interrupt (they resume on the next start)."""
        return self.active + self.queued + self.waiting

    @property
    def unfinished(self) -> int:
        return self.in_progress + self.paused

    @property
    def idle(self) -> bool:
        return self.unfinished == 0


def summarize_jobs(jobs: Iterable[DownloadJob]) -> DownloadSummary:
    by_state: dict[JobState, list[DownloadJob]] = {}
    for job in jobs:
        by_state.setdefault(job.state, []).append(job)

    def of(*states: JobState) -> list[DownloadJob]:
        return [job for state in states for job in by_state.get(state, [])]

    downloading = of(JobState.DOWNLOADING)
    installing = of(JobState.EXTRACTING, JobState.INSTALLING)
    in_flight = of(JobState.RESOLVING, JobState.VERIFYING, JobState.DOWNLOADING, JobState.WAITING,
                   JobState.EXTRACTING, JobState.INSTALLING)
    speed = sum(max(0.0, job.speed_bps) for job in downloading)
    etas = [job.eta_seconds for job in downloading if job.eta_seconds is not None and job.eta_seconds >= 0]

    counts = {
        "downloading": len(downloading),
        "preparing": len(of(JobState.RESOLVING)),
        "verifying": len(of(JobState.VERIFYING)),
        "installing": len(installing),
        "queued": len(of(JobState.QUEUED)),
        "waiting": len(of(JobState.WAITING)),
        "paused": len(of(JobState.PAUSED)),
        "failed": len(of(JobState.FAILED)),
    }
    headline, focus = _headline(by_state, counts, speed, max(etas) if etas else None)
    return DownloadSummary(
        **counts,
        speed_bps=speed,
        eta_seconds=max(etas) if etas else None,
        progress=_aggregate_progress(in_flight),
        headline=headline,
        focus_title=focus,
    )


def _aggregate_progress(jobs: list[DownloadJob]) -> float | None:
    if not jobs:
        return None
    if all(job.bytes_total for job in jobs):
        total = sum(job.bytes_total or 0 for job in jobs)
        return max(0.0, min(1.0, sum(job.progress * (job.bytes_total or 0) for job in jobs) / total))
    return max(0.0, min(1.0, sum(job.progress for job in jobs) / len(jobs)))


def _headline(
    by_state: dict[JobState, list[DownloadJob]],
    counts: dict[str, int],
    speed: float,
    eta: float | None,
) -> tuple[str, str]:
    def first(*states: JobState) -> DownloadJob:
        return next(job for state in states for job in by_state.get(state, []))

    if counts["downloading"]:
        if counts["downloading"] == 1:
            job = first(JobState.DOWNLOADING)
            parts = [f"Downloading {job.title}"]
            if job.bytes_total:
                parts.append(f"{job.progress * 100:.0f}%")
            focus = job.title
        else:
            parts = [f"{counts['downloading']} downloading"]
            focus = ""
        parts.append(format_speed(speed))
        if eta is not None:
            parts.append(f"{format_duration(eta)} left")
        if counts["installing"]:
            parts.append(f"{counts['installing']} installing")
        return " · ".join(parts), focus
    if counts["installing"]:
        if counts["installing"] == 1:
            job = first(*_INSTALL_STATES)
            return f"{job.state.label} {job.title} · {job.progress * 100:.0f}%", job.title
        return f"{counts['installing']} installing", ""
    if counts["verifying"]:
        job = first(JobState.VERIFYING)
        return f"Verification needed for {job.title}", job.title
    if counts["preparing"]:
        job = first(JobState.RESOLVING)
        return f"Preparing {job.title}…", job.title
    if counts["waiting"]:
        job = first(JobState.WAITING)
        return (job.status_text or f"Waiting to retry {job.title}"), job.title
    if counts["queued"]:
        return f"{pluralize(counts['queued'], 'download')} queued", ""
    if counts["paused"]:
        return f"Paused · {pluralize(counts['paused'], 'download')}", ""
    return "No downloads in progress", ""


def tray_tooltip(summary: DownloadSummary, app_name: str = "AnkerClient") -> str:
    """Short multi-line tooltip (Windows truncates tray tooltips at 127 characters)."""
    if summary.idle:
        return app_name
    lines = [app_name]
    if summary.downloading:
        detail = f"{pluralize(summary.downloading, 'download')} · {format_speed(summary.speed_bps)}"
        if summary.progress is not None:
            detail = f"{detail} · {summary.progress * 100:.0f}%"
        lines.append(detail)
    elif summary.active or summary.waiting or summary.queued:
        lines.append(summary.headline)
    else:
        lines.append(f"Paused · {pluralize(summary.paused, 'download')}")
    text = "\n".join(lines)
    return text if len(text) <= 127 else text[:126] + "…"

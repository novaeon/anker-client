"""Domain model shared by every layer.

Rules:
* Pure data — no Qt, no I/O, no service references.
* Every model round-trips through ``to_dict``/``from_dict`` so it can be stored
  as JSON (SQLite ``json`` columns, manifests, the job queue) and sent across
  threads safely (callers receive copies, never shared mutable state).
* Dates are ISO-8601 strings (``YYYY-MM-DD`` or full ``datetime.isoformat()``
  in UTC); sizes are ints in bytes with an optional human ``*_text`` copy of
  what the site displayed.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from anker_client.constants import BASE_URL

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Current UTC time as an ISO string with seconds precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _filter_kwargs(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


class _Serializable:
    """Mixin giving dataclasses a tolerant JSON round-trip."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**_filter_kwargs(cls, dict(data or {})))

    def copy(self) -> Self:
        return copy.deepcopy(self)


# ---------------------------------------------------------------------------
# catalog / store
# ---------------------------------------------------------------------------


class SortOrder(StrEnum):
    """Server-side listing orders (values are the site's ``?sort=`` keys)."""

    NEWEST = "created_at"
    MOST_VIEWED = "view"
    MOST_LIKED = "like_count"
    TOP_RATED = "vote_average"
    RELEASE_DATE = "release_date"
    TITLE = "title"

    @property
    def label(self) -> str:
        return {
            SortOrder.NEWEST: "Recently added",
            SortOrder.MOST_VIEWED: "Most viewed",
            SortOrder.MOST_LIKED: "Most liked",
            SortOrder.TOP_RATED: "Top rated",
            SortOrder.RELEASE_DATE: "Release date",
            SortOrder.TITLE: "Title (A–Z)",
        }[self]


@dataclass(frozen=True, slots=True)
class Genre(_Serializable):
    slug: str  # url slug used by /genre/{slug}
    name: str


@dataclass(slots=True)
class GameSummary(_Serializable):
    """A game as shown on a listing card."""

    slug: str
    title: str
    cover_url: str = ""  # 2:3 poster image (jpg)
    primary_genre: str = ""
    year: int | None = None
    size_text: str = ""  # as displayed, e.g. "104.60 GB"
    size_bytes: int | None = None

    @property
    def page_url(self) -> str:
        return f"{BASE_URL}/game/{self.slug}"


@dataclass(slots=True)
class ListingPage(_Serializable):
    games: list[GameSummary] = field(default_factory=list)
    page: int = 1
    has_next: bool = False
    total_pages: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ListingPage:
        data = dict(data or {})
        data["games"] = [GameSummary.from_dict(g) for g in data.get("games", [])]
        return cls(**_filter_kwargs(cls, data))


@dataclass(slots=True)
class HomeSection(_Serializable):
    title: str
    games: list[GameSummary] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HomeSection:
        data = dict(data or {})
        data["games"] = [GameSummary.from_dict(g) for g in data.get("games", [])]
        return cls(**_filter_kwargs(cls, data))


class DownloadKind(StrEnum):
    FULL = "full"  # complete game archive ("Direct", "Direct V 4.1.1")
    PATCH = "patch"  # "Update Only From V x To V y" — overlay onto an existing install
    ADDON = "addon"  # "Language Pack", "Launcher", DLC… — overlay onto an existing install

    @staticmethod
    def classify(label: str) -> DownloadKind:
        """PATCH for "Update Only From V a To V b", ADDON only when the label names an add-on,
        FULL otherwise — mirror/host labels ("Direct", "DataNodes", "Mirror 2") are the full game."""
        text = label.casefold()
        if "update only" in text or re.search(r"\bfrom\s+v?\s*[\d.]+\s+to\s+v?\s*[\d.]+", text):
            return DownloadKind.PATCH
        if _ADDON_LABEL_RE.search(text):
            return DownloadKind.ADDON
        return DownloadKind.FULL


_ADDON_LABEL_RE = re.compile(
    r"\b(?:language|lang\s*pack|launcher|dlcs?|add[\s-]?ons?|soundtrack|ost|bonus|artbook|"
    r"voice\s*pack|subtitles?|editor|sdk|dedicated\s+server|crack|fix|trainer|mods?)\b"
)


@dataclass(frozen=True, slots=True)
class DownloadOption(_Serializable):
    """One entry from a game page's download modal."""

    download_id: int
    label: str
    kind: DownloadKind = DownloadKind.FULL
    size_text: str = ""  # parsed from a "(124 MB)" suffix when present
    from_version: str = ""  # PATCH only
    to_version: str = ""  # PATCH only / or "Direct V x" version

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadOption:
        data = dict(data or {})
        if "kind" in data:
            data["kind"] = DownloadKind(data["kind"])
        return cls(**_filter_kwargs(cls, data))


@dataclass(slots=True)
class SystemRequirements(_Serializable):
    raw: str = ""
    os: str = ""
    processor: str = ""
    memory: str = ""
    graphics: str = ""
    directx: str = ""
    storage: str = ""


@dataclass(slots=True)
class GameDetails(_Serializable):
    """Everything known about a game from its /game/{slug} page."""

    slug: str
    title: str
    description: str = ""
    cover_url: str = ""  # 2:3 poster
    hero_url: str = ""  # wide artwork (16:9) when available
    genres: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    version: str = ""  # e.g. "v1.5.12620" (site's softwareVersion)
    release_date: str = ""  # ISO date
    updated_date: str = ""  # ISO date (site's dateModified) — changes when a new build is posted
    size_text: str = ""
    size_bytes: int | None = None
    requirements: SystemRequirements = field(default_factory=SystemRequirements)
    download_options: list[DownloadOption] = field(default_factory=list)
    torrent_available: bool = False
    fetched_at: str = ""  # ISO datetime when parsed

    @property
    def page_url(self) -> str:
        return f"{BASE_URL}/game/{self.slug}"

    @property
    def primary_option(self) -> DownloadOption | None:
        for option in self.download_options:
            if option.kind is DownloadKind.FULL:
                return option
        return self.download_options[0] if self.download_options else None

    def to_summary(self) -> GameSummary:
        year = int(self.release_date[:4]) if self.release_date[:4].isdigit() else None
        return GameSummary(
            slug=self.slug,
            title=self.title,
            cover_url=self.cover_url,
            primary_genre=self.genres[0] if self.genres else "",
            year=year,
            size_text=self.size_text,
            size_bytes=self.size_bytes,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameDetails:
        data = dict(data or {})
        data["requirements"] = SystemRequirements.from_dict(data.get("requirements") or {})
        data["download_options"] = [DownloadOption.from_dict(o) for o in data.get("download_options", [])]
        return cls(**_filter_kwargs(cls, data))


# ---------------------------------------------------------------------------
# download pipeline
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TicketPage(_Serializable):
    """Parsed /download/{signed}/{hash} page (the "treasure box")."""

    ticket_url: str
    file_url: str  # https://ankergames.net/download-file/{ticket}
    wait_seconds: int = 0  # client-side countdown the site applies for this tier
    requires_verification: bool = False  # Cloudflare Turnstile widget present
    turnstile_sitekey: str = ""
    external_provider: str = ""  # name of an external host hand-off (empty = direct)
    is_torrent: bool = False
    version: str = ""
    size_text: str = ""


@dataclass(slots=True)
class ResolvedLink(_Serializable):
    """A final, directly downloadable file URL (usually a signed CDN URL)."""

    url: str
    filename: str = ""
    size: int | None = None
    etag: str = ""
    last_modified: str = ""
    accept_ranges: bool = False
    content_type: str = ""


class JobState(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"  # minting ticket / fetching ticket page
    VERIFYING = "verifying"  # waiting for the browser verification (Turnstile)
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING = "waiting"  # backing off (rate-limited / transient error) until ``retry_at``
    EXTRACTING = "extracting"
    INSTALLING = "installing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in {
            JobState.RESOLVING,
            JobState.VERIFYING,
            JobState.DOWNLOADING,
            JobState.EXTRACTING,
            JobState.INSTALLING,
        }

    @property
    def is_finished(self) -> bool:
        return self in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}

    @property
    def label(self) -> str:
        return {
            JobState.QUEUED: "Queued",
            JobState.RESOLVING: "Preparing",
            JobState.VERIFYING: "Verifying",
            JobState.DOWNLOADING: "Downloading",
            JobState.PAUSED: "Paused",
            JobState.WAITING: "Waiting",
            JobState.EXTRACTING: "Extracting",
            JobState.INSTALLING: "Installing",
            JobState.COMPLETED: "Completed",
            JobState.FAILED: "Failed",
            JobState.CANCELLED: "Cancelled",
        }[self]


class ErrorKind(StrEnum):
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"
    GEO_BLOCKED = "geo_blocked"
    ACCESS_DENIED = "access_denied"
    VERIFICATION = "verification"
    EXTERNAL_HOST = "external_host"
    LINK_EXPIRED = "link_expired"
    DISK_SPACE = "disk_space"
    EXTRACTION = "extraction"
    INSTALL = "install"
    SITE_CHANGED = "site_changed"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class DownloadJob(_Serializable):
    """A persistent download → install pipeline entry."""

    id: str
    slug: str
    title: str
    option: DownloadOption
    library_root: str
    cover_url: str = ""
    target_version: str = ""  # game version when queued (written to the manifest)
    source_updated_date: str = ""  # site dateModified when queued
    genres: list[str] = field(default_factory=list)
    state: JobState = JobState.QUEUED
    position: int = 0  # queue order (lower runs first)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    started_at: str = ""
    completed_at: str = ""
    bytes_done: int = 0
    bytes_total: int | None = None
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    phase_progress: float = 0.0  # 0..1 for extracting/installing
    status_text: str = ""  # short human status line ("Waiting 42s (rate limited)")
    archive_path: str = ""  # local archive (partial or complete)
    filename: str = ""
    resolved_url: str = ""
    etag: str = ""
    error: str = ""
    error_kind: ErrorKind | None = None
    error_url: str = ""  # e.g. ticket URL to open in a browser for EXTERNAL_HOST / VERIFICATION
    retry_at: float | None = None  # epoch seconds, for WAITING
    attempts: int = 0
    install_path: str = ""  # set when completed
    imported_archive: bool = False  # True when installing a user-supplied archive (no download)

    @property
    def progress(self) -> float:
        """Overall 0..1 progress for the current phase."""
        if self.state in (JobState.EXTRACTING, JobState.INSTALLING):
            return max(0.0, min(1.0, self.phase_progress))
        if self.state is JobState.COMPLETED:
            return 1.0
        if self.bytes_total:
            return max(0.0, min(1.0, self.bytes_done / self.bytes_total))
        return 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadJob:
        data = dict(data or {})
        data["option"] = DownloadOption.from_dict(data.get("option") or {})
        if data.get("state"):
            data["state"] = JobState(data["state"])
        if data.get("error_kind"):
            data["error_kind"] = ErrorKind(data["error_kind"])
        return cls(**_filter_kwargs(cls, data))


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InstallManifest(_Serializable):
    """Contents of ``<game dir>/.ankerclient.json`` — the on-disk source of truth."""

    schema: int = 1
    slug: str = ""  # empty for imported folders that could not be matched
    title: str = ""
    version: str = ""
    source_updated_date: str = ""
    installed_at: str = ""
    updated_at: str = ""
    executable: str = ""  # path relative to the install dir, "" = not chosen yet
    launch_args: str = ""
    run_as_admin: bool = False
    applied_options: list[str] = field(default_factory=list)  # labels of FULL/PATCH/ADDON applied
    has_redist: bool = False
    redist_installed: bool = False
    cover_url: str = ""
    genres: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InstalledGame(_Serializable):
    """A game folder in a library root, merged with per-user DB data."""

    install_id: str  # stable: slug when known, else "local:<folder name casefolded>"
    title: str
    path: str  # absolute install directory
    library_root: str
    slug: str = ""
    managed: bool = True  # has a manifest written by AnkerClient
    version: str = ""
    source_updated_date: str = ""
    installed_at: str = ""
    executable: str = ""  # relative to ``path``; "" = needs selection
    launch_args: str = ""
    run_as_admin: bool = False
    applied_options: list[str] = field(default_factory=list)
    has_redist: bool = False
    redist_installed: bool = False
    cover_url: str = ""
    genres: list[str] = field(default_factory=list)
    # per-user data (SQLite)
    favorite: bool = False
    hidden: bool = False
    playtime_seconds: int = 0
    last_played: str = ""
    size_bytes: int | None = None
    latest_version: str = ""
    update_available: bool = False

    @property
    def executable_path(self) -> str:
        if not self.executable:
            return ""
        import os

        return os.path.normpath(os.path.join(self.path, self.executable))

    @property
    def folder_name(self) -> str:
        import os

        return os.path.basename(os.path.normpath(self.path))


@dataclass(slots=True)
class InstallRequest(_Serializable):
    archive_path: str
    slug: str
    title: str
    option: DownloadOption
    library_root: str
    version: str = ""
    source_updated_date: str = ""
    cover_url: str = ""
    genres: list[str] = field(default_factory=list)
    existing_install_path: str = ""  # target for PATCH/ADDON, or the install being replaced


@dataclass(slots=True)
class InstallResult(_Serializable):
    install_path: str
    executable: str = ""  # relative; "" when ambiguous
    executable_candidates: list[str] = field(default_factory=list)  # relative, best first
    has_redist: bool = False
    size_bytes: int = 0


# ---------------------------------------------------------------------------
# accounts / updates
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UserInfo(_Serializable):
    display_name: str
    email: str = ""
    avatar_url: str = ""
    profile_url: str = ""
    is_subscriber: bool = False


@dataclass(slots=True)
class AppRelease(_Serializable):
    version: str
    url: str
    notes: str = ""
    published_at: str = ""
    download_url: str = ""


@dataclass(slots=True)
class GameUpdate(_Serializable):
    install_id: str
    slug: str
    title: str
    installed_version: str
    latest_version: str
    patch_option: DownloadOption | None = None  # small "Update Only" patch when applicable
    full_option: DownloadOption | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameUpdate:
        data = dict(data or {})
        for key in ("patch_option", "full_option"):
            if data.get(key):
                data[key] = DownloadOption.from_dict(data[key])
        return cls(**_filter_kwargs(cls, data))


@dataclass(slots=True)
class ArchiveEntry(_Serializable):
    path: str
    size: int = 0
    is_dir: bool = False

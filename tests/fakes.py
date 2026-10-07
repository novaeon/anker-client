"""In-memory fakes implementing every service contract, for UI tests and screenshots.

``FakeContext`` has the same attributes as ``services.container.AppContext``.
Real: ``paths``, ``settings``, ``events``, ``db``, ``runner``. Everything else
is a fake with deterministic sample data, small artificial latency (so loading
states are exercised) and the same events the real services publish.

Run ``python -m tests.fakes`` to open the real ``MainWindow`` on fake data
(once the shell is implemented) — handy for manual UI review.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from anker_client.core import events as ev
from anker_client.core.db import Database
from anker_client.core.errors import ExecutableNotSetError, LoginFailedError, NotFoundError
from anker_client.core.models import (
    AppRelease,
    DownloadJob,
    DownloadKind,
    DownloadOption,
    ErrorKind,
    GameDetails,
    GameSummary,
    GameUpdate,
    Genre,
    HomeSection,
    InstalledGame,
    JobState,
    ListingPage,
    ResolvedLink,
    SortOrder,
    SystemRequirements,
    UserInfo,
    utc_now_iso,
)
from anker_client.core.paths import AppPaths, normalize_title
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken, TaskRunner

LATENCY = 0.15

_TITLES = [
    "Hollow Knight", "Hollow Knight: Silksong", "Gears of War: E-Day", "Cyberpunk 2077", "Elden Ring",
    "Red Dead Redemption 2", "Baldur's Gate 3", "The Dark Queen of Mortholme", "Roadhouse Simulator",
    "Onimusha: Warlords", "Wild West Pioneers", "Wasteland 3", "Minecraft", "Lethal Company",
    "Age Of Empires II", "Blasphemous", "Forza Horizon 6", "Grand Theft Auto V", "Stardew Valley",
    "Hades II", "Celeste", "Dead Cells", "Terraria", "Subnautica", "Resident Evil 4", "Sekiro",
    "DOOM Eternal", "Portal 2", "Disco Elysium", "Outer Wilds", "Slay the Spire", "Cuphead",
    "Ori and the Will of the Wisps", "Inside", "Limbo", "Tunic", "Death Stranding", "It Takes Two",
    "Mewgenics", "Horripilant", "Songs of Glimmerwick", "Nivalis Nights", "Graveyard Keeper 2",
    "Mass Effect: Andromeda", "Capcom Fighting Collection", "Happy Wheels", "SILENT HILL: Townfall",
    "Among Us", "American Truck Simulator", "ALL WILL FALL", "007 First Light", "1000xRESIST",
    "33 Immortals", "60 Parsecs!", "7 Days To Die", "9 Days", "#DRIVE Rally", "Project Wingman",
    "GERONIMO", "Void Crew",
]
_GENRES = ["Action", "Adventure", "RPG", "Indie", "Simulation", "Horror", "Racing", "Puzzle", "Survival",
           "Open World", "Multiplayer", "Sports"]


def _slug(title: str) -> str:
    return "-".join(normalize_title(title).split())


def sample_games() -> list[GameSummary]:
    games = []
    for i, title in enumerate(_TITLES):
        size = (0.2 + (i * 7.3) % 120) * 1024**3
        games.append(GameSummary(
            slug=_slug(title), title=title, cover_url=f"https://fake.invalid/poster/{_slug(title)}.png",
            primary_genre=_GENRES[i % len(_GENRES)], year=2010 + (i % 17),
            size_text=f"{size / 1024**3:.2f} GB", size_bytes=int(size),
        ))
    return games


def sample_details(summary: GameSummary) -> GameDetails:
    i = _TITLES.index(summary.title) if summary.title in _TITLES else 0
    options = [DownloadOption(1000 + i, "Direct", DownloadKind.FULL)]
    if i % 4 == 0:
        options.append(DownloadOption(2000 + i, "Language Pack (1.2 GB)", DownloadKind.ADDON, size_text="1.2 GB"))
    if i % 5 == 0:
        options.append(DownloadOption(3000 + i, "Update Only From V 1.0.0 To V 1.1.0 (124 MB)", DownloadKind.PATCH,
                                      size_text="124 MB", from_version="1.0.0", to_version="1.1.0"))
    return GameDetails(
        slug=summary.slug, title=summary.title,
        description=(f"{summary.title} is a sample game used by AnkerClient's test fakes. " * 6).strip(),
        cover_url=summary.cover_url, hero_url=f"https://fake.invalid/hero/{summary.slug}.png",
        genres=[summary.primary_genre, *(_GENRES[(i + k) % len(_GENRES)] for k in (3, 5, 7))],
        screenshots=[f"https://fake.invalid/shot/{summary.slug}/{n}.png" for n in range(1, 5)],
        version="v1.1.0", release_date=f"{summary.year or 2020}-03-1{i % 9}", updated_date="2026-09-06",
        size_text=summary.size_text, size_bytes=summary.size_bytes,
        requirements=SystemRequirements(raw="OS: Windows 10", os="Windows 10 64-bit", processor="Intel Core i5",
                                        memory="8 GB RAM", graphics="GeForce GTX 1060", directx="Version 11",
                                        storage="20 GB available space"),
        download_options=options, torrent_available=True, fetched_at=utc_now_iso(),
    )


# --- fake services ---------------------------------------------------------------------


class FakeHttp:
    def __init__(self) -> None:
        self._ua = "FakeUA/1.0"
        self._cookies: list[dict[str, Any]] = []

    @property
    def user_agent(self) -> str:
        return self._ua

    def set_user_agent(self, ua: str) -> None:
        self._ua = ua

    def export_cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies)

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = list(cookies)

    def clear_cookies(self) -> None:
        self._cookies = []

    def cookie(self, name: str, domain: str | None = None) -> str | None:
        return next((c["value"] for c in self._cookies if c.get("name") == name), None)

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.games = sample_games()
        self.http = FakeHttp()

    def _page(self, games: list[GameSummary], page: int, per_page: int = 24) -> ListingPage:
        start = (page - 1) * per_page
        chunk = games[start:start + per_page]
        return ListingPage(games=[g.copy() for g in chunk], page=page, has_next=start + per_page < len(games),
                           total_pages=(len(games) + per_page - 1) // per_page)

    def browse(self, *, page: int = 1, sort: SortOrder = SortOrder.NEWEST, genre: str | None = None,
               token: CancelToken | None = None) -> ListingPage:
        time.sleep(LATENCY)
        games = list(self.games)
        if genre:
            games = [g for g in games if _slug(g.primary_genre) == genre or genre == "vr"]
        if sort is SortOrder.TITLE:
            games.sort(key=lambda g: g.title.casefold())
        elif sort is SortOrder.RELEASE_DATE:
            games.sort(key=lambda g: g.year or 0, reverse=True)
        return self._page(games, page)

    def search(self, query: str, *, page: int = 1, token: CancelToken | None = None) -> ListingPage:
        time.sleep(LATENCY)
        q = normalize_title(query)
        return self._page([g for g in self.games if q in normalize_title(g.title)], page)

    def top_games(self, *, token: CancelToken | None = None) -> list[GameSummary]:
        time.sleep(LATENCY)
        return [g.copy() for g in self.games[5:17]]

    def home_sections(self, *, token: CancelToken | None = None) -> list[HomeSection]:
        time.sleep(LATENCY)
        return [
            HomeSection("Trending Games", [g.copy() for g in self.games[:12]]),
            HomeSection("Upcoming Games", [g.copy() for g in self.games[12:20]]),
            HomeSection("Latest Games", [g.copy() for g in self.games[20:34]]),
            HomeSection("Masterpiece Collection", [g.copy() for g in self.games[34:44]]),
        ]

    def genres(self, *, token: CancelToken | None = None) -> list[Genre]:
        return [Genre(_slug(g), g) for g in _GENRES] + [Genre("vr", "VR"), Genre("nsfw", "NSFW")]

    def game_details(self, slug: str, *, token: CancelToken | None = None) -> GameDetails:
        time.sleep(LATENCY * 2)
        for g in self.games:
            if g.slug == slug:
                return sample_details(g)
        raise NotFoundError()

    def mint_download_ticket(self, download_id: int, *, referer_slug: str = "", token: CancelToken | None = None) -> str:
        return f"https://fake.invalid/download/{download_id}"

    def probe(self, url: str, *, token: CancelToken | None = None) -> ResolvedLink:
        return ResolvedLink(url=url, filename="game.zip", size=1024**3, accept_ranges=True)

    def login(self, email: str, password: str, *, remember: bool = True, token: CancelToken | None = None) -> UserInfo:
        time.sleep(LATENCY)
        if password != "password":
            raise LoginFailedError()
        return UserInfo(display_name=email.split("@", maxsplit=1)[0], email=email)

    def logout(self, *, token: CancelToken | None = None) -> None:
        pass

    def current_user(self, *, token: CancelToken | None = None) -> UserInfo | None:
        return None


class FakeAuth:
    def __init__(self, client: FakeClient, events: ev.EventBus) -> None:
        self._client = client
        self._events = events
        self._user: UserInfo | None = None

    @property
    def user(self) -> UserInfo | None:
        return self._user

    @property
    def is_logged_in(self) -> bool:
        return self._user is not None

    def remembered_email(self) -> str:
        return "player@example.com"

    def restore(self, *, token: CancelToken | None = None) -> UserInfo | None:
        return self._user

    def login(self, email: str, password: str, *, remember: bool = True, token: CancelToken | None = None) -> UserInfo:
        self._user = self._client.login(email, password)
        self._events.publish(ev.AuthChanged(self._user))
        return self._user

    def login_with_cookies(self, cookies: list[dict[str, Any]], *, token: CancelToken | None = None) -> UserInfo:
        self._user = UserInfo(display_name="discord-user")
        self._events.publish(ev.AuthChanged(self._user))
        return self._user

    def logout(self, *, token: CancelToken | None = None) -> None:
        self._user = None
        self._events.publish(ev.AuthChanged(None))

    def save_session(self) -> None:
        pass


class FakeImageCache:
    """Generates a coloured poster PNG per URL (deterministic colour from the URL hash)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, url: str) -> Path:
        return self._root / (hashlib.sha1(url.encode()).hexdigest() + ".png")

    def cached_path(self, url: str) -> Path | None:
        p = self._path(url)
        return p if p.exists() else None

    def fetch(self, url: str, *, token: CancelToken | None = None) -> Path:
        path = self._path(url)
        with self._lock:
            if path.exists():
                return path
            from PyQt6.QtCore import QRect, Qt
            from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter

            wide = "/hero/" in url or "/shot/" in url
            w, h = (960, 540) if wide else (300, 450)
            digest = hashlib.sha1(url.encode()).digest()
            c1 = QColor.fromHsv(digest[0] * 360 // 256, 150, 200)
            c2 = QColor.fromHsv((digest[0] * 360 // 256 + 40) % 360, 200, 90)
            image = QImage(w, h, QImage.Format.Format_RGB32)
            p = QPainter(image)
            grad = QLinearGradient(0, 0, w, h)
            grad.setColorAt(0, c1)
            grad.setColorAt(1, c2)
            p.fillRect(0, 0, w, h, grad)
            p.setPen(QColor(255, 255, 255, 230))
            font = QFont("Segoe UI", 22 if not wide else 30)
            font.setBold(True)
            p.setFont(font)
            name = url.rstrip("/").split("/")[-1 if not wide else -2].replace(".png", "").replace("-", " ").title()
            p.drawText(QRect(16, 16, w - 32, h - 32), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), name)
            p.end()
            image.save(str(path), "PNG")
        return path

    def import_file(self, url: str, source: Path) -> Path | None:
        return None

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._root.glob("*.png"))

    def clear(self) -> None:
        for p in self._root.glob("*.png"):
            p.unlink(missing_ok=True)

    def prune(self) -> int:
        return 0


class FakeCatalog:
    def __init__(self, client: FakeClient, events: ev.EventBus) -> None:
        self._client = client
        self._events = events
        self._wishlist: dict[str, GameSummary] = {}
        self._details: dict[str, GameDetails] = {}

    def count(self) -> int:
        return len(self._client.games)

    def last_synced(self) -> str:
        return utc_now_iso()

    def needs_sync(self, max_age: Any) -> bool:
        return False

    def sync(self, *, full: bool = False, token: CancelToken, on_progress: Any = None) -> int:
        for i in range(1, 4):
            token.sleep(0.1)
            self._events.publish(ev.CatalogSyncProgress(i, 3))
        self._events.publish(ev.CatalogUpdated(self.count(), 0))
        return 0

    def search(self, query: str, *, genre: str = "", limit: int = 200) -> list[GameSummary]:
        tokens = normalize_title(query).split()
        out = [g.copy() for g in self._client.games if all(t in normalize_title(g.title) for t in tokens)]
        if genre:
            out = [g for g in out if g.primary_genre.casefold() == genre.casefold()]
        return out[:limit]

    def get(self, slug: str) -> GameSummary | None:
        return next((g.copy() for g in self._client.games if g.slug == slug), None)

    def match_title(self, name: str) -> GameSummary | None:
        n = normalize_title(name)
        return next((g.copy() for g in self._client.games if normalize_title(g.title) == n), None)

    def upsert(self, games: list[GameSummary]) -> int:
        return 0

    def cached_details(self, slug: str) -> GameDetails | None:
        d = self._details.get(slug)
        return d.copy() if d else None

    def details(self, slug: str, *, max_age: Any = None, token: CancelToken | None = None) -> GameDetails:
        d = self._client.game_details(slug)
        self._details[slug] = d
        return d.copy()

    def wishlist(self) -> list[GameSummary]:
        return [g.copy() for g in self._wishlist.values()]

    def is_wishlisted(self, slug: str) -> bool:
        return slug in self._wishlist

    def set_wishlisted(self, game: GameSummary, wishlisted: bool) -> None:
        if wishlisted:
            self._wishlist[game.slug] = game.copy()
        else:
            self._wishlist.pop(game.slug, None)
        self._events.publish(ev.WishlistChanged(game.slug, wishlisted))


class FakeLibrary:
    def __init__(self, events: ev.EventBus, root: Path, client: FakeClient) -> None:
        self._events = events
        self._root = root
        root.mkdir(parents=True, exist_ok=True)
        self._games: dict[str, InstalledGame] = {}
        picks = [(0, True, 3600 * 41, "v1.1.0", False), (4, True, 600, "v1.0.0", True), (8, False, 0, "", False),
                 (12, True, 3600 * 300, "v1.1.0", False), (19, True, 120, "v1.0.0", True)]
        for idx, managed, playtime, version, update in picks:
            g = client.games[idx]
            path = root / g.title.replace(":", "")
            path.mkdir(parents=True, exist_ok=True)
            install_id = g.slug if managed else f"local:{path.name.casefold()}"
            self._games[install_id] = InstalledGame(
                install_id=install_id, title=g.title, path=str(path), library_root=str(root),
                slug=g.slug if managed else "", managed=managed, version=version,
                installed_at="2026-09-01T12:00:00+00:00", executable=f"{g.title.split(':')[0]}.exe" if idx != 19 else "",
                cover_url=g.cover_url, genres=[g.primary_genre], playtime_seconds=playtime,
                last_played="2026-10-05T20:00:00+00:00" if playtime else "", size_bytes=g.size_bytes,
                latest_version="v1.1.0" if update else version, update_available=update, has_redist=idx == 4,
                favorite=idx == 12,
            )

    def scan(self, *, token: CancelToken | None = None) -> list[InstalledGame]:
        time.sleep(LATENCY)
        self._events.publish(ev.LibraryChanged())
        return self.games()

    def games(self, *, include_hidden: bool = True) -> list[InstalledGame]:
        return [g.copy() for g in self._games.values() if include_hidden or not g.hidden]

    def get(self, install_id: str) -> InstalledGame | None:
        g = self._games.get(install_id)
        return g.copy() if g else None

    def find_by_slug(self, slug: str) -> InstalledGame | None:
        return next((g.copy() for g in self._games.values() if g.slug and g.slug == slug), None)

    def find_by_path(self, path: str) -> InstalledGame | None:
        return next((g.copy() for g in self._games.values() if g.path == path), None)

    def executable_candidates(self, install_id: str) -> list[str]:
        title = self._games[install_id].title.split(":")[0]
        return [f"{title}.exe", "Launcher.exe", r"bin\x64\Game-Win64-Shipping.exe"]

    def _change(self, install_id: str, **changes: Any) -> None:
        self._games[install_id] = replace(self._games[install_id], **changes)
        self._events.publish(ev.LibraryChanged(frozenset({install_id})))

    def register_install(self, result: Any, request: Any) -> InstalledGame:
        raise NotImplementedError

    def adopt(self, install_id: str, *, slug: str = "", title: str = "") -> InstalledGame:
        g = self._games.pop(install_id)
        new_id = slug or install_id
        self._games[new_id] = replace(g, install_id=new_id, slug=slug, managed=True, title=title or g.title)
        self._events.publish(ev.LibraryChanged())
        return self._games[new_id].copy()

    def set_executable(self, install_id: str, relative_path: str) -> None:
        self._change(install_id, executable=relative_path)

    def set_launch_options(self, install_id: str, *, args: str = "", run_as_admin: bool = False) -> None:
        self._change(install_id, launch_args=args, run_as_admin=run_as_admin)

    def rename(self, install_id: str, title: str) -> None:
        self._change(install_id, title=title)

    def set_favorite(self, install_id: str, favorite: bool) -> None:
        self._change(install_id, favorite=favorite)

    def set_hidden(self, install_id: str, hidden: bool) -> None:
        self._change(install_id, hidden=hidden)

    def mark_redist_installed(self, install_id: str) -> None:
        self._change(install_id, redist_installed=True)

    def record_play_session(self, install_id: str, started_at: str, ended_at: str, seconds: int) -> None:
        g = self._games[install_id]
        self._change(install_id, playtime_seconds=g.playtime_seconds + seconds, last_played=ended_at)

    def set_update_state(self, install_id: str, *, latest_version: str, available: bool) -> None:
        self._change(install_id, latest_version=latest_version, update_available=available)

    def compute_size(self, install_id: str, *, token: CancelToken | None = None) -> int:
        time.sleep(LATENCY)
        return self._games[install_id].size_bytes or 0

    def uninstall(self, install_id: str, *, token: CancelToken | None = None) -> None:
        time.sleep(LATENCY * 3)
        g = self._games.pop(install_id)
        self._events.publish(ev.GameUninstalled(install_id, g.title))
        self._events.publish(ev.LibraryChanged())


class FakeLauncher:
    def __init__(self, library: FakeLibrary, events: ev.EventBus) -> None:
        self._library = library
        self._events = events
        self._running: set[str] = set()

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def launch(self, install_id: str) -> None:
        g = self._library.get(install_id)
        if g is None or not g.executable:
            raise ExecutableNotSetError()
        self._running.add(install_id)
        self._events.publish(ev.GameLaunched(install_id, g.title))

    def is_running(self, install_id: str) -> bool:
        return install_id in self._running

    def running(self) -> set[str]:
        return set(self._running)

    def stop(self, install_id: str) -> None:
        if install_id in self._running:
            self._running.discard(install_id)
            g = self._library.get(install_id)
            self._library.record_play_session(install_id, utc_now_iso(), utc_now_iso(), 60)
            self._events.publish(ev.GameExited(install_id, g.title if g else install_id, 60))

    def run_redist(self, install_id: str, *, token: CancelToken | None = None) -> int:
        self._library.mark_redist_installed(install_id)
        return 2

    def open_folder(self, install_id: str) -> None:
        pass


class FakeDownloads:
    """Simulates a progressing queue on a background thread."""

    def __init__(self, events: ev.EventBus, client: FakeClient, library_root: Path) -> None:
        self._events = events
        self._lock = threading.RLock()
        self._jobs: dict[str, DownloadJob] = {}
        self._stop = threading.Event()
        g = client.games
        self._add(g[2], JobState.DOWNLOADING, bytes_done=int(0.37 * (g[2].size_bytes or 1)), speed=18.4 * 1024**2)
        self._add(g[3], JobState.QUEUED)
        self._add(g[6], JobState.PAUSED, bytes_done=int(0.62 * (g[6].size_bytes or 1)))
        self._add(g[9], JobState.WAITING, status="Waiting 42s (rate limited)")
        failed = self._add(g[10], JobState.FAILED)
        failed.error = "This download is hosted on an external file host. Open it in your browser, then import the archive."
        failed.error_kind = ErrorKind.EXTERNAL_HOST
        failed.error_url = "https://ankergames.net/download/example"
        self._add(g[11], JobState.EXTRACTING, phase=0.48)
        self._add(g[0], JobState.COMPLETED, install_path=str(library_root / "Hollow Knight"))
        self._library_root = str(library_root)
        self._thread = threading.Thread(target=self._tick, name="fake-downloads", daemon=True)

    def _add(self, game: GameSummary, state: JobState, *, bytes_done: int = 0, speed: float = 0.0, phase: float = 0.0,
             status: str = "", install_path: str = "") -> DownloadJob:
        job = DownloadJob(
            id=uuid.uuid4().hex, slug=game.slug, title=game.title,
            option=DownloadOption(1, "Direct", DownloadKind.FULL), library_root="C:/Games",
            cover_url=game.cover_url, state=state, position=len(self._jobs), bytes_done=bytes_done,
            bytes_total=game.size_bytes, speed_bps=speed, eta_seconds=((game.size_bytes or 0) - bytes_done) / speed if speed else None,
            phase_progress=phase, status_text=status, install_path=install_path, filename=f"{game.slug}.zip",
        )
        self._jobs[job.id] = job
        return job

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stop.set()

    def _tick(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                for job in self._jobs.values():
                    if job.state is JobState.DOWNLOADING and job.bytes_total:
                        job.bytes_done = min(job.bytes_total, job.bytes_done + int(job.speed_bps * 0.5))
                        job.eta_seconds = (job.bytes_total - job.bytes_done) / job.speed_bps
                        self._events.publish(ev.JobUpdated(job.copy()))
                    elif job.state is JobState.EXTRACTING:
                        job.phase_progress = min(1.0, job.phase_progress + 0.01)
                        self._events.publish(ev.JobUpdated(job.copy()))

    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            return sorted((j.copy() for j in self._jobs.values()), key=lambda j: j.position)

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.copy() if j else None

    def active_jobs(self) -> list[DownloadJob]:
        return [j for j in self.jobs() if j.state.is_active]

    def job_for(self, slug: str) -> DownloadJob | None:
        return next((j for j in self.jobs() if j.slug == slug and not j.state.is_finished), None)

    def _set(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for k, v in changes.items():
                setattr(job, k, v)
            snapshot = job.copy()
        self._events.publish(ev.JobUpdated(snapshot))

    def enqueue(self, *, slug: str, title: str, option: DownloadOption, library_root: str | None = None,
                cover_url: str = "", version: str = "", source_updated_date: str = "",
                genres: list[str] | None = None) -> DownloadJob:
        with self._lock:
            existing = self.job_for(slug)
            if existing:
                return existing
            job = DownloadJob(id=uuid.uuid4().hex, slug=slug, title=title, option=option,
                              library_root=library_root or self._library_root, cover_url=cover_url,
                              state=JobState.DOWNLOADING, bytes_total=2 * 1024**3, speed_bps=25 * 1024**2,
                              position=len(self._jobs))
            self._jobs[job.id] = job
        self._events.publish(ev.JobAdded(job.copy()))
        return job.copy()

    def import_archive(self, archive_path: str, *, slug: str, title: str, kind: DownloadKind = DownloadKind.FULL,
                       library_root: str | None = None, cover_url: str = "", version: str = "") -> DownloadJob:
        job = DownloadJob(id=uuid.uuid4().hex, slug=slug, title=title, option=DownloadOption(0, "Local archive", kind),
                          library_root=library_root or self._library_root, cover_url=cover_url,
                          state=JobState.EXTRACTING, imported_archive=True, archive_path=archive_path,
                          position=len(self._jobs))
        with self._lock:
            self._jobs[job.id] = job
        self._events.publish(ev.JobAdded(job.copy()))
        return job.copy()

    def pause(self, job_id: str) -> None:
        self._set(job_id, state=JobState.PAUSED, speed_bps=0.0)

    def resume(self, job_id: str) -> None:
        self._set(job_id, state=JobState.DOWNLOADING, speed_bps=20 * 1024**2)

    def retry(self, job_id: str) -> None:
        self._set(job_id, state=JobState.QUEUED, error="", error_kind=None)

    def cancel(self, job_id: str) -> None:
        self._set(job_id, state=JobState.CANCELLED, speed_bps=0.0)

    def install(self, job_id: str) -> None:
        self._set(job_id, state=JobState.EXTRACTING, phase_progress=0.0)

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
        self._events.publish(ev.JobRemoved(job_id))

    def clear_finished(self) -> None:
        with self._lock:
            for job_id in [j.id for j in self._jobs.values() if j.state.is_finished]:
                self._jobs.pop(job_id)
        self._events.publish(ev.QueueChanged())

    def move(self, job_id: str, index: int) -> None:
        with self._lock:
            ordered = sorted(self._jobs.values(), key=lambda j: j.position)
            job = self._jobs[job_id]
            ordered.remove(job)
            ordered.insert(max(0, min(index, len(ordered))), job)
            for pos, j in enumerate(ordered):
                j.position = pos
        self._events.publish(ev.QueueChanged())

    def pause_all(self) -> None:
        for j in self.jobs():
            if j.state in (JobState.DOWNLOADING, JobState.QUEUED, JobState.WAITING):
                self.pause(j.id)

    def resume_all(self) -> None:
        for j in self.jobs():
            if j.state is JobState.PAUSED:
                self.resume(j.id)


class FakeUpdates:
    def __init__(self, library: FakeLibrary, events: ev.EventBus) -> None:
        self._library = library
        self._events = events

    def last_checked(self) -> str:
        return utc_now_iso()

    def pending(self) -> list[GameUpdate]:
        return [GameUpdate(g.install_id, g.slug, g.title, g.version, g.latest_version,
                           full_option=DownloadOption(1, "Direct"))
                for g in self._library.games() if g.update_available]

    def check(self, *, token: CancelToken, install_ids: list[str] | None = None, on_progress: Any = None) -> list[GameUpdate]:
        token.sleep(0.3)
        ups = self.pending()
        self._events.publish(ev.UpdatesFound(tuple(ups)))
        return ups


class FakeAppUpdates:
    def check(self, *, token: CancelToken | None = None) -> AppRelease | None:
        return None


class _Null:
    """Accepts any call; used for services the UI should not call directly."""

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


class FakeContext:
    def __init__(self, home: Path) -> None:
        home = Path(home)
        self.paths = AppPaths.under(home).ensure()
        self.events = ev.EventBus()
        self.settings = SettingsStore(self.paths.settings_file, self.events)
        self.settings.update(first_run_completed=True, library_dirs=[str(home / "Games")],
                             default_library=str(home / "Games"))
        self.db = Database(self.paths.database_file)
        self.runner = TaskRunner(max_workers=6, name="fake-task")
        self.client = FakeClient()
        self.http = self.client.http
        self.livewire = _Null()
        self.auth = FakeAuth(self.client, self.events)
        self.catalog = FakeCatalog(self.client, self.events)
        self.images = FakeImageCache(self.paths.images_dir)
        self.shortcuts = _Null()
        self.extractor = _Null()
        self.installer = _Null()
        self.library = FakeLibrary(self.events, home / "Games", self.client)
        self.launcher = FakeLauncher(self.library, self.events)
        self.rate_limiter = _Null()
        self.resolver = _Null()
        self.downloads = FakeDownloads(self.events, self.client, home / "Games")
        self.updates = FakeUpdates(self.library, self.events)
        self.app_updates = FakeAppUpdates()

    def start(self) -> None:
        self.downloads.start()

    def shutdown(self) -> None:
        self.downloads.shutdown()
        self.runner.shutdown(wait=False)
        self.db.close()


def screenshot(widget: Any, name: str, size: tuple[int, int] = (1280, 800)) -> Path:
    """Render ``widget`` offscreen to ``<repo>/build/screens/<name>.png`` and return the path."""
    from PyQt6.QtWidgets import QApplication

    out = Path(__file__).resolve().parent.parent / "build" / "screens"
    out.mkdir(parents=True, exist_ok=True)
    widget.resize(*size)
    widget.show()
    app = QApplication.instance()
    deadline = time.time() + 1.5
    while time.time() < deadline:  # let async loads + image decodes land
        app.processEvents()
        time.sleep(0.02)
    path = out / f"{name}.png"
    from PyQt6.QtGui import QColor, QPixmap

    from anker_client.ui.theme import palette

    pixmap = QPixmap(widget.size())
    pixmap.fill(QColor(palette.current().bg))  # pages are transparent; paint the window background
    widget.render(pixmap)
    pixmap.save(str(path))
    return path


if __name__ == "__main__":  # manual UI review on fake data
    import os
    import sys
    import tempfile

    from PyQt6.QtWidgets import QApplication

    from anker_client.ui.bridge import QtEventBridge
    from anker_client.ui.main_window import MainWindow
    from anker_client.ui.theme.manager import ThemeManager

    app = QApplication(sys.argv)
    ctx = FakeContext(Path(tempfile.mkdtemp()))
    theme = ThemeManager(app)
    theme.apply(os.environ.get("ANKER_THEME", "midnight"))
    bridge = QtEventBridge(ctx.events)
    window = MainWindow(ctx, bridge, theme)  # type: ignore[arg-type]
    window.show()
    ctx.start()
    code = app.exec()
    ctx.shutdown()
    sys.exit(code)

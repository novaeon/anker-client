"""Local index of the whole store + cached game details + wishlist.

* ``sync``: crawl ``client.browse(page=N, sort=NEWEST)`` from page 1 until an
  empty/last page (≈37 pages today). Sleep ``0.35 s`` between pages
  (cancellable). Upsert rows (``title_norm = normalize_title(title)``,
  ``listing_rank`` = global position, ``last_seen`` = now; ``first_seen`` kept).
  Incremental mode (``full=False``) stops after 2 consecutive pages whose slugs
  were all already known AND the catalog was fully synced within 7 days;
  otherwise behaves like full. When an incremental crawl stops early, the ranks
  of rows it did not reach are shifted by how far the last crawled, previously
  ranked game moved (games added since the last sync — including ones first
  seen through ``upsert`` with no rank — push the rest down) so the
  newest-first order stays consistent.
  A crawl that reaches the natural last page deletes rows that existed when
  it started and were not seen in it (rows written meanwhile by ``upsert`` or
  ``details`` are kept) — never on a partial crawl (cancelled, failed,
  stopped early, pagination that repeats itself) and never when the crawl saw
  less than half of the known rows (protects the index against a broken
  listing page).
  Publishes ``CatalogSyncProgress`` per page and ``CatalogUpdated`` when the
  sync ends (also after a failure, so progress indicators always stop);
  stores ``meta['catalog_synced_at']`` (and ``catalog_full_synced_at`` after a
  complete crawl). Concurrent ``sync`` calls are serialised.
* ``search``: local, instant (in-memory index, rebuilt lazily after writes).
  Tokenise ``normalize_title(query)``; every token must be a substring of
  ``title_norm``; rank exact > prefix > word-prefix > substring, then
  ``listing_rank`` (unranked rows last), then title. Optional genre filter on
  ``primary_genre`` (name or slug, compared normalised). An empty query
  returns ``[]`` unless a genre is given (then every game of that genre).
* ``match_title``: best catalog entry for a folder/game name (exact
  ``title_norm`` or slug match first; then the name with trailing
  version/edition noise stripped — e.g. "v1.2", "Build 123", "Deluxe Edition",
  "GOTY", "[FitGirl Repack]", "+ 3 DLCs" — then the unique catalog title that
  extends the stripped name at a word boundary ("The Witcher 3" → "The Witcher
  3: Wild Hunt"); finally, only for names that carried such noise, the longest
  catalog title ending in a sequel number that the name starts with ("Dark
  Souls III Fire Fades Edition" → "Dark Souls III"), unless another sequel
  number follows it). A franchise title is never matched by prefix alone
  ("Doom Eternal" is not "DOOM", "Elden Ring Nightreign" is not "Elden Ring"):
  a wrong match would make an unrelated folder count as that store game's
  install. Returns ``None`` when ambiguous or unsure.
* ``upsert``: rows seen elsewhere (browse/search pages). Never blanks a known
  field with an empty one and never changes ``listing_rank`` of known rows.
* ``details``: cached ``GameDetails`` from ``game_details`` when younger than
  ``max_age``; otherwise fetch via ``client.game_details`` and store
  (concurrent requests for one slug fetch once). On network failure return the
  stale cache if any (else re-raise); ``NotFoundError`` is always re-raised.
  A failure to write the cache is logged; the fresh details are still returned.
* Wishlist: CRUD on ``wishlist`` (newest first, enriched from the catalog),
  publishes ``WishlistChanged`` when the state actually changes.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection, Row

from anker_client.core.db import Database
from anker_client.core.errors import AnkerError, NotFoundError, OperationCancelled
from anker_client.core.events import CatalogSyncProgress, CatalogUpdated, EventBus, WishlistChanged
from anker_client.core.models import GameDetails, GameSummary, ListingPage, SortOrder
from anker_client.core.paths import normalize_title
from anker_client.core.tasks import CancelToken
from anker_client.site.client import AnkerGamesClient
from anker_client.site.parsers import PARSER_VERSION

log = logging.getLogger(__name__)

PAGE_DELAY_SECONDS = 0.35
INCREMENTAL_KNOWN_PAGES = 2
INCREMENTAL_MAX_FULL_AGE = timedelta(days=7)
MAX_SYNC_PAGES = 400  # hard stop should pagination never end

META_SYNCED_AT = "catalog_synced_at"
META_FULL_SYNCED_AT = "catalog_full_synced_at"
META_PAGES = "catalog_pages"

_UNRANKED = 1 << 40
_SQL_CHUNK = 500
_MIN_PREFIX_LEN = 3
_SEQUEL_RE = re.compile(r"\d+|i{1,3}|iv|v|vi{1,3}|ix|x")

_UPSERT_SQL = """
INSERT INTO catalog(slug, title, title_norm, cover_url, primary_genre, year, size_text, size_bytes,
                    listing_rank, first_seen, last_seen)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(slug) DO UPDATE SET
    title         = CASE WHEN excluded.title <> '' THEN excluded.title ELSE catalog.title END,
    title_norm    = CASE WHEN excluded.title <> '' THEN excluded.title_norm ELSE catalog.title_norm END,
    cover_url     = CASE WHEN excluded.cover_url <> '' THEN excluded.cover_url ELSE catalog.cover_url END,
    primary_genre = CASE WHEN excluded.primary_genre <> '' THEN excluded.primary_genre
                         ELSE catalog.primary_genre END,
    year          = COALESCE(excluded.year, catalog.year),
    size_text     = CASE WHEN excluded.size_text <> '' THEN excluded.size_text ELSE catalog.size_text END,
    size_bytes    = COALESCE(excluded.size_bytes, catalog.size_bytes),
    listing_rank  = COALESCE(excluded.listing_rank, catalog.listing_rank),
    last_seen     = excluded.last_seen
"""

# --- match_title noise -----------------------------------------------------------------

_EDITION_NAMES = (
    "game of the year",
    "goty",
    "digital deluxe",
    "super deluxe",
    "ultimate deluxe",
    "deluxe",
    "ultimate",
    "gold",
    "complete",
    "definitive",
    "premium",
    "special",
    "collector'?s",
    "enhanced",
    "standard",
    "legendary",
    "anniversary",
    "royal",
    "platinum",
    "supporter'?s",
    "limited",
    "extended",
    "expanded",
    "maximum",
    "director'?s cut",
)
_SEP = r"[\s_\-:,.]"
# Applied to the casefolded raw name (dots still present, so "1.0.3" is a version but "Gate 3" is not).
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s*[(\[{][^()\[\]{}]*[)\]}]\s*$"),  # trailing "(…)", "[…]", "{…}"
    re.compile(rf"{_SEP}+(?:v|ver\.?|version)\s*\d+(?:[._]\d+)*[a-z]?$"),  # v1.5.78, version 2
    re.compile(rf"{_SEP}+\d+(?:\.\d+)+[a-z]?$"),  # bare dotted version 1.0.3
    re.compile(rf"{_SEP}+build\s*[.#]?\s*\d+$"),  # Build 123
    re.compile(rf"{_SEP}*(?:\+|incl\.?|including|with)\s*(?:\d+\s*|all\s+)?(?:dlcs?|updates?|bonus(?:es)?)$"),
    re.compile(rf"{_SEP}+(?:{'|'.join(_EDITION_NAMES)})(?:\s+edition)?$"),  # GOTY, Deluxe Edition
    re.compile(rf"{_SEP}+[a-z0-9'’]+\s+edition$"),  # any other "<Word> Edition"
    re.compile(
        rf"{_SEP}+(?:repack|fitgirl|dodi|elamigos|gog|codex|plaza|skidrow|empress|tenoke|rune|flt|razor1911"
        r"|portable|x64|x86|win64|multi\d*|pc)$"
    ),
)


def _strip_noise_once(text: str) -> str:
    for pattern in _NOISE_PATTERNS:
        stripped = pattern.sub("", text).rstrip(" _-:,.+")
        if stripped and stripped != text:
            return stripped
    return text


def _noise_variants(name: str) -> list[str]:
    """``name`` normalised, then progressively stripped of trailing noise (normalised, unique, in order)."""
    variants: list[str] = []
    text = " ".join(str(name).casefold().split())
    for _ in range(12):  # each pass strips one suffix; real names never need more
        norm = normalize_title(text)
        if norm and norm not in variants:
            variants.append(norm)
        stripped = _strip_noise_once(text)
        if stripped == text:
            break
        text = stripped
    return variants


def _slugify(norm: str) -> str:
    return "-".join(norm.split())


# --- time helpers ----------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- in-memory index -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    summary: GameSummary  # never handed out — callers get copies
    norm: str
    words: tuple[str, ...]
    rank: int
    genre: str  # normalised primary genre


@dataclass(slots=True)
class _SlugLock:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class _Index:
    def __init__(self, entries: list[_Entry]) -> None:
        entries.sort(key=lambda e: (e.rank, e.norm, e.summary.slug))
        self.entries = entries
        self.by_slug = {e.summary.slug: e for e in entries}
        self.by_norm: dict[str, list[_Entry]] = {}
        for entry in entries:
            self.by_norm.setdefault(entry.norm, []).append(entry)


def _summary_from_row(row: Row) -> GameSummary:
    return GameSummary(
        slug=row["slug"],
        title=row["title"],
        cover_url=row["cover_url"] or "",
        primary_genre=row["primary_genre"] or "",
        year=row["year"],
        size_text=row["size_text"] or "",
        size_bytes=row["size_bytes"],
    )


def _match_tier(entry: _Entry, query: str, tokens: Sequence[str]) -> int:
    if not tokens:
        return 0
    if entry.norm == query:
        return 0
    if entry.norm.startswith(query):
        return 1
    if all(any(word.startswith(tok) for word in entry.words) for tok in tokens):
        return 2
    return 3


def _title_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part) or slug


class CatalogService:
    def __init__(
        self,
        db: Database,
        client: AnkerGamesClient,
        events: EventBus,
        *,
        page_delay: float = PAGE_DELAY_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._client = client
        self._events = events
        self._page_delay = max(0.0, page_delay)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._index_lock = threading.Lock()
        self._index: _Index | None = None
        self._generation = 0
        self._sync_lock = threading.Lock()
        self._slug_locks_guard = threading.Lock()
        self._slug_locks: dict[str, _SlugLock] = {}

    # --- index ----------------------------------------------------------------------
    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM catalog", default=0))

    def last_synced(self) -> str:
        """ISO timestamp of the last completed sync, "" if never."""
        return self._db.get_meta(META_SYNCED_AT) or ""

    def needs_sync(self, max_age: timedelta) -> bool:
        if self.count() == 0:
            return True
        last = _parse_iso(self.last_synced())
        if last is None:
            return True
        return self._clock() - last >= max_age

    def sync(
        self,
        *,
        full: bool = False,
        token: CancelToken,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Returns the number of newly discovered games."""
        while not self._sync_lock.acquire(timeout=0.2):
            token.raise_if_cancelled()
        try:
            return self._sync_locked(full=full, token=token, on_progress=on_progress)
        finally:
            self._sync_lock.release()

    def search(self, query: str, *, genre: str = "", limit: int = 200) -> list[GameSummary]:
        if limit <= 0:
            return []
        normalized = normalize_title(query)
        tokens = normalized.split()
        genre_norm = normalize_title(genre)
        if not tokens and not genre_norm:
            return []
        scored: list[tuple[int, int, str, _Entry]] = []
        for entry in self._get_index().entries:
            if genre_norm and entry.genre != genre_norm:
                continue
            if tokens and not all(tok in entry.norm for tok in tokens):
                continue
            scored.append((_match_tier(entry, normalized, tokens), entry.rank, entry.norm, entry))
        scored.sort(key=lambda item: item[:3])
        return [item[3].summary.copy() for item in scored[:limit]]

    def get(self, slug: str) -> GameSummary | None:
        entry = self._get_index().by_slug.get((slug or "").strip())
        return entry.summary.copy() if entry else None

    def match_title(self, name: str) -> GameSummary | None:
        variants = _noise_variants(name)
        if not variants:
            return None
        index = self._get_index()
        for variant in variants:
            found, ambiguous = self._exact_match(index, variant)
            if found is not None:
                return found.summary.copy()
            if ambiguous:
                return None
        core = variants[-1]  # the name with every recognised suffix stripped
        if len(core) >= _MIN_PREFIX_LEN:
            extending = [e for e in index.entries if e.norm.startswith(core + " ")]
            if len(extending) == 1:
                return extending[0].summary.copy()
            if len(extending) > 1:
                log.debug("match_title(%r): ambiguous prefix %r (%d candidates)", name, core, len(extending))
                return None
        if len(variants) == 1:
            # No recognised noise: extra words after a catalog title name another game
            # ("Doom Eternal", "Far Cry 3 Blood Dragon"), not an edition of it.
            return None
        contained = self._longest_title_prefix(index, variants[0])
        return contained.summary.copy() if contained is not None else None

    def upsert(self, games: list[GameSummary]) -> int:
        """Insert/update listing rows seen elsewhere (browse pages, search results). Returns new count."""
        unique: dict[str, GameSummary] = {}
        for game in games or []:
            slug = (game.slug or "").strip()
            if slug and slug not in unique:
                unique[slug] = game
        if not unique:
            return 0
        return self._write_summaries(list(unique.values()), None, _iso(self._clock()))

    # --- details --------------------------------------------------------------------
    def cached_details(self, slug: str) -> GameDetails | None:
        cached, _fetched_at = self._cached_details_row((slug or "").strip())
        return cached

    def details(
        self, slug: str, *, max_age: timedelta = timedelta(hours=6), token: CancelToken | None = None
    ) -> GameDetails:
        slug = (slug or "").strip()
        if not slug:
            raise NotFoundError()
        cached, fetched_at = self._cached_details_row(slug)
        if cached is not None and self._is_fresh(fetched_at, max_age):
            return cached
        with self._slug_lock(slug, token):
            # Another thread may have fetched it while we waited for the lock.
            cached, fetched_at = self._cached_details_row(slug)
            if cached is not None and self._is_fresh(fetched_at, max_age):
                return cached
            try:
                fresh = self._client.game_details(slug, token=token)
            except (OperationCancelled, NotFoundError):
                raise
            except AnkerError as exc:
                if cached is None:
                    raise
                log.info("Using cached details for %s (refresh failed: %s)", slug, exc)
                return cached
            try:
                self._store_details(slug, fresh)
            except sqlite3.Error:  # e.g. the database is locked or the disk is full: still show the page
                log.warning("Could not cache the details of %s", slug, exc_info=True)
            return fresh.copy()

    # --- wishlist -------------------------------------------------------------------
    def wishlist(self) -> list[GameSummary]:
        rows = self._db.query(
            """
            SELECT w.slug AS slug,
                   COALESCE(NULLIF(c.title, ''), NULLIF(w.title, ''), w.slug) AS title,
                   COALESCE(NULLIF(c.cover_url, ''), w.cover_url, '') AS cover_url,
                   COALESCE(c.primary_genre, '') AS primary_genre,
                   c.year AS year,
                   COALESCE(c.size_text, '') AS size_text,
                   c.size_bytes AS size_bytes
            FROM wishlist w LEFT JOIN catalog c ON c.slug = w.slug
            ORDER BY w.added_at DESC, w.rowid DESC
            """
        )
        return [_summary_from_row(row) for row in rows]

    def is_wishlisted(self, slug: str) -> bool:
        return self._db.query_one("SELECT 1 FROM wishlist WHERE slug = ?", ((slug or "").strip(),)) is not None

    def set_wishlisted(self, game: GameSummary, wishlisted: bool) -> None:
        slug = (game.slug or "").strip()
        if not slug:
            log.warning("Ignoring wishlist change for a game without slug: %r", game.title)
            return
        with self._db.transaction() as conn:
            existed = conn.execute("SELECT 1 FROM wishlist WHERE slug = ?", (slug,)).fetchone() is not None
            if wishlisted:
                conn.execute(
                    """
                    INSERT INTO wishlist(slug, title, cover_url, added_at) VALUES(?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE wishlist.title END,
                        cover_url = CASE WHEN excluded.cover_url <> '' THEN excluded.cover_url
                                         ELSE wishlist.cover_url END
                    """,
                    (slug, game.title or _title_from_slug(slug), game.cover_url or "", _iso(self._clock())),
                )
            else:
                conn.execute("DELETE FROM wishlist WHERE slug = ?", (slug,))
        if existed != wishlisted:
            self._events.publish(WishlistChanged(slug=slug, wishlisted=wishlisted))

    # ===================================================================================
    # internals
    # ===================================================================================

    # --- sync -----------------------------------------------------------------------
    def _sync_locked(
        self,
        *,
        full: bool,
        token: CancelToken,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> int:
        started = self._clock()
        old_ranks = self._rank_snapshot()
        known = set(old_ranks)
        incremental = not full and bool(known) and self._full_sync_is_recent(started)
        estimate = self._page_estimate()
        log.info("Catalog sync started (%s, %d known games)", "incremental" if incremental else "full", len(known))

        seen: set[str] = set()
        order: list[str] = []  # slugs in crawl (newest-first) order; index = new listing_rank
        new_total = 0
        pages_done = 0
        complete = False
        stopped_early = False
        consecutive_known = 0
        try:
            for page in range(1, MAX_SYNC_PAGES + 1):
                token.raise_if_cancelled()
                if page > 1:
                    token.sleep(self._page_delay)
                listing = self._fetch_page(page, token)
                if not listing.games:
                    complete = page > 1  # an empty first page means a broken listing, not an empty store
                    if not complete:
                        log.warning("The first catalog page has no games; keeping the existing index")
                    break
                fresh = self._unique_new(listing, seen)
                if not fresh:
                    log.warning("Catalog page %d repeated earlier pages; stopping the crawl", page)
                    break
                ranks = list(range(len(order), len(order) + len(fresh)))
                order.extend(g.slug for g in fresh)
                seen.update(g.slug for g in fresh)
                new_total += self._write_summaries(fresh, ranks, _iso(self._clock()))
                pages_done += 1
                total = listing.total_pages or (estimate if estimate and estimate >= pages_done else None)
                self._events.publish(CatalogSyncProgress(pages_done=pages_done, pages_total=total))
                if on_progress is not None:
                    on_progress(pages_done, total)
                if not listing.has_next or (listing.total_pages and page >= listing.total_pages):
                    complete = True
                    break
                if incremental:
                    all_known = all(g.slug in known for g in fresh)
                    consecutive_known = consecutive_known + 1 if all_known else 0
                    if consecutive_known >= INCREMENTAL_KNOWN_PAGES:
                        stopped_early = True
                        break
            else:
                log.warning("Catalog sync stopped after %d pages (page limit)", MAX_SYNC_PAGES)

            finished = self._clock()
            if complete:
                self._delete_unseen(known, seen)
                self._db.set_meta(META_FULL_SYNCED_AT, _iso(finished))
                if pages_done:
                    self._db.set_meta(META_PAGES, str(pages_done))
            elif stopped_early:
                self._shift_unseen_ranks(order, old_ranks)
            self._db.set_meta(META_SYNCED_AT, _iso(finished))
            log.info(
                "Catalog sync finished: %d pages, %d games seen, %d new%s",
                pages_done,
                len(seen),
                new_total,
                "" if complete else " (partial crawl)",
            )
            return new_total
        finally:
            self._invalidate_index()
            try:
                self._events.publish(CatalogUpdated(total_games=self.count(), new_games=new_total))
            except Exception:  # never mask the original error
                log.exception("Could not publish CatalogUpdated")

    def _fetch_page(self, page: int, token: CancelToken) -> ListingPage:
        try:
            return self._client.browse(page=page, sort=SortOrder.NEWEST, token=token)
        except NotFoundError:
            if page > 1:  # pages past the end answer 404
                return ListingPage(games=[], page=page, has_next=False)
            raise

    @staticmethod
    def _unique_new(listing: ListingPage, seen: set[str]) -> list[GameSummary]:
        fresh: dict[str, GameSummary] = {}
        for game in listing.games:
            slug = (game.slug or "").strip()
            if slug and slug not in seen and slug not in fresh:
                fresh[slug] = game if game.slug == slug else replace(game, slug=slug)
        return list(fresh.values())

    def _full_sync_is_recent(self, now: datetime) -> bool:
        last_full = _parse_iso(self._db.get_meta(META_FULL_SYNCED_AT))
        return last_full is not None and now - last_full < INCREMENTAL_MAX_FULL_AGE

    def _page_estimate(self) -> int | None:
        try:
            value = int(self._db.get_meta(META_PAGES) or 0)
        except ValueError:
            return None
        return value or None

    def _rank_snapshot(self) -> dict[str, int | None]:
        """``{slug: listing_rank}`` of every row (rank ``None`` for rows only seen through ``upsert``)."""
        return {row[0]: row[1] for row in self._db.query("SELECT slug, listing_rank FROM catalog")}

    def _delete_unseen(self, known: set[str], seen: set[str]) -> None:
        """Delete rows that existed when the crawl started but were not in it."""
        if not seen or len(seen) * 2 < len(known):
            log.warning(
                "Catalog crawl saw %d games but %d were known; not deleting anything", len(seen), len(known)
            )
            return
        # Only rows known at the start: a row upserted while the crawl ran (a game added to the site
        # after its page was crawled, a game opened from a link) is not stale.
        stale = sorted(known - seen)
        if not stale:
            return
        with self._db.transaction() as conn:
            for chunk in _chunks(stale, _SQL_CHUNK):
                conn.execute(f"DELETE FROM catalog WHERE slug IN ({','.join('?' * len(chunk))})", chunk)
        log.info("Removed %d games that are no longer listed", len(stale))

    def _shift_unseen_ranks(self, order: list[str], old_ranks: dict[str, int | None]) -> None:
        """Move the ranks of rows an early-stopped crawl did not reach by as much as the crawled part grew.

        The offset is measured on the last crawled game that already had a rank: everything below it
        moved by the same amount (new games, including ones known only through ``upsert``, were added
        above it; removed ones disappeared above it).
        """
        shift = len(order)  # nothing crawled had a rank: every crawled game is new to the ranking
        for new_rank in range(len(order) - 1, -1, -1):
            old_rank = old_ranks.get(order[new_rank])
            if old_rank is not None:
                shift = new_rank - old_rank
                break
        if shift == 0:
            return
        crawled = set(order)
        rows = self._db.query("SELECT slug, listing_rank FROM catalog WHERE listing_rank IS NOT NULL")
        updates = [(row[1] + shift, row[0]) for row in rows if row[0] not in crawled]
        if updates:
            self._db.executemany("UPDATE catalog SET listing_rank = ? WHERE slug = ?", updates)

    # --- writes ---------------------------------------------------------------------
    def _write_summaries(self, games: list[GameSummary], ranks: list[int] | None, seen_at: str) -> int:
        new = 0
        with self._db.transaction() as conn:
            for position, game in enumerate(games):
                rank = ranks[position] if ranks is not None else None
                if self._upsert_row(conn, game, rank, seen_at):
                    new += 1
        self._invalidate_index()
        return new

    @staticmethod
    def _upsert_row(conn: Connection, game: GameSummary, rank: int | None, seen_at: str) -> bool:
        """Upsert one row inside a transaction; True when the slug was not known before."""
        slug = game.slug.strip()
        existed = conn.execute("SELECT 1 FROM catalog WHERE slug = ?", (slug,)).fetchone() is not None
        title = (game.title or "").strip()
        if not title and not existed:
            title = _title_from_slug(slug)
        conn.execute(
            _UPSERT_SQL,
            (
                slug,
                title,
                normalize_title(title),
                game.cover_url or "",
                game.primary_genre or "",
                game.year,
                game.size_text or "",
                game.size_bytes,
                rank,
                seen_at,
                seen_at,
            ),
        )
        return not existed

    # --- details internals ----------------------------------------------------------
    def _cached_details_row(self, slug: str) -> tuple[GameDetails | None, str]:
        if not slug:
            return None, ""
        row = self._db.query_one("SELECT json, fetched_at FROM game_details WHERE slug = ?", (slug,))
        if row is None:
            return None, ""
        try:
            data = json.loads(row["json"])
            if data.get("parser_version") != PARSER_VERSION:
                # Parsed by an older parser that may have misread the page: fetch it again.
                return None, ""
            return GameDetails.from_dict(data), row["fetched_at"] or ""
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("Discarding unreadable cached details for %s: %s", slug, exc)
            return None, ""

    def _is_fresh(self, fetched_at: str, max_age: timedelta) -> bool:
        moment = _parse_iso(fetched_at)
        if moment is None or max_age <= timedelta(0):
            return False
        age = self._clock() - moment
        return timedelta(0) <= age < max_age

    def _store_details(self, slug: str, details: GameDetails) -> None:
        now = _iso(self._clock())
        if not details.fetched_at:
            details.fetched_at = now
        payload = json.dumps({**details.to_dict(), "parser_version": PARSER_VERSION}, ensure_ascii=False)
        keys = {slug, details.slug} if details.slug else {slug}
        inserted = False
        with self._db.transaction() as conn:
            for key in keys:
                conn.execute(
                    "INSERT INTO game_details(slug, json, fetched_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(slug) DO UPDATE SET json = excluded.json, fetched_at = excluded.fetched_at",
                    (key, payload, now),
                )
            # Games reached only through their page (links, library) become searchable too.
            if details.slug and details.title:
                exists = conn.execute("SELECT 1 FROM catalog WHERE slug = ?", (details.slug,)).fetchone()
                if exists is None:
                    self._upsert_row(conn, details.to_summary(), None, now)
                    inserted = True
        if inserted:
            self._invalidate_index()

    @contextmanager
    def _slug_lock(self, slug: str, token: CancelToken | None) -> Iterator[None]:
        """Serialise detail fetches per slug so concurrent callers hit the network once."""
        with self._slug_locks_guard:
            holder = self._slug_locks.setdefault(slug, _SlugLock())
            holder.users += 1
        try:
            while not holder.lock.acquire(timeout=0.25):
                if token is not None:
                    token.raise_if_cancelled()
            try:
                yield
            finally:
                holder.lock.release()
        finally:
            with self._slug_locks_guard:
                holder.users -= 1
                if holder.users == 0:
                    self._slug_locks.pop(slug, None)

    # --- match internals ------------------------------------------------------------
    @staticmethod
    def _exact_match(index: _Index, variant: str) -> tuple[_Entry | None, bool]:
        """(entry, ambiguous) for an exact title_norm or slug match."""
        slug = _slugify(variant)
        matches = index.by_norm.get(variant, [])
        if len(matches) == 1:
            return matches[0], False
        if len(matches) > 1:
            preferred = [m for m in matches if m.summary.slug == slug]
            return (preferred[0], False) if len(preferred) == 1 else (None, True)
        by_slug = index.by_slug.get(slug)
        return by_slug, False

    @staticmethod
    def _longest_title_prefix(index: _Index, text: str) -> _Entry | None:
        """The longest catalog title ending in a sequel number that ``text`` starts with (at a word boundary).

        Only numbered titles qualify ("Dark Souls III", "Cyberpunk 2077"): an unnumbered one is usually
        a franchise name that other games extend ("DOOM" → "DOOM Eternal"). Rejected when the rest of
        ``text`` starts with another sequel marker, or when two different games share that longest title.
        """
        best: list[_Entry] = []
        for entry in index.entries:
            if len(entry.norm) < _MIN_PREFIX_LEN or not text.startswith(entry.norm + " "):
                continue
            if not entry.words or not _SEQUEL_RE.fullmatch(entry.words[-1]):
                continue
            rest = text[len(entry.norm) + 1 :].split()
            if rest and _SEQUEL_RE.fullmatch(rest[0]):
                continue
            if not best or len(entry.norm) > len(best[0].norm):
                best = [entry]
            elif len(entry.norm) == len(best[0].norm):
                best.append(entry)
        return best[0] if len(best) == 1 else None

    # --- index internals ------------------------------------------------------------
    def _get_index(self) -> _Index:
        with self._index_lock:
            if self._index is not None:
                return self._index
            generation = self._generation
        rows = self._db.query(
            "SELECT slug, title, title_norm, cover_url, primary_genre, year, size_text, size_bytes, listing_rank "
            "FROM catalog"
        )
        entries = []
        for row in rows:
            norm = row["title_norm"] or normalize_title(row["title"])
            rank = row["listing_rank"]
            entries.append(
                _Entry(
                    summary=_summary_from_row(row),
                    norm=norm,
                    words=tuple(norm.split()),
                    rank=_UNRANKED if rank is None else int(rank),
                    genre=normalize_title(row["primary_genre"] or ""),
                )
            )
        index = _Index(entries)
        with self._index_lock:
            if self._generation == generation:  # no write happened while we were reading
                self._index = index
        return index

    def _invalidate_index(self) -> None:
        with self._index_lock:
            self._generation += 1
            self._index = None


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]

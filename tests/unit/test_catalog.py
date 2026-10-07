"""CatalogService: sync crawl, search, match_title, upsert, details cache, wishlist."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anker_client.core import events as ev
from anker_client.core.db import Database
from anker_client.core.errors import NetworkError, NotFoundError, OperationCancelled, SiteChangedError
from anker_client.core.models import GameDetails, GameSummary, ListingPage, SortOrder
from anker_client.core.tasks import CancelToken
from anker_client.services.catalog import CatalogService


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class RecordingToken(CancelToken):
    """Records ``sleep`` requests instead of sleeping (deterministic pacing tests)."""

    __slots__ = ("sleeps",)

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        super().sleep(0)


def game(slug: str, title: str | None = None, **kwargs) -> GameSummary:
    return GameSummary(slug=slug, title=title if title is not None else slug.replace("-", " ").title(), **kwargs)


class FakeClient:
    """Serves ``pages`` (lists of GameSummary) like ``AnkerGamesClient.browse``."""

    def __init__(self, pages: list[list[GameSummary]] | None = None) -> None:
        self.pages = pages or []
        self.browse_calls: list[int] = []
        self.sorts: list[SortOrder] = []
        self.fail_on_page: int | None = None
        self.not_found_past_end = False
        self.repeat_first_page = False
        self.total_pages: int | None = None
        self.details: dict[str, GameDetails] = {}
        self.details_error: Exception | None = None
        self.details_calls: list[str] = []
        self.details_delay = 0.0

    def set_games(self, games: list[GameSummary], per_page: int) -> None:
        self.pages = [games[i : i + per_page] for i in range(0, len(games), per_page)]

    def browse(self, *, page: int = 1, sort: SortOrder = SortOrder.NEWEST, genre: str | None = None,
               token: CancelToken | None = None) -> ListingPage:
        self.browse_calls.append(page)
        self.sorts.append(sort)
        if self.fail_on_page == page:
            raise NetworkError(status=503)
        if self.repeat_first_page:
            return ListingPage(games=[g.copy() for g in self.pages[0]], page=page, has_next=True)
        if page > len(self.pages):
            if self.not_found_past_end:
                raise NotFoundError()
            return ListingPage(games=[], page=page, has_next=False)
        return ListingPage(
            games=[g.copy() for g in self.pages[page - 1]],
            page=page,
            has_next=page < len(self.pages),
            total_pages=self.total_pages,
        )

    def game_details(self, slug: str, *, token: CancelToken | None = None) -> GameDetails:
        self.details_calls.append(slug)
        if self.details_delay:
            time.sleep(self.details_delay)
        if self.details_error is not None:
            raise self.details_error
        if slug not in self.details:
            raise NotFoundError()
        return self.details[slug].copy()


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "anker.db")
    yield database
    database.close()


@pytest.fixture
def bus() -> ev.EventBus:
    return ev.EventBus()


@pytest.fixture
def recorded(bus: ev.EventBus) -> list[ev.Event]:
    seen: list[ev.Event] = []
    bus.subscribe(ev.Event, seen.append)
    return seen


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def catalog(db: Database, client: FakeClient, bus: ev.EventBus, clock: Clock) -> CatalogService:
    return CatalogService(db, client, bus, page_delay=0, clock=clock)  # type: ignore[arg-type]


def ranks(db: Database) -> dict[str, int | None]:
    return {row["slug"]: row["listing_rank"] for row in db.query("SELECT slug, listing_rank FROM catalog")}


def seed(catalog: CatalogService, client: FakeClient, titles: list[str] | list[GameSummary]) -> None:
    games = [t if isinstance(t, GameSummary) else game(_slug(t), t) for t in titles]
    client.set_games(games, per_page=max(1, len(games)))
    catalog.sync(full=True, token=CancelToken())


def _slug(title: str) -> str:
    from anker_client.core.paths import normalize_title

    return "-".join(normalize_title(title).split())


# --- sync ----------------------------------------------------------------------------------


class TestSync:
    def test_full_sync_inserts_everything_with_global_ranks(self, catalog, client, db, recorded):
        games = [game(f"g{i}", f"Game {i}", primary_genre="Action", year=2020, size_text="1 GB", size_bytes=2**30)
                 for i in range(7)]
        client.set_games(games, per_page=3)

        new = catalog.sync(full=True, token=CancelToken())

        assert new == 7
        assert catalog.count() == 7
        assert client.browse_calls == [1, 2, 3]
        assert set(client.sorts) == {SortOrder.NEWEST}
        assert ranks(db) == {f"g{i}": i for i in range(7)}
        row = db.query_one("SELECT * FROM catalog WHERE slug = 'g4'")
        assert row["title_norm"] == "game 4"
        assert row["first_seen"] == row["last_seen"] == "2026-10-06T12:00:00+00:00"
        progress = [e for e in recorded if isinstance(e, ev.CatalogSyncProgress)]
        assert [p.pages_done for p in progress] == [1, 2, 3]
        updated = [e for e in recorded if isinstance(e, ev.CatalogUpdated)]
        assert updated == [ev.CatalogUpdated(total_games=7, new_games=7)]
        assert catalog.last_synced() == "2026-10-06T12:00:00+00:00"
        assert db.get_meta("catalog_full_synced_at") == "2026-10-06T12:00:00+00:00"

    def test_on_progress_and_total_pages(self, catalog, client):
        client.set_games([game(f"g{i}") for i in range(4)], per_page=2)
        client.total_pages = 2
        calls: list[tuple[int, int | None]] = []
        catalog.sync(full=True, token=CancelToken(), on_progress=lambda done, total: calls.append((done, total)))
        assert calls == [(1, 2), (2, 2)]

    def test_page_estimate_from_previous_sync(self, catalog, client):
        client.set_games([game(f"g{i}") for i in range(4)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        calls: list[tuple[int, int | None]] = []
        catalog.sync(full=True, token=CancelToken(), on_progress=lambda d, t: calls.append((d, t)))
        assert calls == [(1, 2), (2, 2)]

    def test_resync_keeps_first_seen_and_updates_fields(self, catalog, client, db, clock):
        client.set_games([game("a", "Alpha", cover_url="c1")], per_page=1)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(days=1)
        client.set_games([game("a", "Alpha Remastered", cover_url="c2")], per_page=1)

        assert catalog.sync(full=True, token=CancelToken()) == 0

        row = db.query_one("SELECT * FROM catalog WHERE slug = 'a'")
        assert row["title"] == "Alpha Remastered"
        assert row["title_norm"] == "alpha remastered"
        assert row["cover_url"] == "c2"
        assert row["first_seen"] == "2026-10-06T12:00:00+00:00"
        assert row["last_seen"] == "2026-10-07T12:00:00+00:00"

    def test_duplicate_slugs_in_crawl_are_ranked_once(self, catalog, client, db):
        client.pages = [[game("a"), game("b"), game("a")], [game("b"), game("c")]]
        assert catalog.sync(full=True, token=CancelToken()) == 3
        assert ranks(db) == {"a": 0, "b": 1, "c": 2}

    def test_complete_crawl_deletes_unseen_rows(self, catalog, client):
        client.set_games([game(f"g{i}") for i in range(6)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        client.set_games([game(f"g{i}") for i in range(5)], per_page=2)  # g5 removed from the site

        catalog.sync(full=True, token=CancelToken())

        assert catalog.count() == 5
        assert catalog.get("g5") is None

    def test_partial_crawl_never_deletes(self, catalog, client, db):
        client.set_games([game(f"g{i}") for i in range(6)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        first_sync = catalog.last_synced()
        client.set_games([game("new")] + [game(f"g{i}") for i in range(5)], per_page=2)
        client.fail_on_page = 3

        with pytest.raises(NetworkError):
            catalog.sync(full=True, token=CancelToken())

        assert catalog.count() == 7  # g5 kept, "new" added from the pages that were crawled
        assert catalog.get("new") is not None
        assert catalog.last_synced() == first_sync

    def test_failed_sync_still_publishes_catalog_updated(self, catalog, client, recorded):
        client.set_games([game("a")], per_page=1)
        client.fail_on_page = 1
        with pytest.raises(NetworkError):
            catalog.sync(full=True, token=CancelToken())
        assert any(isinstance(e, ev.CatalogUpdated) for e in recorded)

    def test_rows_written_during_the_crawl_are_not_deleted(self, catalog, client):
        client.set_games([game(f"g{i}") for i in range(4)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        client.set_games([game(f"g{i}") for i in range(3)], per_page=2)  # g3 removed from the site
        client.details["linked-game"] = GameDetails(slug="linked-game", title="Linked Game")
        original = client.browse

        def browse(**kwargs):
            if kwargs["page"] == 2:  # the store and a game page write rows while the crawl runs
                catalog.upsert([game("added-after-page-1")])
                catalog.details("linked-game")
            return original(**kwargs)

        client.browse = browse  # type: ignore[method-assign]
        catalog.sync(full=True, token=CancelToken())

        assert catalog.get("g3") is None
        assert catalog.get("added-after-page-1") is not None
        assert catalog.get("linked-game") is not None

    def test_crawl_seeing_less_than_half_does_not_delete(self, catalog, client):
        client.set_games([game(f"g{i}") for i in range(10)], per_page=5)
        catalog.sync(full=True, token=CancelToken())
        client.set_games([game("g0"), game("g1")], per_page=5)  # broken listing: only 2 games

        catalog.sync(full=True, token=CancelToken())

        assert catalog.count() == 10

    def test_404_past_the_end_is_the_last_page(self, catalog, client, db):
        client.pages = [[game("a"), game("b")], [game("c")]]
        client.not_found_past_end = True

        # make page 2 claim there is a next page so the crawler requests page 3 (→ 404)
        original = client.browse

        def browse(**kwargs):
            listing = original(**kwargs)
            if kwargs["page"] == 2:
                listing.has_next = True
            return listing

        client.browse = browse  # type: ignore[method-assign]
        assert catalog.sync(full=True, token=CancelToken()) == 3
        assert client.browse_calls == [1, 2, 3]
        assert db.get_meta("catalog_full_synced_at")

    def test_empty_first_page_keeps_the_index(self, catalog, client, db):
        client.set_games([game(f"g{i}") for i in range(4)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        full_synced = db.get_meta("catalog_full_synced_at")
        client.pages = []

        assert catalog.sync(full=True, token=CancelToken()) == 0

        assert catalog.count() == 4
        assert db.get_meta("catalog_full_synced_at") == full_synced

    def test_404_on_first_page_is_raised(self, catalog, client):
        client.not_found_past_end = True
        client.pages = []

        def browse(**kwargs):
            raise NotFoundError()

        client.browse = browse  # type: ignore[method-assign]
        with pytest.raises(NotFoundError):
            catalog.sync(full=True, token=CancelToken())

    def test_repeating_pagination_stops_without_deleting(self, catalog, client, db):
        client.set_games([game(f"g{i}") for i in range(6)], per_page=2)
        catalog.sync(full=True, token=CancelToken())
        full_synced = db.get_meta("catalog_full_synced_at")
        client.repeat_first_page = True

        catalog.sync(full=True, token=CancelToken())

        assert client.browse_calls[-2:] == [1, 2]  # page 2 repeated page 1 → stop
        assert catalog.count() == 6
        assert db.get_meta("catalog_full_synced_at") == full_synced

    def test_cancel_during_page_delay(self, db, client, bus, clock):
        catalog = CatalogService(db, client, bus, page_delay=30, clock=clock)  # type: ignore[arg-type]
        client.set_games([game(f"g{i}") for i in range(4)], per_page=2)
        token = CancelToken()
        started = time.monotonic()
        with pytest.raises(OperationCancelled):  # cancelled while waiting before page 2
            catalog.sync(full=True, token=token, on_progress=lambda d, t: threading.Timer(0.05, token.cancel).start())
        assert time.monotonic() - started < 5
        assert catalog.count() == 2  # first page kept
        assert catalog.last_synced() == ""

    def test_page_delay_is_applied_between_pages(self, db, client, bus, clock):
        catalog = CatalogService(db, client, bus, page_delay=0.35, clock=clock)  # type: ignore[arg-type]
        client.set_games([game(f"g{i}") for i in range(3)], per_page=1)
        token = RecordingToken()
        catalog.sync(full=True, token=token)
        assert token.sleeps == [0.35, 0.35]  # two delays for three pages, none before the first

    def test_default_page_delay_is_the_polite_one(self, db, client, bus):
        assert CatalogService(db, client, bus)._page_delay == 0.35  # type: ignore[arg-type,attr-defined]

    def test_already_cancelled_token(self, catalog, client):
        client.set_games([game("a")], per_page=1)
        token = CancelToken()
        token.cancel()
        with pytest.raises(OperationCancelled):
            catalog.sync(token=token)
        assert client.browse_calls == []


class TestIncrementalSync:
    def _site(self, n: int) -> list[GameSummary]:
        return [game(f"g{i}") for i in range(n)]

    def test_stops_after_two_known_pages_and_shifts_ranks(self, catalog, client, db, clock):
        client.set_games(self._site(15), per_page=3)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(hours=12)
        site = [game("new1"), *self._site(15)]
        client.set_games(site, per_page=3)
        client.browse_calls.clear()

        new = catalog.sync(full=False, token=CancelToken())

        assert new == 1
        assert client.browse_calls == [1, 2, 3]  # page 1 has a new game; pages 2 and 3 are all known
        assert ranks(db) == {g.slug: i for i, g in enumerate(site)}
        assert catalog.count() == 16
        assert catalog.last_synced() == "2026-10-07T00:00:00+00:00"
        assert db.get_meta("catalog_full_synced_at") == "2026-10-06T12:00:00+00:00"  # not a complete crawl

    def test_runs_full_when_last_full_sync_is_old(self, catalog, client, clock):
        client.set_games(self._site(15), per_page=3)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(days=8)
        client.browse_calls.clear()

        catalog.sync(full=False, token=CancelToken())

        assert client.browse_calls == [1, 2, 3, 4, 5]

    def test_runs_full_on_empty_catalog(self, catalog, client):
        client.set_games(self._site(9), per_page=3)
        assert catalog.sync(full=False, token=CancelToken()) == 9
        assert client.browse_calls == [1, 2, 3]

    def test_games_first_seen_through_upsert_still_shift_unreached_ranks(self, catalog, client, db, clock):
        # The store's "Recently added" page upserts the newest game (no rank) before the next sync runs;
        # it is "known" to the crawl, yet every unreached row must still move down one place.
        client.set_games(self._site(15), per_page=3)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(hours=1)
        catalog.upsert([game("new1")])
        site = [game("new1"), *self._site(15)]
        client.set_games(site, per_page=3)

        assert catalog.sync(full=False, token=CancelToken()) == 0  # nothing unknown was found

        assert ranks(db) == {g.slug: i for i, g in enumerate(site)}

    def test_removed_and_added_games_balance_out(self, catalog, client, db, clock):
        client.set_games(self._site(15), per_page=3)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(hours=1)
        site = [game("new1"), game("g0"), *self._site(15)[2:]]  # g1 removed, new1 added
        client.set_games(site, per_page=3)

        catalog.sync(full=False, token=CancelToken())

        got = ranks(db)
        assert {slug: got[slug] for slug in got if slug != "g1"} == {g.slug: i for i, g in enumerate(site)}

    def test_incremental_reaching_the_end_counts_as_complete(self, catalog, client, db, clock):
        client.set_games(self._site(4), per_page=2)
        catalog.sync(full=True, token=CancelToken())
        clock.advance(hours=1)
        client.set_games([game("n1"), game("n2"), game("n3"), *self._site(3)], per_page=2)

        catalog.sync(full=False, token=CancelToken())

        assert catalog.get("g3") is None  # removed: crawl reached the natural end
        assert db.get_meta("catalog_full_synced_at") == "2026-10-06T13:00:00+00:00"


class TestNeedsSync:
    def test_empty_catalog_needs_sync(self, catalog):
        assert catalog.needs_sync(timedelta(days=1))

    def test_age_based(self, catalog, client, clock):
        client.set_games([game("a")], per_page=1)
        catalog.sync(full=True, token=CancelToken())
        assert not catalog.needs_sync(timedelta(hours=24))
        clock.advance(hours=25)
        assert catalog.needs_sync(timedelta(hours=24))

    def test_rows_without_sync_timestamp(self, catalog):
        catalog.upsert([game("a")])
        assert catalog.needs_sync(timedelta(days=365))


# --- search ----------------------------------------------------------------------------------


class TestSearch:
    @pytest.fixture
    def seeded(self, catalog, client):
        seed(catalog, client, [
            game("unhollowed-knights", "Unhollowed Knights", primary_genre="Indie"),           # rank 0
            game("hollow-knight-silksong", "Hollow Knight: Silksong", primary_genre="Action"),  # rank 1
            game("the-hollow-knightly-tale", "The Hollow Knightly Tale", primary_genre="RPG"),  # rank 2
            game("hollow-knight", "Hollow Knight", primary_genre="Action"),              # rank 3
            game("hollow-knight-voidheart", "Hollow Knight Voidheart", primary_genre="Open World"),  # rank 4
            game("baldurs-gate-3", "Baldur's Gate 3", primary_genre="RPG"),
            game("cyberpunk-2077", "Cyberpunk 2077", primary_genre="Open World"),
        ])
        return catalog

    def test_ranking_tiers_then_listing_rank(self, seeded):
        result = [g.slug for g in seeded.search("hollow knight")]
        assert result == [
            "hollow-knight",  # exact
            "hollow-knight-silksong",  # prefix (rank 1)
            "hollow-knight-voidheart",  # prefix (rank 4)
            "the-hollow-knightly-tale",  # every token starts a word
            "unhollowed-knights",  # substring only
        ]

    def test_token_order_and_case_do_not_matter(self, seeded):
        assert [g.slug for g in seeded.search("KNIGHT  silk")] == ["hollow-knight-silksong"]

    def test_apostrophes_are_ignored(self, seeded):
        assert [g.slug for g in seeded.search("baldurs gate")] == ["baldurs-gate-3"]
        assert [g.slug for g in seeded.search("Baldur's")] == ["baldurs-gate-3"]

    def test_every_token_must_match(self, seeded):
        assert seeded.search("hollow cyberpunk") == []

    def test_genre_filter_accepts_names_and_slugs(self, seeded):
        assert [g.slug for g in seeded.search("knight", genre="Action")] == [
            "hollow-knight-silksong", "hollow-knight"]
        assert [g.slug for g in seeded.search("", genre="open-world")] == [
            "hollow-knight-voidheart", "cyberpunk-2077"]

    def test_empty_query_without_genre(self, seeded):
        assert seeded.search("") == []
        assert seeded.search("  !!  ") == []

    def test_limit(self, seeded):
        assert len(seeded.search("knight", limit=2)) == 2
        assert seeded.search("knight", limit=0) == []

    def test_results_are_copies(self, seeded):
        first = seeded.search("cyberpunk")[0]
        first.title = "mutated"
        assert seeded.search("cyberpunk")[0].title == "Cyberpunk 2077"
        assert seeded.get("cyberpunk-2077").title == "Cyberpunk 2077"

    def test_index_refreshes_after_writes(self, seeded):
        assert seeded.search("portal") == []
        seeded.upsert([game("portal-2", "Portal 2")])
        assert [g.slug for g in seeded.search("portal")] == ["portal-2"]

    def test_unranked_rows_sort_after_ranked(self, seeded):
        seeded.upsert([game("cyberpunk-2077-phantom-liberty", "Cyberpunk 2077 Phantom Liberty")])
        seeded.upsert([game("cyberpunk-2077-ultimate", "Cyberpunk 2077 Ultimate")])
        assert [g.slug for g in seeded.search("cyberpunk 2077")] == [
            "cyberpunk-2077", "cyberpunk-2077-phantom-liberty", "cyberpunk-2077-ultimate"]

    def test_search_is_instant_on_a_large_catalog(self, catalog):
        words = ["shadow", "knight", "dragon", "racing", "farm", "space", "war", "legend", "city", "ocean"]
        games = [game(f"g{i}", f"{words[i % 10]} {words[(i // 10) % 10]} saga {i}") for i in range(2500)]
        catalog.upsert(games)
        catalog.search("warm up")  # builds the index
        started = time.perf_counter()
        for query in ["dragon", "knight war", "saga 12", "ocean city saga", "zzz", "a"] * 10:
            catalog.search(query)
        per_query = (time.perf_counter() - started) / 60
        assert per_query < 0.05
        found = catalog.search("dragon racing", limit=1000)
        assert len(found) == 50  # "dragon racing saga N" and "racing dragon saga N"
        assert all(g.title.startswith("dragon racing") for g in found[:25])  # prefix tier first


# --- match_title ---------------------------------------------------------------------------


class TestMatchTitle:
    @pytest.fixture
    def seeded(self, catalog, client):
        seed(catalog, client, [
            game("hollow-knight", "Hollow Knight"),
            game("hollow-knight-silksong", "Hollow Knight: Silksong"),
            game("cyberpunk-2077", "Cyberpunk 2077"),
            game("elden-ring", "ELDEN RING"),
            game("baldurs-gate-3", "Baldur's Gate 3"),
            game("the-witcher-3-wild-hunt", "The Witcher 3: Wild Hunt"),
            game("doom", "DOOM"),
            game("dark-souls", "Dark Souls"),
            game("dark-souls-iii", "Dark Souls III"),
            game("hades", "Hades"),
            game("hades-ii", "Hades II"),
            game("prey-2006", "Prey"),
            game("prey-2017", "Prey"),
        ])
        return catalog

    @pytest.mark.parametrize(("name", "slug"), [
        ("Hollow Knight", "hollow-knight"),
        ("Hollow Knight v1.5.78", "hollow-knight"),
        ("Hollow_Knight_v1.5.78.11833", "hollow-knight"),
        ("Hollow Knight (v1.5.78)", "hollow-knight"),
        ("hollow-knight", "hollow-knight"),
        ("Cyberpunk 2077 Ultimate Edition", "cyberpunk-2077"),
        ("Cyberpunk 2077 GOTY", "cyberpunk-2077"),
        ("Cyberpunk 2077 - Game of the Year Edition", "cyberpunk-2077"),
        ("Cyberpunk 2077 + 3 DLCs", "cyberpunk-2077"),
        ("Cyberpunk 2077 v2.12 Ultimate Edition [FitGirl Repack]", "cyberpunk-2077"),
        ("ELDEN RING", "elden-ring"),
        ("Elden Ring", "elden-ring"),
        ("Elden Ring Build 123456", "elden-ring"),
        ("Elden Ring 1.10.1", "elden-ring"),
        ("Baldurs Gate 3", "baldurs-gate-3"),
        ("Baldur's Gate 3", "baldurs-gate-3"),
        ("The Witcher 3", "the-witcher-3-wild-hunt"),
        ("Dark Souls III Fire Fades Edition", "dark-souls-iii"),
        ("Hades II [FitGirl Repack]", "hades-ii"),
        ("Hollow Knight Voidheart Edition", "hollow-knight"),
    ])
    def test_matches(self, seeded, name, slug):
        match = seeded.match_title(name)
        assert match is not None, name
        assert match.slug == slug

    @pytest.mark.parametrize("name", [
        "Hollow",  # Hollow Knight + Hollow Knight: Silksong
        "Dark",  # several titles extend it
        "Prey",  # two games share the title
        "Doom 3 BFG",  # "DOOM" + a sequel number is another game
        "Hades III",
        "Totally Unknown Game",
        "",
        "   ",
        "v1.0",
    ])
    def test_no_match(self, seeded, name):
        assert seeded.match_title(name) is None

    @pytest.mark.parametrize("name", [
        "Doom Eternal",  # a different game whose title starts with a catalog title
        "DOOM Eternal v6.66",  # noise does not make an unnumbered franchise title a match
        "Hades Remake [FitGirl Repack]",
        "Elden Ring Nightreign",
        "Elden Ring Nightreign Deluxe Edition",
        "Baldurs Gate 3 Toolkit",  # numbered title, but no edition/version noise
        "Dark Souls III 2 Deluxe Edition",  # another sequel number follows the numbered title
    ])
    def test_no_false_positive_for_games_extending_a_catalog_title(self, seeded, name):
        assert seeded.match_title(name) is None

    def test_numbered_title_with_noise_matches_by_prefix(self, catalog, client):
        seed(catalog, client, [game("cyberpunk-2077", "Cyberpunk 2077"), game("doom", "DOOM")])
        assert catalog.match_title("Cyberpunk 2077 Phantom Liberty v2.1").slug == "cyberpunk-2077"
        assert catalog.match_title("Cyberpunk 2077 Phantom Liberty") is None

    def test_apostrophe_in_catalog_but_not_in_folder_and_vice_versa(self, catalog, client):
        seed(catalog, client, [game("baldurs-gate-3", "Baldurs Gate 3")])
        assert catalog.match_title("Baldur's Gate 3").slug == "baldurs-gate-3"

    def test_edition_in_catalog_wins_over_stripping(self, catalog, client):
        seed(catalog, client, [
            game("cyberpunk-2077", "Cyberpunk 2077"),
            game("cyberpunk-2077-ultimate-edition", "Cyberpunk 2077 Ultimate Edition"),
        ])
        assert catalog.match_title("Cyberpunk 2077 Ultimate Edition").slug == "cyberpunk-2077-ultimate-edition"
        assert catalog.match_title("Cyberpunk 2077 v2.1").slug == "cyberpunk-2077"

    def test_duplicate_titles_prefer_the_matching_slug(self, catalog, client):
        seed(catalog, client, [game("doom", "Doom"), game("doom-1993", "DOOM")])
        assert catalog.match_title("Doom").slug == "doom"

    def test_empty_catalog(self, catalog):
        assert catalog.match_title("Hollow Knight") is None


# --- upsert / get -----------------------------------------------------------------------------


class TestUpsert:
    def test_returns_new_count_and_skips_invalid(self, catalog):
        assert catalog.upsert([game("a"), game("b"), game(""), game("  "), game("a")]) == 2
        assert catalog.upsert([game("a"), game("c")]) == 1
        assert catalog.count() == 3
        assert catalog.upsert([]) == 0

    def test_never_blanks_known_fields_or_changes_rank(self, catalog, client, db):
        seed(catalog, client, [game("a", "Alpha", cover_url="cover", primary_genre="RPG", year=2001,
                                    size_text="2 GB", size_bytes=2 * 2**30)])
        catalog.upsert([GameSummary(slug="a", title="")])
        got = catalog.get("a")
        assert got == GameSummary("a", "Alpha", "cover", "RPG", 2001, "2 GB", 2 * 2**30)
        assert ranks(db) == {"a": 0}

        catalog.upsert([GameSummary(slug="a", title="Alpha 2", cover_url="new-cover")])
        got = catalog.get("a")
        assert (got.title, got.cover_url, got.primary_genre) == ("Alpha 2", "new-cover", "RPG")

    def test_title_falls_back_to_slug_for_new_rows(self, catalog):
        catalog.upsert([GameSummary(slug="some-cool-game", title="")])
        assert catalog.get("some-cool-game").title == "Some Cool Game"

    def test_get_unknown(self, catalog):
        assert catalog.get("nope") is None
        assert catalog.get("") is None


# --- details ------------------------------------------------------------------------------------


def details_for(slug: str, version: str = "v1.0", **kwargs) -> GameDetails:
    return GameDetails(slug=slug, title=slug.replace("-", " ").title(), version=version, **kwargs)


class TestDetails:
    def test_fetches_and_caches(self, catalog, client):
        client.details["hk"] = details_for("hk")
        first = catalog.details("hk")
        second = catalog.details("hk")
        assert first.version == second.version == "v1.0"
        assert client.details_calls == ["hk"]
        assert catalog.cached_details("hk").version == "v1.0"
        assert first.fetched_at  # stamped when the parser did not

    def test_refetches_when_stale(self, catalog, client, clock):
        client.details["hk"] = details_for("hk")
        catalog.details("hk")
        clock.advance(hours=7)
        client.details["hk"] = details_for("hk", version="v2.0")
        assert catalog.details("hk").version == "v2.0"
        assert catalog.details("hk", max_age=timedelta(minutes=1)).version == "v2.0"
        assert client.details_calls == ["hk", "hk"]

    def test_zero_max_age_always_fetches(self, catalog, client):
        client.details["hk"] = details_for("hk")
        catalog.details("hk")
        catalog.details("hk", max_age=timedelta(0))
        assert client.details_calls == ["hk", "hk"]

    @pytest.mark.parametrize("error", [NetworkError(), SiteChangedError()])
    def test_network_failure_returns_stale_cache(self, catalog, client, clock, error):
        client.details["hk"] = details_for("hk")
        catalog.details("hk")
        clock.advance(days=2)
        client.details_error = error
        assert catalog.details("hk").version == "v1.0"

    def test_network_failure_without_cache_raises(self, catalog, client):
        client.details_error = NetworkError()
        with pytest.raises(NetworkError):
            catalog.details("hk")

    def test_not_found_is_raised_even_with_cache(self, catalog, client, clock):
        client.details["hk"] = details_for("hk")
        catalog.details("hk")
        clock.advance(days=2)
        client.details_error = NotFoundError()
        with pytest.raises(NotFoundError):
            catalog.details("hk")

    def test_cancellation_is_raised(self, catalog, client):
        client.details_error = OperationCancelled()
        with pytest.raises(OperationCancelled):
            catalog.details("hk")

    def test_round_trip_preserves_nested_models(self, catalog, client):
        from anker_client.core.models import DownloadKind, DownloadOption, SystemRequirements

        client.details["hk"] = details_for(
            "hk",
            genres=["Action"],
            requirements=SystemRequirements(os="Windows 10"),
            download_options=[DownloadOption(5, "Update Only From V 1 To V 2", DownloadKind.PATCH,
                                             from_version="1", to_version="2")],
        )
        catalog.details("hk")
        cached = catalog.cached_details("hk")
        assert cached.requirements.os == "Windows 10"
        assert cached.download_options[0].kind is DownloadKind.PATCH
        assert cached.download_options[0].from_version == "1"

    def test_details_add_unknown_games_to_the_catalog(self, catalog, client):
        client.details["new-game"] = details_for("new-game", release_date="2024-05-01", cover_url="poster")
        catalog.details("new-game")
        got = catalog.get("new-game")
        assert got is not None and got.year == 2024 and got.cover_url == "poster"
        assert [g.slug for g in catalog.search("new game")] == ["new-game"]

    def test_corrupt_cache_is_ignored(self, catalog, client, db):
        db.execute("INSERT INTO game_details(slug, json, fetched_at) VALUES('hk', '{not json', '2026-10-06T12:00:00')")
        assert catalog.cached_details("hk") is None
        client.details["hk"] = details_for("hk")
        assert catalog.details("hk").version == "v1.0"

    def test_concurrent_requests_fetch_once(self, catalog, client):
        client.details["hk"] = details_for("hk")
        client.details_delay = 0.2
        results: list[GameDetails] = []
        threads = [threading.Thread(target=lambda: results.append(catalog.details("hk"))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(results) == 4
        assert client.details_calls == ["hk"]

    def test_empty_slug(self, catalog):
        with pytest.raises(NotFoundError):
            catalog.details("")
        assert catalog.cached_details("") is None

    def test_cache_write_failure_still_returns_fresh_details(self, catalog, client, monkeypatch):
        client.details["hk"] = details_for("hk", version="v3.0")

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(catalog, "_store_details", locked)
        assert catalog.details("hk").version == "v3.0"
        assert catalog.cached_details("hk") is None

    def test_waiter_is_cancellable_while_another_thread_fetches(self, catalog, client):
        client.details["hk"] = details_for("hk")
        gate = threading.Event()
        entered = threading.Event()

        def slow(slug, *, token=None):
            entered.set()
            gate.wait(5)
            return details_for(slug)

        client.game_details = slow  # type: ignore[method-assign]
        leader = threading.Thread(target=lambda: catalog.details("hk"))
        leader.start()
        assert entered.wait(5)
        token = CancelToken()
        threading.Timer(0.05, token.cancel).start()
        started = time.monotonic()
        with pytest.raises(OperationCancelled):
            catalog.details("hk", token=token)
        assert time.monotonic() - started < 2
        gate.set()
        leader.join(5)


# --- wishlist -----------------------------------------------------------------------------------


class TestWishlist:
    def test_add_remove_and_events(self, catalog, recorded, clock):
        a = game("a", "Alpha", cover_url="ca")
        b = game("b", "Beta", cover_url="cb")
        catalog.set_wishlisted(a, True)
        clock.advance(minutes=1)
        catalog.set_wishlisted(b, True)
        catalog.set_wishlisted(b, True)  # no change → no event

        assert catalog.is_wishlisted("a") and catalog.is_wishlisted("b")
        assert [g.slug for g in catalog.wishlist()] == ["b", "a"]  # newest first

        catalog.set_wishlisted(a, False)
        catalog.set_wishlisted(a, False)
        assert not catalog.is_wishlisted("a")
        assert [g.slug for g in catalog.wishlist()] == ["b"]

        changes = [(e.slug, e.wishlisted) for e in recorded if isinstance(e, ev.WishlistChanged)]
        assert changes == [("a", True), ("b", True), ("a", False)]

    def test_same_second_adds_keep_insertion_order(self, catalog):
        for slug in ("x", "y", "z"):
            catalog.set_wishlisted(game(slug), True)
        assert [g.slug for g in catalog.wishlist()] == ["z", "y", "x"]

    def test_enriched_from_catalog(self, catalog, client):
        seed(catalog, client, [game("a", "Alpha", cover_url="catalog-cover", primary_genre="RPG", year=2020,
                                    size_text="3 GB", size_bytes=3)])
        catalog.set_wishlisted(GameSummary(slug="a", title="Old title"), True)
        catalog.set_wishlisted(GameSummary(slug="gone", title="Gone Game", cover_url="gc"), True)
        items = {g.slug: g for g in catalog.wishlist()}
        assert items["a"] == GameSummary("a", "Alpha", "catalog-cover", "RPG", 2020, "3 GB", 3)
        assert items["gone"] == GameSummary("gone", "Gone Game", "gc")

    def test_game_without_slug_is_ignored(self, catalog, recorded):
        catalog.set_wishlisted(GameSummary(slug="", title="x"), True)
        assert catalog.wishlist() == []
        assert not any(isinstance(e, ev.WishlistChanged) for e in recorded)


def test_concurrent_syncs_are_serialised(db, client, bus, clock):
    catalog = CatalogService(db, client, bus, page_delay=0.02, clock=clock)  # type: ignore[arg-type]
    client.set_games([game(f"g{i}") for i in range(6)], per_page=2)
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(catalog.sync(full=True, token=CancelToken())))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == [0, 6]
    assert catalog.count() == 6


def test_details_cached_by_an_older_parser_are_refetched(tmp_path) -> None:
    """Regression: a parser fix must not be hidden by details cached with the old parse."""
    import json as _json

    from anker_client.core.db import Database
    from anker_client.core.events import EventBus
    from anker_client.core.models import DownloadKind, DownloadOption, GameDetails, utc_now_iso
    from anker_client.services.catalog import CatalogService
    from anker_client.site.parsers import PARSER_VERSION

    fresh = GameDetails(slug="left-4-dead-2", title="Left 4 Dead 2",
                        download_options=[DownloadOption(4150, "DataNodes", DownloadKind.FULL)])

    class Client:
        calls = 0

        def game_details(self, slug, *, token=None):
            Client.calls += 1
            return fresh.copy()

    db = Database(tmp_path / "c.db")
    stale = GameDetails(slug="left-4-dead-2", title="Left 4 Dead 2",
                        download_options=[DownloadOption(4150, "DataNodes File integrity MD5 x", DownloadKind.ADDON)])
    db.execute("INSERT INTO game_details(slug, json, fetched_at) VALUES(?, ?, ?)",
               ("left-4-dead-2", _json.dumps(stale.to_dict()), utc_now_iso()))  # no parser_version = old parser
    catalog = CatalogService(db, Client(), EventBus())

    assert catalog.cached_details("left-4-dead-2") is None
    details = catalog.details("left-4-dead-2")
    assert Client.calls == 1
    assert details.download_options[0].kind is DownloadKind.FULL
    assert catalog.cached_details("left-4-dead-2").download_options[0].label == "DataNodes"  # re-cached, current version
    stored = _json.loads(db.scalar("SELECT json FROM game_details WHERE slug = ?", ("left-4-dead-2",)))
    assert stored["parser_version"] == PARSER_VERSION
    db.close()

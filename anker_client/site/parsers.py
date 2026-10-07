"""Pure HTML → model parsers for ankergames.net. No network, no globals, no Qt.

Every function takes the page HTML (and the URL it came from when relative
links matter) and returns models from ``anker_client.core.models``. Parsers are
defensive: a missing optional field yields a default; a missing *essential*
structure raises ``SiteChangedError`` with a precise ``detail``. Each page is
parsed exactly once with BeautifulSoup + ``lxml`` (``html.parser`` fallback).

Site facts (verified 2026-10-06 against live pages, saved under tests/fixtures/site):

Listing cards (``/games``, ``/games?page=N&sort=K``, ``/genre/{slug}``,
``/games/vr``, ``/search/{q}?page=N``, ``/top-games``, home page sections)::

    <article class="group relative" x-data="uiPostCard('https:\\/\\/ankergames.net\\/game\\/{slug}')" ...>
      ... <picture><source srcset="...poster....webp" type="image/webp">
                <img src="...poster....jpg" alt="Title" ...></picture>
      <span title="B 25724756">…</span>                     (build/version badge — ignored)
      <p ... title="Action">Action</p>                      (first/primary genre — optional)
      <h3 ... title="Gears of War: E-Day">Gears of War: E-Day</h3>
      <p ...><span>2026</span><span ...></span><span class="truncate">104.60 GB</span></p>  (year, size — optional)
      <a href="https://ankergames.net/game/{slug}" title="..." ...>
    </article>

  * Titles contain HTML entities (``&#039;``) — always unescape.
  * ``/top-games`` additionally shows a podium (``<article class="tc-throne|tc-sec">``)
    with a wide cover, an ``<h2>``/title div and a "Genre · Genre · 2013" meta
    line; those are parsed by the generic link-card reader (cover_url is then the
    wide image — the only one the podium provides).
  * The Livewire quick-search fragment lists ``<li><a href="/game/{slug}"><img alt>
    <h4>Title</h4>…`` — also read by the generic link-card reader, which is the
    fallback whenever a *fragment* (``parse_cards``) holds no game articles.
    Full listing pages never use that fallback: their sidebars (e.g. "Top this
    week" on search pages) link to unrelated games, so a page without cards is
    an empty page.
  * Pagination: ``<nav aria-label="Pagination">`` with ``<a rel="next">`` /
    ``<a rel="prev">`` (``/top-games`` adds numbered links and "Page 1 of 86").
    The next page exists when ``rel="next"`` or a link with ``page=current+1``
    is present — and never after a page without cards. Requests beyond the
    last page return HTTP 404 (the client treats that as an empty, final page).
  * A page holds ~56 cards; the same slug can appear twice — de-duplicate,
    keep first occurrence order.

Home page (``/``): ``<h2>`` section headings ("Trending Games", "Upcoming Games",
"Latest Games", "Epic Collections", …). A section's cards are those inside the
largest ancestor of its heading that contains no other section heading.
Headings inside cards/links (collection hubs, the game-of-the-day article)
are not section headings. Special cases:
  * "Upcoming Games" entries are not on the site yet: they link to the Steam
    store. They are returned with a slug derived from the title (the site's
    slug scheme), so their game page may 404 until the site publishes them.
  * ``<article x-data="gameOfTheDay(…)">`` becomes a one-game "Game of the Day"
    section at its document position.
  * The hero slider and collection hubs (links to ``/collection/…``) carry no
    game cards and are skipped; sections without cards are dropped.

Game page (``/game/{slug}``):
  * ``<script type="application/ld+json">`` holds a JSON **list**; the item with
    ``"@type": "VideoGame"`` has name, description, image (list: [wide cover,
    2:3 poster] — the poster is identified by the ``aspect-2/3`` picture in the
    page, then by "poster"/"cover" in the file name, then by position),
    datePublished, dateModified, fileSize ("1.1 GB"), softwareVersion
    ("v1.5.12620" or "Build 25724756"), softwareRequirements (multi-line
    "OS: …\\r\\nProcessor: …"), processorRequirements, memoryRequirements,
    storageRequirements, screenshot (list of URLs, possibly percent-encoded),
    genre (list).
  * Download modal (inside ``<template x-if="downloadOpen">``):
    ``<li class="py-4 font-medium …"><div>LABEL</div> … @click.prevent="generateDownloadUrl(ID)"``
    per option. lxml drops ``@``-attributes, so the page is parsed with them
    rewritten to Alpine's long form ``x-on:``. Labels seen: "Direct",
    "Direct V 4.1.1.7631656", "Language Pack (61.2 GB)", "Launcher",
    "Update Only From V 4.1.1.7398727 To V 4.1.1.7631656 (124 MB)".
  * Torrent: guests see a "Torrent Download" block ("Sign in required");
    signed-in users get ``generateTorrentUrl(ID)``. Either means available.
    (The bare JS definition ``generateTorrentUrl(downloadId)`` is on every page
    and is NOT a signal.)
  * ``<meta name="csrf-token" content="…">`` (stale on Cloudflare-cached guest pages).

Ticket page (``/download/{signed}/{hash}``):
  * ``x-data="downloadPage('https:\\/\\/ankergames.net\\/download-file\\/{ticket}', PROVIDER, IS_TORRENT,
    STATUS, STATUS_URL, WAIT)"``
    where PROVIDER is ``null`` or a JS object literal with a ``name`` key,
    IS_TORRENT is ``true``/``false`` and WAIT is the countdown in seconds.
    Read with the page script's own semantics: the URL always goes through
    ``decodeURIComponent``, WAIT through ``Math.max(0, Math.ceil(WAIT))``
    (numeric strings count; a missing WAIT defaults to 5, ``null`` is 0) and
    IS_TORRENT through JS truthiness.
  * Turnstile is active when an element with ``data-ag-turnstile`` exists
    (``data-sitekey`` holds the site key).
  * Version: ``<span title="V 1.5.12620">`` above a "Version" label; size above
    a "File Size" label.

Login page (``/login``): ``<form method="POST" action=".../login">`` with
fields ``_token``, ``email``, ``password``, ``remember``.

Logged-in detection (no signed-in fixture exists — heuristics, in order):
  1. Positive markers: a ``<form>`` posting to ``/logout`` or a link to
     ``/logout``; or ``<meta name="page-auth">`` (the origin only renders it
     for authenticated users; Cloudflare-cached guest pages never have it).
  2. Negative marker: a link to ``/login`` that is not inside a
     ``cf-guest-only`` element (those are CSS-hidden for signed-in users).
     ``meta page-auth`` without a logout marker loses to any ``/login`` link.
  3. Account details are read from the "account container": the nearest
     ancestor of the logout form/link that holds a profile link
     (``/profile/{name}``) or an avatar image. Display name: profile link text
     (unless generic like "Profile"), else avatar ``alt``, else the profile URL
     name, else "" (the client falls back to the email used to sign in).
     ``is_subscriber`` is True only when that container shows a
     premium/VIP/subscriber badge and no upgrade/subscribe call to action.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag
from requests.utils import requote_uri

from anker_client.constants import BASE_URL
from anker_client.core.errors import SiteChangedError
from anker_client.core.formatting import parse_size
from anker_client.core.models import (
    DownloadKind,
    DownloadOption,
    GameDetails,
    GameSummary,
    Genre,
    HomeSection,
    ListingPage,
    SystemRequirements,
    TicketPage,
    UserInfo,
    utc_now_iso,
)
from anker_client.site import _js
from anker_client.site._html import (
    absolute,
    attr,
    clean,
    first_srcset_url,
    image_url,
    is_size_text,
    make_soup,
    page_param,
    slug_from_url,
    text_of,
)

log = logging.getLogger(__name__)

_POST_CARD_RE = re.compile(r"uiPostCard\(\s*(['\"])(.*?)\1")
_YEAR_RE = re.compile(r"(?<![\d.,])((?:19|20)\d{2})(?![\d.,])")
_GENRE_PATH_RE = re.compile(r"/genre/([^/?#\s]+)/?$")
_GENRE_COUNT_SUFFIX_RE = re.compile(r"\s*[\d.,]+\s*[kKmM]?\s+games?\s*$", re.IGNORECASE)
_PAGE_OF_RE = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
#: Bump whenever parse_game_page output changes; cached GameDetails from older parsers are refetched.
PARSER_VERSION = 2

_DOWNLOAD_CALL_RE = re.compile(r"generateDownloadUrl\(\s*(\d+)\s*\)")
_TORRENT_CALL_RE = re.compile(r"generateTorrentUrl\(\s*\d+\s*\)")
_OPTION_SIZE_RE = re.compile(r"\(\s*(\d+(?:[.,]\d+)?\s*(?:KB|KiB|MB|MiB|GB|GiB|TB|TiB))\s*\)", re.IGNORECASE)
_VERSION_TOKEN = r"(?:v(?:ersion)?|build|b)?\.?\s*([0-9][\w.\-]*)"
_PATCH_RE = re.compile(rf"\bfrom\s+{_VERSION_TOKEN}\s+to\s+{_VERSION_TOKEN}", re.IGNORECASE)
_LABEL_VERSION_RE = re.compile(r"\b(?:v(?:ersion)?|build)\.?\s*([0-9][\w.\-]*)", re.IGNORECASE)
_REQUIREMENT_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./()-]{0,40}?)\s*:\s*(.+?)\s*$")
_LIVEWIRE_CONFIG_RE = re.compile(r"livewireScriptConfig\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SUBSCRIBER_RE = re.compile(r"\b(premium|vip|subscriber|supporter|pro member)\b", re.IGNORECASE)
_UPSELL_RE = re.compile(r"\b(upgrade|subscribe|go premium|get premium|become)\b", re.IGNORECASE)
_GENERIC_ACCOUNT_LABELS = frozenset(
    {"profile", "my profile", "view profile", "account", "my account", "settings", "dashboard", "logout", "log out"}
)
_REQUIREMENT_KEYS = {
    "os": "os",
    "operating system": "os",
    "processor": "processor",
    "cpu": "processor",
    "memory": "memory",
    "ram": "memory",
    "system memory": "memory",
    "graphics": "graphics",
    "video card": "graphics",
    "video": "graphics",
    "gpu": "graphics",
    "directx": "directx",
    "direct x": "directx",
    "storage": "storage",
    "hard drive": "storage",
    "hard disk": "storage",
    "disk space": "storage",
    "hdd": "storage",
}
_DATE_FORMATS = ("%d %b, %Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%Y/%m/%d", "%d/%m/%Y")
_UPCOMING_SECTION_RE = re.compile(r"upcoming", re.IGNORECASE)
_PROFILE_HREF_RE = re.compile(r"/(?:profile|user|u)/[^/?#]+/?$")


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------


def parse_listing(html: str, *, page: int = 1, url: str = "") -> ListingPage:
    """Cards + pagination from any listing page (games, genre, vr, search, top-games)."""
    soup = make_soup(html)
    if page == 1:
        # Callers that only have the URL (or nothing) still get the right page number.
        canonical = attr(soup.find("link", rel="canonical"), "href")
        page = page_param(url) or page_param(canonical) or 1
    games = _cards_in(soup, base=url or BASE_URL, link_fallback=False)
    has_next, total_pages = _pagination(soup, page, url or BASE_URL)
    if not games:
        has_next = False  # an empty page is the end, whatever its pagination widget says
    if not has_next and total_pages is None and games:
        total_pages = page
    return ListingPage(games=games, page=page, has_next=has_next, total_pages=total_pages)


def parse_cards(html: str) -> list[GameSummary]:
    """All game cards in a fragment, de-duplicated by slug (first occurrence wins)."""
    return _cards_in(make_soup(html), base=BASE_URL, link_fallback=True)


def _cards_in(root: Tag, *, base: str, link_fallback: bool) -> list[GameSummary]:
    """``uiPostCard`` articles and other game-card articles (podium) in document order.

    With ``link_fallback`` (fragments only), a root holding no game articles at all
    falls back to any link to a game that wraps an image (the quick-search list).
    """
    games: list[GameSummary] = []
    seen: set[str] = set()
    for article in root.find_all("article"):
        _append_unique(games, seen, _article_card(article, base))
    if not games and link_fallback:
        for link in root.find_all("a", href=True):
            if link.find("img") is not None:
                _append_unique(games, seen, _link_card(link, link, base))
    return games


def _article_card(article: Tag, base: str) -> GameSummary | None:
    card = _post_card(article, base)
    if card is None and article.find("article") is None:  # wrappers of other cards are not cards
        card = _link_card_in(article, base)
    return card


def _append_unique(games: list[GameSummary], seen: set[str], game: GameSummary | None) -> None:
    if game is not None and game.slug not in seen:
        seen.add(game.slug)
        games.append(game)


def _post_card(article: Tag, base: str) -> GameSummary | None:
    match = _POST_CARD_RE.search(attr(article, "x-data"))
    if match is None:
        return None
    slug = slug_from_url(_js.unescape_string(match.group(2)))
    if not slug:
        link = article.find("a", href=True)
        slug = slug_from_url(attr(link, "href"))
    if not slug:
        return None
    heading = article.find("h3")
    title = clean(attr(heading, "title")) or text_of(heading)
    if not title:
        link = article.find("a", attrs={"title": True})
        img = article.find("img")
        title = clean(attr(link, "title")) or clean(attr(img, "alt"))
    if not title:
        return None
    genre = ""
    year: int | None = None
    size_text = ""
    if heading is not None:
        genre_el = heading.find_previous_sibling("p")
        genre = clean(attr(genre_el, "title")) or text_of(genre_el)
        meta_el = heading.find_next_sibling("p")
        if meta_el is not None:
            year, size_text = _year_and_size(span.get_text(strip=True) for span in meta_el.find_all("span"))
    return GameSummary(
        slug=slug,
        title=title,
        cover_url=image_url(article.find("picture") or article, base),
        primary_genre=genre,
        year=year,
        size_text=size_text,
        size_bytes=parse_size(size_text),
    )


def _year_and_size(texts: Iterable[str]) -> tuple[int | None, str]:
    year: int | None = None
    size_text = ""
    for raw in texts:
        text = clean(raw)
        if year is None and re.fullmatch(r"(?:19|20)\d{2}", text):
            year = int(text)
        elif not size_text and is_size_text(text):
            size_text = text
    return year, size_text


def _link_card_in(container: Tag, base: str) -> GameSummary | None:
    """A non-``uiPostCard`` article (e.g. the top-games podium) that links to one game."""
    for link in container.find_all("a", href=True):
        if slug_from_url(attr(link, "href")):
            return _link_card(container, link, base)
    return None


def _link_card(container: Tag, link: Tag, base: str) -> GameSummary | None:
    """Best-effort summary from a block holding a link to a game; the link's own image wins."""
    slug = slug_from_url(absolute(attr(link, "href"), base))
    title = _container_title(container)
    if not slug or not title:
        return None
    year, size_text, genre = _loose_meta(container, title)
    return GameSummary(
        slug=slug,
        title=title,
        cover_url=image_url(link, base) or image_url(container, base),
        primary_genre=genre,
        year=year,
        size_text=size_text,
        size_bytes=parse_size(size_text),
    )


def _container_title(container: Tag) -> str:
    for name in ("h1", "h2", "h3", "h4"):
        heading = container.find(name)
        if heading is not None and text_of(heading):
            return text_of(heading)
    for element in container.find_all(attrs={"title": True}):
        if element.name in ("a", "div", "span") and clean(attr(element, "title")):
            title = clean(attr(element, "title"))
            if not title.lower().startswith(("view ", "version", "b ", "v ")):
                return title
    img = container.find("img")
    alt = clean(attr(img, "alt"))
    return re.sub(r"\s*(?:-\s*cover art|logo|background|poster)$", "", alt, flags=re.IGNORECASE).strip()


def _loose_meta(container: Tag, title: str) -> tuple[int | None, str, str]:
    """Year, size and primary genre from free-form card text ("Action · Adventure · 2013", "118.4 GB")."""
    year: int | None = None
    size_text = ""
    genre = ""
    times = container.find_all("time", attrs={"datetime": True})
    # Prefer a <time> that displays just a year (release) over e.g. a "featured on" timestamp.
    time_el = next((t for t in times if re.fullmatch(r"(?:19|20)\d{2}", text_of(t))), times[0] if times else None)
    if time_el is not None:
        match = _YEAR_RE.search(attr(time_el, "datetime"))
        year = int(match.group(1)) if match else None
    for raw in container.stripped_strings:
        text = clean(raw)
        if not text or text == title:
            continue
        if not size_text and is_size_text(text):
            size_text = text
            continue
        parts = [p.strip() for p in re.split(r"[·•|]", text) if p.strip()]
        if len(parts) >= 2 and len(text) < 80:
            if year is None:
                for part in parts:
                    if re.fullmatch(r"(?:19|20)\d{2}", part):
                        year = int(part)
            if not genre and not re.fullmatch(r"(?:19|20)\d{2}", parts[0]):
                genre = parts[0]
    return year, size_text, genre


def _pagination(soup: BeautifulSoup, page: int, url: str) -> tuple[bool, int | None]:
    nav = soup.find("nav", attrs={"aria-label": re.compile(r"pagination", re.IGNORECASE)})
    scope: Tag = nav if isinstance(nav, Tag) else soup
    has_next = False
    numbers: list[int] = []
    numbered = False
    for link in scope.find_all("a", href=True):
        rel = [r.lower() for r in (link.get("rel") or [])]
        target = page_param(absolute(attr(link, "href"), url))
        if "next" in rel:
            has_next = True
        if target is not None:
            numbers.append(target)
            if target == page + 1:
                has_next = True
            if re.fullmatch(r"\d+", text_of(link)):
                numbered = True
    if not has_next and nav is None and soup.find("link", rel="next") is not None:
        has_next = True
    total: int | None = None
    if isinstance(nav, Tag):
        match = _PAGE_OF_RE.search(text_of(nav))
        if match:
            total = int(match.group(2))
    if total is None and numbered and numbers:
        total = max(*numbers, page)
    return has_next, total


# ---------------------------------------------------------------------------
# home page
# ---------------------------------------------------------------------------


def parse_home_sections(html: str) -> list[HomeSection]:
    """Titled card rows from the home page (sections with no cards are dropped)."""
    return _home_sections(make_soup(html))


def _home_sections(soup: BeautifulSoup) -> list[HomeSection]:
    root = soup.body or soup
    markers: list[Tag] = []
    for element in root.find_all(["h2", "article"]):
        if element.name == "h2":
            if element.find_parent(["article", "a"]) is None and text_of(element):
                markers.append(element)
        elif attr(element, "x-data").lstrip().startswith("gameOfTheDay"):
            markers.append(element)
    headings = [m for m in markers if m.name == "h2"]
    containers = _section_containers(headings)
    sections: list[HomeSection] = []
    for marker in markers:
        if marker.name == "article":
            game = _link_card_in(marker, BASE_URL)
            section = HomeSection(title="Game of the Day", games=[game] if game else [])
        else:
            title = text_of(marker)
            container = containers[id(marker)]
            games = _cards_in_section(container, upcoming=bool(_UPCOMING_SECTION_RE.search(title)))
            section = HomeSection(title=title, games=games)
        if section.games:
            sections.append(section)
    return sections


def _section_containers(headings: list[Tag]) -> dict[int, Tag]:
    """For each heading, its largest ancestor that contains no other heading."""
    ancestor_sets = {id(h): {id(a) for a in h.parents} for h in headings}
    containers: dict[int, Tag] = {}
    for heading in headings:
        others = [ancestor_sets[id(h)] for h in headings if h is not heading]
        container: Tag = heading
        for ancestor in heading.parents:
            if not isinstance(ancestor, Tag) or ancestor.name in ("body", "html", "[document]"):
                break
            if any(id(ancestor) in s for s in others):
                break
            container = ancestor
        containers[id(heading)] = container
    return containers


def _cards_in_section(container: Tag, *, upcoming: bool) -> list[GameSummary]:
    games: list[GameSummary] = []
    seen: set[str] = set()
    for article in container.find_all("article"):
        _append_unique(games, seen, _article_card(article, BASE_URL))
    if games or not upcoming:
        return games
    for link in container.find_all("a", href=True):
        heading = link.find(["h3", "h4"])
        if heading is None or link.find("img") is None:
            continue
        _append_unique(games, seen, _upcoming_card(link, text_of(heading)))
    return games


def _upcoming_card(link: Tag, title: str) -> GameSummary | None:
    href = absolute(attr(link, "href"))
    slug = slug_from_url(href)
    if not slug:
        host = (urlsplit(href).hostname or "").lower()
        if host.endswith("ankergames.net"):
            return None  # an internal non-game link (collection, page…)
        slug = _slugify(title)
    if not slug or not title:
        return None
    year, _size, _genre = _loose_meta(link, title)
    if year is None:
        match = _YEAR_RE.search(link.get_text(" ", strip=True))
        year = int(match.group(1)) if match else None
    genre_spans = [text_of(s) for s in link.find_all("span") if text_of(s) and text_of(s) not in ("PC", title)]
    return GameSummary(
        slug=slug,
        title=title,
        cover_url=image_url(link),
        primary_genre=genre_spans[0] if genre_spans else "",
        year=year,
    )


def _slugify(title: str) -> str:
    """The site's slug scheme: lower-case ASCII words joined by hyphens ("Death's Gambit" → "death-s-gambit")."""
    return re.sub(r"[^a-z0-9]+", "-", clean(title).casefold()).strip("-")


def parse_genres(html: str) -> list[Genre]:
    """Main genre navigation (``/genre/{slug}`` links) — de-duplicated, display order."""
    return _genres_in(make_soup(html))


def _parse_home_page(html: str) -> tuple[list[HomeSection], list[Genre]]:
    """Home sections and the genre navigation from one parse (the client caches the genres)."""
    soup = make_soup(html)
    return _home_sections(soup), _genres_in(soup)


def _genres_in(soup: BeautifulSoup) -> list[Genre]:
    nav = soup.find("nav", attrs={"aria-label": re.compile(r"^\s*genres?\s*$", re.IGNORECASE)})
    links = (nav if isinstance(nav, Tag) else soup).find_all("a", href=True)
    genres: list[Genre] = []
    seen: set[str] = set()
    for link in links:
        match = _GENRE_PATH_RE.search(urlsplit(absolute(attr(link, "href"))).path)
        if not match:
            continue
        slug = unquote(match.group(1)).strip().lower()
        name = clean(attr(link, "title")) or _GENRE_COUNT_SUFFIX_RE.sub("", text_of(link)).strip()
        if slug and slug not in seen:
            seen.add(slug)
            genres.append(Genre(slug=slug, name=name or slug.replace("-", " ").title()))
    return genres


# ---------------------------------------------------------------------------
# game page
# ---------------------------------------------------------------------------


def parse_game_page(html: str, *, slug: str) -> GameDetails:
    """Full details for one game. Raises ``SiteChangedError`` if no title can be found."""
    soup = make_soup(html, keep_alpine_events=True)
    data = _video_game_ld(soup)
    options = _download_options(soup)
    title = clean(_ld_str(data.get("name")))
    if not title and (options or _TORRENT_CALL_RE.search(html)):
        title = text_of(soup.find("h1"))
    if not title:
        raise SiteChangedError(detail=f"Game page for {slug!r} has no VideoGame JSON-LD name or game heading")
    cover, hero = _game_images(soup, data)
    size_text = clean(_ld_str(data.get("fileSize")))
    return GameDetails(
        slug=slug,
        title=title,
        description=_description(soup, data),
        cover_url=cover,
        hero_url=hero,
        genres=_ld_list(data.get("genre")),
        screenshots=_screenshots(data),
        version=clean(_ld_str(data.get("softwareVersion"))),
        release_date=_iso_date(_ld_str(data.get("datePublished"))),
        updated_date=_iso_date(_ld_str(data.get("dateModified"))),
        size_text=size_text,
        size_bytes=parse_size(size_text),
        requirements=_requirements(data),
        download_options=options,
        torrent_available=_torrent_available(soup, html),
        fetched_at=utc_now_iso(),
    )


def _iter_ld_items(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_ld_items(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_ld_items(graph)


def _video_game_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw, strict=False)
        except ValueError:
            log.debug("Skipping malformed JSON-LD block")
            continue
        for item in _iter_ld_items(payload):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "VideoGame" in kinds:
                return item
    return {}


def _ld_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "url", "contentUrl", "@id"):
            if isinstance(value.get(key), str):
                return value[key]
    if isinstance(value, list) and value:
        return _ld_str(value[0])
    return ""


def _ld_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"\s*,\s*", value) if isinstance(value, str) else []
    out: list[str] = []
    for item in items:
        text = clean(_ld_str(item))
        if text and text not in out:
            out.append(text)
    return out


def _ld_urls(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    urls: list[str] = []
    for item in items:
        url = absolute(_ld_str(item))
        if url:
            url = requote_uri(url)
            if url not in urls:
                urls.append(url)
    return urls


def _description(soup: BeautifulSoup, data: dict[str, Any]) -> str:
    text = _ld_str(data.get("description"))
    if not text:
        meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
            "meta", attrs={"name": "description"}
        )
        text = attr(meta, "content")
    value = str(text or "")
    if "&" in value:
        value = htmllib.unescape(value)
    # Keep paragraph breaks, collapse everything else.
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n|\r\n\s*\r\n", value.replace("\r\n", "\n"))]
    return "\n\n".join(p for p in paragraphs if p)


def _game_images(soup: BeautifulSoup, data: dict[str, Any]) -> tuple[str, str]:
    """(2:3 poster, wide hero) — see the module docstring for the identification order."""
    images = _ld_urls(data.get("image"))
    poster = ""
    for element in soup.find_all(class_=re.compile(r"aspect-2/3")):
        if element.find_parent("article") is not None:
            continue  # related-game cards share the class
        poster = image_url(element) or _picture_source(element)
        if poster:
            break
    if not poster:
        poster = next((u for u in images if "poster" in _filename(u)), "")
    hero = next((u for u in images if u != poster and "cover" in _filename(u)), "")
    if not hero:
        og_image = absolute(attr(soup.find("meta", attrs={"property": "og:image"}), "content"))
        hero = og_image if og_image and og_image != poster else ""
    remaining = [u for u in images if u not in (poster, hero)]
    if not poster and remaining:
        # Without other clues the site lists [wide, poster].
        poster = remaining[-1]
        remaining = remaining[:-1]
    if not hero and remaining:
        hero = remaining[0]
    return poster, hero


def _picture_source(element: Tag) -> str:
    sources = sorted(element.find_all("source"), key=lambda s: "webp" in (attr(s, "type") + attr(s, "srcset")).lower())
    for source in sources:
        url = absolute(first_srcset_url(attr(source, "srcset")))
        if url:
            return url
    return ""


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]).lower()


def _screenshots(data: dict[str, Any]) -> list[str]:
    return _ld_urls(data.get("screenshot")) if data.get("screenshot") else []


def _iso_date(text: str) -> str:
    text = clean(text)
    if not text:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _requirements(data: dict[str, Any]) -> SystemRequirements:
    raw_text = _ld_str(data.get("softwareRequirements")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw_text.split("\n")]
    lines = [clean(line) for line in lines if line]
    req = SystemRequirements(raw="\n".join(lines))
    for line in lines:
        match = _REQUIREMENT_LINE_RE.match(line)
        if not match:
            continue
        key = _REQUIREMENT_KEYS.get(match.group(1).strip().lower())
        if key and not getattr(req, key):
            setattr(req, key, match.group(2))
    fallbacks = {
        "processor": data.get("processorRequirements"),
        "memory": data.get("memoryRequirements"),
        "storage": data.get("storageRequirements"),
    }
    for key, value in fallbacks.items():
        if not getattr(req, key):
            setattr(req, key, clean(_ld_str(value)))
    return req


def _download_options(soup: BeautifulSoup) -> list[DownloadOption]:
    template = soup.find("template", attrs={"x-if": re.compile(r"^\s*downloadOpen\s*$")})
    scope: Tag = template if isinstance(template, Tag) else soup
    options: list[DownloadOption] = []
    seen: set[int] = set()
    for trigger in scope.find_all(_has_download_call):
        download_id = _download_id(trigger)
        if download_id is None or download_id in seen:
            continue
        seen.add(download_id)
        options.append(_make_download_option(download_id, _option_label(trigger)))
    # Every game has a full download; if no label looked like one (an unknown host name that
    # happens to contain an add-on word), the first non-patch option is it.
    if options and not any(o.kind is DownloadKind.FULL for o in options):
        for index, option in enumerate(options):
            if option.kind is not DownloadKind.PATCH:
                options[index] = replace(option, kind=DownloadKind.FULL)
                break
    return options


def _has_download_call(tag: Tag) -> bool:
    return any(isinstance(v, str) and "generateDownloadUrl(" in v for v in tag.attrs.values())


def _download_id(tag: Tag) -> int | None:
    for value in tag.attrs.values():
        if isinstance(value, str):
            match = _DOWNLOAD_CALL_RE.search(value)
            if match:
                return int(match.group(1))
    return None


def _option_label(trigger: Tag) -> str:
    container = trigger.find_parent("li") or (trigger.parent.parent if trigger.parent is not None else None)
    if container is None:
        return "Download"
    # The row's label cell sits just before the cell holding the button
    # (<div>DataNodes</div><div><a @click.prevent=…></div>). Rows can carry more
    # below it (e.g. "File integrity" MD5/SHA-256 hashes) that is not part of the label.
    node: Tag | None = trigger
    while node is not None and node is not container:
        for sibling in node.find_previous_siblings():
            if isinstance(sibling, Tag) and sibling.name not in ("script", "style", "template"):
                # Not get_text(): inside <template> the strings are TemplateString, which it skips.
                text = clean(" ".join(str(s) for s in sibling.find_all(string=True)))
                if text:
                    return text
        node = node.parent if isinstance(node.parent, Tag) else None
    parts = [
        clean(str(s))
        for s in container.find_all(string=True)
        if s.parent is not None
        and s.parent.name not in ("script", "style")
        and not any(parent is trigger for parent in s.parents)  # identity: Tag == is structural
    ]
    label = clean(" ".join(p for p in parts if p))
    return label or "Download"


def _make_download_option(download_id: int, label: str) -> DownloadOption:
    """Classify a download-modal label and extract size / version hints from it."""
    label = clean(label)
    kind = DownloadKind.classify(label)
    size_match = _OPTION_SIZE_RE.search(label)
    from_version = to_version = ""
    patch = _PATCH_RE.search(label)
    if patch:
        from_version, to_version = patch.group(1).rstrip(".-"), patch.group(2).rstrip(".-")
    else:
        version = _LABEL_VERSION_RE.search(label)
        if version:
            to_version = version.group(1).rstrip(".-")
    return DownloadOption(
        download_id=download_id,
        label=label,
        kind=kind,
        size_text=clean(size_match.group(1)) if size_match else "",
        from_version=from_version,
        to_version=to_version,
    )


def _torrent_available(soup: BeautifulSoup, html: str) -> bool:
    if _TORRENT_CALL_RE.search(html):
        return True
    heading = soup.find(["h3", "h4", "h5"], string=re.compile(r"^\s*torrent download\s*$", re.IGNORECASE))
    return heading is not None


# ---------------------------------------------------------------------------
# misc page fragments
# ---------------------------------------------------------------------------


def parse_csrf_token(html: str) -> str:
    """``<meta name="csrf-token">`` content, or "" when absent."""
    match = re.search(r"<meta\b[^>]*\bname\s*=\s*[\"']csrf-token[\"'][^>]*>", html or "", re.IGNORECASE)
    if match is None:
        return ""
    content = re.search(r"\bcontent\s*=\s*[\"']([^\"']*)[\"']", match.group(0), re.IGNORECASE)
    return clean(content.group(1)) if content else ""


def parse_ticket_page(html: str, *, ticket_url: str) -> TicketPage:
    """The download page. Raises ``SiteChangedError`` if ``downloadPage(...)`` is missing."""
    soup = make_soup(html)
    holder = soup.find(attrs={"x-data": re.compile(r"^\s*downloadPage\s*\(")})
    args = _js.call_arguments(attr(holder, "x-data"), "downloadPage") if holder is not None else None
    if not args:
        raise SiteChangedError(detail=f"downloadPage(...) not found on ticket page {ticket_url}")
    values = [_js.literal(a) for a in args]
    raw_url = values[0] if isinstance(values[0], str) else ""
    # The page script runs decodeURIComponent() on it before every use.
    file_url = absolute(unquote(raw_url), ticket_url or BASE_URL)
    if not file_url:
        raise SiteChangedError(detail=f"downloadPage(...) has no file URL on {ticket_url}")
    wait = values[5] if len(values) > 5 else 5  # the JS default
    widget = soup.find(attrs={"data-ag-turnstile": True})
    version, size_text = _ticket_facts(soup)
    return TicketPage(
        ticket_url=ticket_url,
        file_url=file_url,
        wait_seconds=_seconds(wait),
        requires_verification=widget is not None,
        turnstile_sitekey=attr(widget, "data-sitekey"),
        external_provider=_provider_name(values[1] if len(values) > 1 else None),
        is_torrent=_js_truthy(values[2]) if len(values) > 2 else False,
        version=version,
        size_text=size_text,
    )


def _seconds(value: Any) -> int:
    """``Math.max(0, Math.ceil(value))`` for the values a page can pass (numbers or numeric strings)."""
    if isinstance(value, str):
        try:
            value = float(value.strip() or "0")
        except ValueError:
            return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(0, math.ceil(value))


def _js_truthy(value: Any) -> bool:
    """JavaScript truthiness of a literal read by ``_js.literal`` (``{}``/``[]``/``"0"`` are truthy)."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def _provider_name(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, dict):
        name = value.get("name") or value.get("title") or value.get("label")
        return clean(str(name)) if name else "an external file host"
    if isinstance(value, str):
        match = re.search(r"""\bname\s*:\s*(['"])(.*?)\1""", value)
        if match:
            return clean(_js.unescape_string(match.group(2)))
        return clean(value) if not value.lstrip().startswith("{") else "an external file host"
    return "an external file host"


def _ticket_facts(soup: BeautifulSoup) -> tuple[str, str]:
    version = size_text = ""
    for label in soup.find_all(string=re.compile(r"^\s*(version|file size)\s*$", re.IGNORECASE)):
        holder = label.parent
        if holder is None:
            continue
        value_el = holder.find_previous_sibling()
        if value_el is None:
            continue
        titled = value_el if value_el.get("title") else value_el.find(attrs={"title": True})
        value = clean(attr(titled, "title")) or text_of(value_el)
        if label.strip().lower() == "version" and not version:
            version = value
        elif label.strip().lower() == "file size" and not size_text and is_size_text(value):
            size_text = value
    return version, size_text


def parse_login_form(html: str) -> dict[str, str]:
    """Hidden inputs of the login form (at least ``_token``) — used to build the POST body.

    Raises ``SiteChangedError`` when the page has no login form (e.g. it redirected).
    """
    soup = make_soup(html)
    for form in soup.find_all("form"):
        action_path = urlsplit(absolute(attr(form, "action"))).path.rstrip("/")
        has_password = form.find("input", attrs={"name": "password"}) is not None
        if action_path.endswith("/login") or has_password:
            fields: dict[str, str] = {}
            for field in form.find_all("input", attrs={"type": re.compile(r"^hidden$", re.IGNORECASE)}):
                name = attr(field, "name")
                if name:
                    fields[name] = attr(field, "value")
            return fields
    raise SiteChangedError(detail="Login form not found")


def parse_logged_in_user(html: str) -> UserInfo | None:
    """``UserInfo`` when the page is rendered for a signed-in user, else ``None`` (see module docstring)."""
    soup = make_soup(html)
    logout = _logout_marker(soup)
    page_auth = soup.find("meta", attrs={"name": "page-auth"})
    if attr(page_auth, "content").strip().lower() in ("0", "false", "guest", "no"):
        page_auth = None
    strong_login_link = _has_strong_login_link(soup)
    if logout is None and (page_auth is None or strong_login_link):
        return None
    if logout is not None and strong_login_link and page_auth is None:
        # Both a visible "Log in" link and a logout control: a guest template with hidden auth markup.
        return None
    if logout is not None:
        menu = logout.find_parent(["header", "nav", "aside"])
        return _account_info(_account_container(logout), menu)
    # Only ``meta page-auth``: the account menu can only be looked for in the site header.
    header = soup.find("header")
    return _account_info(header, header)


def _logout_marker(soup: BeautifulSoup) -> Tag | None:
    for form in soup.find_all("form"):
        if _is_path(attr(form, "action"), "/logout"):
            return form
    for link in soup.find_all("a", href=True):
        if _is_path(attr(link, "href"), "/logout"):
            return link
    return None


def _is_path(url: str, path: str) -> bool:
    if not url:
        return False
    try:
        return urlsplit(absolute(url)).path.rstrip("/") == path
    except ValueError:
        return False


def _has_strong_login_link(soup: BeautifulSoup) -> bool:
    for link in soup.find_all("a", href=True):
        if not _is_path(attr(link, "href"), "/login") or link.find_parent("form") is not None:
            continue
        guest_only = link.find_parent(class_=re.compile(r"guest-only")) is not None or "guest-only" in attr(
            link, "class"
        )
        if not guest_only:
            return True
    return False


def _account_info(container: Tag | None, menu: Tag | None) -> UserInfo:
    """Account details from ``container``; ``menu`` (enclosing header/nav) is the wider fallback scope."""
    scopes = [s for s in (container, menu) if s is not None]
    profile = next((p for s in scopes if (p := s.find("a", href=_PROFILE_HREF_RE)) is not None), None)
    avatar = next((a for s in scopes if (a := _avatar(s)) is not None), None)
    profile_url = absolute(attr(profile, "href"))
    name = text_of(profile)
    if not name or name.lower() in _GENERIC_ACCOUNT_LABELS or len(name) > 60:
        alt = clean(attr(avatar, "alt"))
        name = re.sub(r"(?:'s)?\s*(?:avatar|profile picture|profile image)$", "", alt, flags=re.IGNORECASE).strip()
    if not name and profile_url:
        name = unquote(urlsplit(profile_url).path.rstrip("/").rsplit("/", 1)[-1])
    container_text = text_of(container) if container else ""
    email_match = _EMAIL_RE.search(container_text)
    subscriber = bool(_SUBSCRIBER_RE.search(container_text)) and not _UPSELL_RE.search(container_text)
    return UserInfo(
        display_name=name,
        email=email_match.group(0) if email_match else "",
        avatar_url=image_url(avatar) if avatar is not None else "",
        profile_url=profile_url,
        is_subscriber=subscriber,
    )


def _account_container(anchor: Tag) -> Tag:
    """Nearest ancestor of the logout control holding a profile link or avatar.

    The walk never leaves the enclosing ``header``/``nav``/``aside`` (so comment
    authors elsewhere on the page are never mistaken for the account).
    """
    boundary = anchor.find_parent(["header", "nav", "aside"])
    node: Tag = anchor
    for _ in range(6):
        if node.find("a", href=_PROFILE_HREF_RE) is not None or _avatar(node) is not None:
            return node
        parent = node.parent
        if node is boundary or not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
            break
        node = parent
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


def _avatar(container: Tag) -> Tag | None:
    for img in container.find_all("img"):
        hint = " ".join((attr(img, "alt"), attr(img, "src"), attr(img, "class"))).lower()
        if any(word in hint for word in ("avatar", "gravatar", "profile", "user")):
            return img
    return None


def parse_livewire_config(html: str) -> dict[str, str]:
    """``window.livewireScriptConfig`` → ``{"csrf": …, "uri": ".../livewire-xxxx/update", …}``.

    Returns ``{}`` when the page has no (readable) config.
    """
    match = _LIVEWIRE_CONFIG_RE.search(html or "")
    if match is None:
        return {}
    try:
        data = json.loads(match.group(1))
    except ValueError:
        log.debug("Unreadable livewireScriptConfig")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in data.items() if isinstance(v, (str, int, float, bool))}


def parse_livewire_snapshots(html: str) -> dict[str, str]:
    """``{component name: raw snapshot JSON string}`` from ``wire:snapshot`` attributes (first wins)."""
    soup = make_soup(html)
    snapshots: dict[str, str] = {}
    for element in soup.find_all(attrs={"wire:snapshot": True}):
        raw = attr(element, "wire:snapshot")
        try:
            memo = json.loads(raw).get("memo") or {}
        except (ValueError, AttributeError):
            continue
        name = memo.get("name") if isinstance(memo, dict) else None
        if isinstance(name, str) and name and name not in snapshots:
            snapshots[name] = raw
    return snapshots

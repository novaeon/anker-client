import re
import json
import threading
import time
from collections.abc import Callable
from html import unescape as html_unescape
from urllib.parse import unquote, urljoin, urlparse
from bs4 import BeautifulSoup
from anker_client.config import BASE_URL


_SEARCH_CACHE_TTL_SECONDS = 10 * 60
_SEARCH_MAX_PAGES = 80
_SEARCH_MAX_RESULTS = 60
_page_cache: dict[str, tuple[float, list[dict], str | None]] = {}
_page_cache_lock = threading.RLock()
_page_fetch_lock = threading.Lock()


def _copy_games(games: list[dict]) -> list[dict]:
    """Keep callers from mutating shared cached search entries."""

    return [
        {
            **game,
            "genres": list(game.get("genres", [])),
        }
        for game in games
    ]


def parse_search_results(html: str) -> list[dict]:
    """
    Parse game cards from the /games search results page.
    Returns list of dicts with keys: title, slug, cover_url.
    """
    listing_results = parse_livewire_results(html)
    if listing_results:
        return listing_results

    soup = BeautifulSoup(html, "html.parser")

    # Collect all data keyed by slug, merging title and cover_url across
    # the multiple <a href="/game/{slug}"> tags that appear per card.
    slugs_ordered = []
    by_slug: dict[str, dict] = {}

    for a in soup.find_all("a", href=re.compile(r"(?:https?://[^/]+)?/game/[^/?#]+")):
        path = urlparse(a["href"]).path
        m = re.match(r"^/game/([^/?#]+)", path)
        if not m:
            continue
        slug = m.group(1).strip("/")

        if slug not in by_slug:
            slugs_ordered.append(slug)
            by_slug[slug] = {"title": None, "slug": slug, "cover_url": ""}

        entry = by_slug[slug]

        # Cover image: the <img> may be a sibling of this anchor inside its
        # parent container rather than a direct child, so search the parent div.
        if not entry["cover_url"]:
            # First try direct child
            img = a.find("img")
            # Then try the immediate parent container
            if not img and a.parent:
                img = a.parent.find("img")
            if img and img.get("src"):
                entry["cover_url"] = urljoin(BASE_URL, img["src"])

        # Title: look for h2/h3/h4 inside this anchor or nearby containers
        if not entry["title"]:
            if a.get("title"):
                entry["title"] = a["title"].strip()
                continue
            container = a
            for _ in range(5):
                if container is None:
                    break
                h = container.find(["h2", "h3", "h4"])
                if h:
                    entry["title"] = h.get_text(strip=True)
                    break
                container = container.parent

    # Return only complete entries (must have a title)
    return [by_slug[s] for s in slugs_ordered if by_slug[s]["title"]]


def parse_game_page(html: str) -> dict:
    """
    Parse a /game/{slug} page.
    Returns dict with: title, description, csrf_token, download_id, genres.
    """
    soup = BeautifulSoup(html, "html.parser")

    # CSRF token
    csrf_tag = soup.find("meta", {"name": "csrf-token"})
    csrf_token = csrf_tag.get("content") if csrf_tag else None

    # Download ID from Alpine.js @click.prevent="generateDownloadUrl(N)"
    download_id = None
    btn = soup.find(attrs={"@click.prevent": re.compile(r"generateDownloadUrl\(\d+\)")})
    if btn:
        m = re.search(r"generateDownloadUrl\((\d+)\)", btn.get("@click.prevent", ""))
        if m:
            download_id = int(m.group(1))

    # JSON-LD structured data for title, description, genres, screenshots
    title, description, genres, screenshots = None, None, [], []
    ld_tag = soup.find("script", {"type": "application/ld+json"})
    if ld_tag:
        try:
            ld = json.loads(ld_tag.string)
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            title = ld.get("name")
            description = ld.get("description", "")
            raw_genres = ld.get("genre", [])
            genres = raw_genres if isinstance(raw_genres, list) else [raw_genres]
            # Screenshots: may be URL strings or ImageObject dicts
            raw_shots = ld.get("screenshot", [])
            if isinstance(raw_shots, str):
                raw_shots = [raw_shots]
            for s in raw_shots:
                if isinstance(s, str) and s.startswith("http"):
                    screenshots.append(s)
                elif isinstance(s, dict):
                    url = s.get("url") or s.get("contentUrl", "")
                    if url and url.startswith("http"):
                        screenshots.append(url)
        except (json.JSONDecodeError, TypeError):
            pass

    # HTML fallback: match imgs whose alt text or URL path contains "screenshot"
    if not screenshots:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src", "")
            if not src or not src.startswith("http"):
                continue
            alt = (img.get("alt") or "").lower()
            if "screenshot" in alt or "screenshot" in src.lower():
                screenshots.append(src)

    # Fallback title from h1
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else None

    # File size: bare <span> containing a size value (e.g. "161.7 MB")
    # Game-card sizes use class="truncate ...", so classless spans are unique to this page.
    file_size = None
    m = re.search(r"<span>(\d+(?:\.\d+)?\s*(?:GB|MB|KB))</span>", html, re.IGNORECASE)
    if m:
        file_size = m.group(1)

    return {
        "title": title,
        "description": description,
        "csrf_token": csrf_token,
        "download_id": download_id,
        "genres": genres,
        "screenshots": screenshots,
        "file_size": file_size,
    }


def parse_livewire_results(html: str) -> list[dict]:
    """
    Parse game cards from a Livewire/listing rendered HTML fragment.

    Each card is a <div listing='{"type":"game","title":"...","slug":"...",
    "imageurl":"...","coverurl":"...","genres":[...],"size_gb":"...","release_date":"..."}'>
    Only type=game entries are returned.

    Returns list of dicts with keys: title, slug, cover_url, genres, size_gb, release_date.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for tag in soup.find_all(attrs={"listing": True}):
        try:
            data = json.loads(html_unescape(tag["listing"]))
        except (json.JSONDecodeError, KeyError):
            continue
        if data.get("type") != "game":
            continue
        slug = data.get("slug", "")
        title = data.get("title", "")
        cover_url = data.get("imageurl") or data.get("coverurl", "")
        if cover_url:
            cover_url = urljoin(BASE_URL, cover_url)
        # Extract optional metadata fields
        raw_genres = data.get("genres", [])
        genres = [g["title"] for g in raw_genres if isinstance(g, dict) and g.get("title")]
        size_gb = data.get("size_gb", "") or ""
        release_date = data.get("release_date", "") or ""
        if slug and title:
            result = {
                "title": title,
                "slug": slug,
                "cover_url": cover_url,
                "genres": genres,
                "size_gb": size_gb,
                "release_date": release_date,
            }
            if data.get("steam_app_id"):
                result["steam_app_id"] = str(data["steam_app_id"])
            if data.get("overview"):
                result["overview"] = data["overview"]
            results.append(result)
    return results


def _extract_next_page_url(html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        aria = (a.get("aria-label") or "").lower()
        if (text == "next" or "next" in aria) and "page=" in a["href"]:
            return urljoin(current_url, a["href"])
    return None


def _fetch_listing_page(session, url: str) -> tuple[list[dict], str | None]:
    now = time.time()
    with _page_cache_lock:
        cached = _page_cache.get(url)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            return _copy_games(cached[1]), cached[2]

    # Search and library refreshes can overlap.  Serialize a cache miss and
    # re-check once inside the lock so they never download the same page twice.
    with _page_fetch_lock:
        now = time.time()
        with _page_cache_lock:
            cached = _page_cache.get(url)
            if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
                return _copy_games(cached[1]), cached[2]

        resp = session.get(url, timeout=(5, 15))
        resp.raise_for_status()
        results = parse_search_results(resp.text)
        next_url = _extract_next_page_url(resp.text, url)

        with _page_cache_lock:
            _page_cache[url] = (now, results, next_url)
            # Pagination is currently bounded, but pruning also protects us if
            # the site starts emitting unstable query-string URLs.
            if len(_page_cache) > 128:
                oldest = sorted(_page_cache, key=lambda key: _page_cache[key][0])
                for key in oldest[:-128]:
                    _page_cache.pop(key, None)
        return _copy_games(results), next_url


def _matches_query(game: dict, query: str) -> bool:
    terms = [t for t in re.split(r"\s+", query.casefold().strip()) if t]
    if not terms:
        return True

    haystack_parts = [
        game.get("title", ""),
        game.get("slug", ""),
        game.get("steam_app_id", ""),
        " ".join(game.get("genres", [])),
    ]
    haystack = " ".join(str(p) for p in haystack_parts if p).casefold()
    return all(term in haystack for term in terms)


def search_games(
    session,
    query: str,
    max_results: int = _SEARCH_MAX_RESULTS,
    max_pages: int = _SEARCH_MAX_PAGES,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict]:
    """
    Search current AnkerGames listing pages by following pagination and
    filtering the embedded listing JSON locally.
    """
    matches: list[dict] = []
    seen_slugs: set[str] = set()
    url = f"{BASE_URL}/games"

    for _ in range(max_pages):
        if should_cancel and should_cancel():
            return []
        page_results, next_url = _fetch_listing_page(session, url)
        if should_cancel and should_cancel():
            return []
        for game in page_results:
            slug = game.get("slug")
            if not slug or slug in seen_slugs:
                continue
            if _matches_query(game, query):
                seen_slugs.add(slug)
                matches.append(game)
                if len(matches) >= max_results:
                    return matches

        if not next_url or next_url == url:
            break
        url = next_url

    return matches


def livewire_search(
    session,
    query: str,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict]:
    """
    Compatibility wrapper for the UI's existing search worker.

    The old Livewire /livewire/update route is no longer exposed by the site,
    so search now follows the public paginated listing pages and filters the
    embedded listing JSON locally.
    """
    return search_games(session, query, should_cancel=should_cancel)


def _fetch_fresh_csrf_token(session) -> str:
    resp = session.get(f"{BASE_URL}/csrf-token", timeout=10)
    resp.raise_for_status()
    try:
        token = resp.json().get("token")
    except (AttributeError, ValueError, TypeError) as exc:
        raise RuntimeError("Fresh CSRF token response was not valid JSON") from exc
    if not token:
        raise RuntimeError("Fresh CSRF token response did not include a token")
    return token


def _request_treasure_box_url(session, download_id: int, csrf_token: str) -> str:
    resp = session.post(
        f"{BASE_URL}/generate-download-url/{download_id}",
        json={},
        headers={
            "X-CSRF-TOKEN": csrf_token,
            "Content-Type": "application/json",
            "Referer": BASE_URL,
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    treasure_box_url = data.get("download_url") or data.get("url")
    if not treasure_box_url:
        raise RuntimeError(f"No download URL in response: {data}")
    return treasure_box_url


def _normalize_download_url(value: str) -> str:
    """Decode the percent-, HTML-, and JavaScript-escaped download URL."""

    url = html_unescape(unquote(value.strip()))
    # Alpine state is sometimes emitted from JSON without unescaping its
    # forward slashes, producing ``https:\/\/host\/path`` as literal text.
    url = url.replace(r"\/", "/")
    url = re.sub(r"\\u002[fF]", "/", url)

    if url.startswith("/"):
        url = urljoin(BASE_URL, url)

    parsed = urlparse(url)
    try:
        hostname = parsed.hostname
    except ValueError:
        hostname = None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise RuntimeError(f"Unexpected download URL extracted: {url[:120]}")
    return url


def get_download_url(session, download_id: int, csrf_token: str) -> str:
    """
    Resolve the real CDN download URL for a game.

    The site uses a two-step process:
      1. POST /generate-download-url/{id} → "treasure-box" page URL
         (https://ankergames.net/download/{signed-token}/{hash})
      2. GET that page → parse the real CDN URL from the Alpine.js
         downloadPage('URL_ENCODED_CDN_URL', ...) initializer

    Raises RuntimeError on any failure.
    """
    # Step 1: get the treasure-box page URL. Current cached guest pages often
    # contain a stale meta CSRF token, so retry once with /csrf-token on 419.
    try:
        treasure_box_url = _request_treasure_box_url(session, download_id, csrf_token)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) != 419:
            raise
        fresh_token = _fetch_fresh_csrf_token(session)
        treasure_box_url = _request_treasure_box_url(session, download_id, fresh_token)

    # Step 2: GET the treasure-box page and extract the real CDN URL.
    tb_resp = session.get(treasure_box_url, timeout=15)
    tb_resp.raise_for_status()

    m = re.search(r"downloadPage\('([^']+)'", tb_resp.text)
    if not m:
        raise RuntimeError(
            "Could not find download URL in treasure-box page. "
            "The site may have changed its download page structure."
        )

    return _normalize_download_url(m.group(1))

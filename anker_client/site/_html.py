"""BeautifulSoup plumbing shared by the site parsers (no network, no models)."""

from __future__ import annotations

import html as htmllib
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from anker_client.constants import BASE_URL

try:  # lxml is a hard dependency, but keep parsing alive if a frozen build lacks it.
    import lxml  # noqa: F401

    PARSER = "lxml"
except ImportError:  # pragma: no cover - environment dependent
    PARSER = "html.parser"

# Alpine's ``@event="…"`` shorthand is not a valid attribute name for libxml2, which
# silently drops it; ``x-on:event`` is the long form and survives parsing.
_ALPINE_EVENT_ATTR_RE = re.compile(r"(?<=\s)@(?=[A-Za-z][\w.:-]*\s*=)")
# Livewire wraps every Blade @if in these marker comments (~1,300 per listing page) and the
# markup is heavily indented; dropping both before parsing makes BeautifulSoup ~45% faster.
_LIVEWIRE_MARKERS = ("<!--[if BLOCK]><![endif]-->", "<!--[if ENDBLOCK]><![endif]-->")
_INTER_TAG_WHITESPACE_RE = re.compile(r">\s+<")
_GAME_PATH_RE = re.compile(r"/game/([^/?#\s]+)/?$")
_SIZE_TEXT_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\s*$", re.IGNORECASE)
_PAGE_AUTH_META_RE = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']page-auth[\"']", re.IGNORECASE)


def make_soup(html: str, *, keep_alpine_events: bool = False) -> BeautifulSoup:
    """Parse once with lxml. ``keep_alpine_events`` rewrites ``@click`` → ``x-on:click`` first.

    Whitespace-only text between tags is dropped (callers use ``get_text(" ")`` / ``clean``).
    """
    if keep_alpine_events and "@" in html:
        html = _ALPINE_EVENT_ATTR_RE.sub("x-on:", html)
    for marker in _LIVEWIRE_MARKERS:
        html = html.replace(marker, "")
    html = _INTER_TAG_WHITESPACE_RE.sub("><", html)
    return BeautifulSoup(html, PARSER)


def clean(text: str | None) -> str:
    """Unescape leftover HTML entities (the site double-encodes some) and collapse whitespace."""
    if not text:
        return ""
    value = str(text)
    if "&" in value:
        value = htmllib.unescape(value)
    return " ".join(value.split())


def text_of(element: Tag | None) -> str:
    return clean(element.get_text(" ", strip=True)) if element is not None else ""


def attr(element: Tag | None, name: str) -> str:
    """Attribute as a string ('' when missing; multi-valued attributes joined by spaces)."""
    if element is None:
        return ""
    value = element.get(name)
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(value)
    return str(value)


def absolute(url: str, base: str = BASE_URL) -> str:
    url = (url or "").strip().replace("\\/", "/")
    if not url or url.startswith(("data:", "javascript:", "#")):
        return ""
    return urljoin(base if base.endswith("/") else base + "/", url)


def slug_from_url(url: str) -> str:
    """``https://ankergames.net/game/hollow-knight`` → ``hollow-knight`` ('' for other URLs)."""
    url = (url or "").replace("\\/", "/").strip()
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    match = _GAME_PATH_RE.search(path)
    if not match:
        return ""
    slug = unquote(match.group(1)).strip()
    return slug if slug and "/" not in slug else ""


def page_param(url: str) -> int | None:
    try:
        values = parse_qs(urlsplit(url).query).get("page")
    except ValueError:
        return None
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def first_srcset_url(srcset: str) -> str:
    candidate = (srcset or "").split(",")[0].strip()
    return candidate.split()[0] if candidate else ""


def image_url(container: Tag | None, base: str = BASE_URL) -> str:
    """Best image URL inside ``container``: ``<img src>`` (jpg/png), else a ``<source srcset>`` (non-WebP first)."""
    if container is None:
        return ""
    img = container if container.name == "img" else container.find("img")
    src = absolute(attr(img, "src"), base) if img is not None else ""
    if src:
        return src
    picture = img.find_parent("picture") if img is not None else None
    sources = (picture or container).find_all("source")
    ordered = sorted(sources, key=lambda s: "webp" in attr(s, "type").lower())
    for source in ordered:
        url = absolute(first_srcset_url(attr(source, "srcset")), base)
        if url:
            return url
    return ""


def is_size_text(text: str) -> bool:
    return bool(_SIZE_TEXT_RE.match(text or ""))


def has_page_auth_meta(html: str) -> bool:
    """True when the origin rendered the page for a signed-in user (``<meta name="page-auth">``).

    Guest pages are served from Cloudflare's edge cache and never carry it.
    """
    return bool(_PAGE_AUTH_META_RE.search(html or ""))


def form_errors(html: str) -> list[str]:
    """Validation messages Laravel renders next to form fields (best effort, de-duplicated)."""
    soup = make_soup(html)
    messages: list[str] = []
    candidates = soup.select(
        '[role="alert"], .invalid-feedback, .error, .errors li, ul.text-red-600 li, '
        'p.text-red-600, p.text-red-500, div.text-red-600, div.text-red-500, span.text-red-600'
    )
    for element in candidates:
        text = text_of(element)
        if text and len(text) < 300 and text not in messages:
            messages.append(text)
    return messages

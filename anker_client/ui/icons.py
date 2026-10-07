"""Inline SVG icon set (24×24, 2px stroke, Lucide-style) rendered in any colour.

``icon("download")`` returns a QIcon tinted with the current theme's text
colour (call ``set_default_color`` when the theme changes); pass ``color`` to
override. Unknown names return an empty QIcon and log once.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

_PATHS: dict[str, str] = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "store": '<path d="M3 9h18l-1.5-5h-15z"/><path d="M4 9v11h16V9"/><path d="M9 20v-6h6v6"/>',
    "library": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
               '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
                'a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3'
                'l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1'
                'a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9'
                'a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8'
                'l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "play": '<path d="M7 4v16l13-8z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    "resume": '<path d="M7 4v16l13-8z"/>',
    "cancel": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "retry": '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/>'
             '<path d="M3 21v-5h5"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/>'
               '<path d="M3 21v-5h5"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 15h10l1-15"/><path d="M10 11v6M14 11v6"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "external": '<path d="M14 3h7v7"/><path d="M10 14 21 3"/><path d="M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7'
                'a2 2 0 0 1 2-2h5"/>',
    "heart": '<path d="M12 20s-7-4.4-9.2-8.6C1.3 8.4 3 5 6.4 5c2 0 3.3 1.1 4 2.2.7-1.1 2-2.2 4-2.2C17.8 5 19.5 8.4 18 11.4'
             '17.8 11.8 12 20 12 20z"/>',
    "heart_filled": '<path fill="currentColor" d="M12 20s-7-4.4-9.2-8.6C1.3 8.4 3 5 6.4 5c2 0 3.3 1.1 4 2.2.7-1.1 2-2.2 '
                    '4-2.2C17.8 5 19.5 8.4 18 11.4 17.8 11.8 12 20 12 20z"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>',
    "star_filled": '<path fill="currentColor" d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6'
                   'l6.2-.9z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "login": '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5"/><path d="M12 8h.01"/>',
    "warning": '<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "check_circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "filter": '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
    "sort": '<path d="M7 4v16"/><path d="m3 8 4-4 4 4"/><path d="M17 20V4"/><path d="m21 16-4 4-4-4"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
    "update": '<path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M4 15a8 8 0 0 0 16 0"/>',
    "shortcut": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 15 15 9"/><path d="M10 9h5v5"/>',
    "chevron_left": '<path d="m15 18-6-6 6-6"/>',
    "chevron_right": '<path d="m9 18 6-6-6-6"/>',
    "chevron_down": '<path d="m6 9 6 6 6-6"/>',
    "chevron_up": '<path d="m18 15-6-6-6 6"/>',
    "arrow_left": '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    "arrow_up": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    "arrow_down": '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "hdd": '<path d="M22 12H2"/><path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/><path d="M6 16h.01M10 16h.01"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "tag": '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="8" cy="8" r="1.5"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
    "cpu": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/>'
           '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "package": '<path d="m12 3 9 5v8l-9 5-9-5V8z"/><path d="m3 8 9 5 9-5"/><path d="M12 13v8"/>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye_off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1"/>'
               '<path d="M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "link": '<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
    "archive": '<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/>'
               '<path d="M10 12h4"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    "palette": '<circle cx="12" cy="12" r="9"/><circle cx="7.5" cy="10.5" r="1"/><circle cx="12" cy="7.5" r="1"/>'
               '<circle cx="16.5" cy="10.5" r="1"/><path d="M12 21a2 2 0 0 1 0-4h2a3 3 0 0 0 3-3"/>',
    "sparkles": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.7 2.3L22 18'
                'l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7z"/>',
    "flame": '<path d="M12 22a7 7 0 0 0 7-7c0-4-3-6-4-10-2 2-3 4-3 6-1-1-2-2-2-4-2 2-5 5-5 8a7 7 0 0 0 7 7z"/>',
    "trophy": '<path d="M8 21h8M12 17v4"/><path d="M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M17 5h3v2a3 3 0 0 1-3 3"/>'
              '<path d="M7 5H4v2a3 3 0 0 0 3 3"/>',
    "vr": '<rect x="2" y="7" width="20" height="11" rx="3"/><circle cx="8" cy="12.5" r="2"/><circle cx="16" cy="12.5" r="2"/>',
    "discord": '<path fill="currentColor" stroke="none" d="M19.3 5.3A16.5 16.5 0 0 0 15.2 4l-.5 1a15 15 0 0 0-5.4 0l-.5-1'
               'a16.5 16.5 0 0 0-4.1 1.3C2.1 9.2 1.4 13 1.7 16.8A16.7 16.7 0 0 0 6.8 19.4l1.1-1.7a10.7 10.7 0 0 1-1.7-.8'
               'l.4-.3a11.8 11.8 0 0 0 10.8 0l.4.3a10.7 10.7 0 0 1-1.7.8l1.1 1.7a16.6 16.6 0 0 0 5.1-2.6c.4-4.4-.7-8.2-3-11.5z'
               'M8.7 14.5c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2zm6.6 0c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2'
               '-.8 2-1.8 2z"/>',
    "tray": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/>',
    "github": '<path d="M9 19c-4 1.5-4-2-6-2.5M15 22v-3.5a3 3 0 0 0-.9-2.4c3-.3 6-1.5 6-6.5a5 5 0 0 0-1.4-3.6 4.7 4.7 0 '
              '0 0-.1-3.5s-1.1-.3-3.6 1.4a12.4 12.4 0 0 0-6.4 0C6.1 1.2 5 1.5 5 1.5a4.7 4.7 0 0 0-.1 3.5A5 5 0 0 0 3.5 '
              '8.6c0 5 3 6.2 6 6.5a3 3 0 0 0-.9 2.4V22"/>',
}

ICON_NAMES = frozenset(_PATHS)

_default_color = QColor("#e6e8ee")
_warned: set[str] = set()


def set_default_color(color: str | QColor) -> None:
    """Called by the theme manager whenever the palette changes."""
    global _default_color
    _default_color = QColor(color)
    _render.cache_clear()


def svg_markup(name: str, color: str, opacity: float = 1.0) -> str:
    """SVG source for ``name``; ``color`` must be ``#rrggbb`` (SVG has no ARGB syntax)."""
    body = _PATHS[name].replace("currentColor", color)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="{opacity:.3f}">{body}</svg>'
    )


@lru_cache(maxsize=512)
def _render(name: str, color: str, alpha: int, size: int, dpr: float) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_markup(name, color, alpha / 255).encode()))
    pixmap = QPixmap(QSize(int(size * dpr), int(size * dpr)))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def pixmap(name: str, size: int = 20, color: str | QColor | None = None, dpr: float = 2.0) -> QPixmap:
    if name not in _PATHS:
        if name not in _warned:
            _warned.add(name)
            log.warning("Unknown icon %r", name)
        return QPixmap()
    c = QColor(color) if color is not None else _default_color
    return _render(name, c.name(QColor.NameFormat.HexRgb), c.alpha(), size, dpr)


def icon(name: str, color: str | QColor | None = None, *, disabled_color: str | QColor | None = None) -> QIcon:
    """Multi-size QIcon for buttons, menus and the sidebar."""
    result = QIcon()
    if name not in _PATHS:
        pixmap(name)  # logs once
        return result
    for size in (16, 20, 24, 32):
        result.addPixmap(pixmap(name, size, color), QIcon.Mode.Normal)
        if disabled_color is not None:
            result.addPixmap(pixmap(name, size, disabled_color), QIcon.Mode.Disabled)
    return result

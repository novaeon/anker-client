"""Theme colour tokens. QSS and custom painting must use these — never hard-code colours.

``current()`` returns the active palette (updated by ``ThemeManager.apply``).
Widgets that paint themselves (delegates, cards, badges) read ``current()`` at
paint time so a theme switch only needs a repaint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    key: str
    name: str
    dark: bool
    bg: str  # window background
    surface: str  # panels, sidebar, cards
    surface_alt: str  # inputs, hovered rows, secondary panels
    elevated: str  # popups, menus, dialogs, toasts
    border: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_text: str  # text drawn on accent
    success: str
    warning: str
    danger: str
    info: str
    overlay: str  # scrims behind modals / text over artwork (rgba)
    font_family: str = '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
    mono_family: str = '"Cascadia Mono", Consolas, monospace'
    radius: int = 8  # px; small radius = radius // 2
    wallpaper: str = ""  # resource file name (anker_client/resources) or ""
    surface_alpha: int = 255  # <255 makes panels translucent over the wallpaper

    @property
    def swatches(self) -> tuple[str, str, str]:
        return (self.bg, self.surface, self.accent)


THEMES: dict[str, Palette] = {
    p.key: p
    for p in (
        Palette(
            key="midnight", name="Midnight", dark=True,
            bg="#0e1014", surface="#15181e", surface_alt="#1c2028", elevated="#222731", border="#2a303b",
            text="#e7e9ee", text_muted="#9aa3b2", text_faint="#666f7e",
            accent="#4f8cff", accent_hover="#6c9fff", accent_text="#ffffff",
            success="#3ecf8e", warning="#f5a524", danger="#f0505f", info="#4f8cff",
            overlay="rgba(6, 8, 12, 0.72)",
        ),
        Palette(
            key="daylight", name="Daylight", dark=False,
            bg="#f3f4f7", surface="#ffffff", surface_alt="#eceef3", elevated="#ffffff", border="#dcdfe6",
            text="#16191f", text_muted="#59616f", text_faint="#8c94a2",
            accent="#2f6fec", accent_hover="#255fd1", accent_text="#ffffff",
            success="#16a26b", warning="#c97a00", danger="#d93848", info="#2f6fec",
            overlay="rgba(20, 24, 32, 0.55)",
        ),
        Palette(
            key="classic_steam", name="Classic Steam", dark=True,
            bg="#1b2838", surface="#171d25", surface_alt="#2a3f5a", elevated="#233246", border="#2f4560",
            text="#c7d5e0", text_muted="#8f98a0", text_faint="#5f6b75",
            accent="#66c0f4", accent_hover="#8fd3ff", accent_text="#0e1a26",
            success="#a4d007", warning="#e5b143", danger="#d94a4a", info="#66c0f4",
            overlay="rgba(10, 16, 24, 0.75)",
            font_family='Arial, "Segoe UI", sans-serif', radius=3,
        ),
        Palette(
            key="terminal", name="Terminal", dark=True,
            bg="#050805", surface="#0a100a", surface_alt="#0f1a0f", elevated="#0d150d", border="#1d3a1d",
            text="#39ff6a", text_muted="#25b84c", text_faint="#1a7a34",
            accent="#39ff6a", accent_hover="#7dff9c", accent_text="#031003",
            success="#39ff6a", warning="#e7ff39", danger="#ff4d4d", info="#39d0ff",
            overlay="rgba(0, 0, 0, 0.8)",
            font_family='"Cascadia Mono", Consolas, "Courier New", monospace', radius=0,
        ),
        Palette(
            key="vaporwave", name="Vaporwave", dark=True,
            bg="#170a2a", surface="#21103d", surface_alt="#2d1652", elevated="#2a1450", border="#5b2a8a",
            text="#fbe7ff", text_muted="#d3a6e6", text_faint="#9670ad",
            accent="#ff71ce", accent_hover="#ff9ddc", accent_text="#1a0624",
            success="#05ffa1", warning="#fffb96", danger="#ff4f7a", info="#01cdfe",
            overlay="rgba(23, 10, 42, 0.7)",
            wallpaper="vaporwave.gif", surface_alpha=215, radius=10,
        ),
        Palette(
            key="bliss_xp", name="Bliss XP", dark=False,
            bg="#3a6ea5", surface="#ece9d8", surface_alt="#ffffff", elevated="#f6f5ee", border="#7f9db9",
            text="#000000", text_muted="#3d3d3d", text_faint="#6d6d6d",
            accent="#316ac5", accent_hover="#4a85de", accent_text="#ffffff",
            success="#2c8a2c", warning="#b8860b", danger="#c42b1c", info="#316ac5",
            overlay="rgba(0, 0, 0, 0.45)",
            font_family='Tahoma, "Segoe UI", sans-serif', radius=4,
            wallpaper="bliss.png", surface_alpha=235,
        ),
    )
}

DEFAULT_THEME = "midnight"

_current: Palette = THEMES[DEFAULT_THEME]


def get(key: str) -> Palette:
    return THEMES.get(key) or THEMES[DEFAULT_THEME]


def current() -> Palette:
    return _current


def set_current(key: str) -> Palette:
    global _current
    _current = get(key)
    return _current

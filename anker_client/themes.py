# anker_client/themes.py
"""
Application themes.  Each entry has:
  name     – display name shown in settings
  swatches – three representative hex colours for the preview chips
  qss      – Qt stylesheet applied to the QApplication
"""


def _combobox(
    bg: str, color: str, border: str, radius: str,
    focus: str, arrow: str, dropdown_bg: str, selection: str,
    font: str = "",
) -> str:
    font_line = f"\n    font-family: {font};" if font else ""
    return f"""
QComboBox {{{font_line}
    background: {bg};
    color: {color};
    border: 1px solid {border};
    border-radius: {radius};
    padding: 5px 8px;
    min-height: 20px;
}}
QComboBox:focus {{ border-color: {focus}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border-left: 1px solid {border};
    border-top-right-radius: {radius};
    border-bottom-right-radius: {radius};
}}
QComboBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {arrow};
    width: 0;
    height: 0;
}}
QComboBox QAbstractItemView {{
    background: {dropdown_bg};
    color: {color};
    border: 1px solid {border};
    selection-background-color: {selection};
    selection-color: {color};
    outline: none;
}}
"""


def _scrollbar(track: str, handle: str, hover: str) -> str:
    return f"""
QScrollBar:vertical {{
    background: {track};
    width: 8px;
    border: none;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {handle};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {hover};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {track};
    height: 8px;
    border: none;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {handle};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
"""


# ---------------------------------------------------------------------------
# DEFAULT DARK
# ---------------------------------------------------------------------------
_DEFAULT_QSS = """
QWidget {
    background-color: #0f172a;
    color: #e2e8f0;
    font-size: 12px;
}
QMainWindow { background-color: #0f172a; }
QDialog      { background-color: #0f172a; }

QTabWidget::pane {
    background: #0f172a;
    border: 1px solid #1e293b;
    border-top: none;
}
QTabBar { background: #0f172a; }
QTabBar::tab {
    background: #1e293b;
    color: #94a3b8;
    padding: 8px 18px;
    border: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #0f172a;
    color: #e2e8f0;
    border-bottom: 2px solid #3b82f6;
    font-weight: bold;
}
QTabBar::tab:hover:!selected { color: #cbd5e1; background: #1e293b; }

QPushButton {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 20px;
}
QPushButton:hover   { background: #334155; border-color: #475569; }
QPushButton:pressed { background: #0f172a; }
QPushButton:disabled { color: #475569; border-color: #1e293b; background: #0f172a; }

QLineEdit {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #3b82f6;
}
QLineEdit:focus { border-color: #3b82f6; }

QSplitter::handle          { background: #1e293b; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

QStatusBar {
    background: #0f172a;
    color: #64748b;
    border-top: 1px solid #1e293b;
}

QMenu {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    padding: 4px;
}
QMenu::item          { padding: 6px 20px; border-radius: 3px; }
QMenu::item:selected { background: #334155; }
QMenu::separator     { height: 1px; background: #334155; margin: 3px 8px; }

QScrollArea  { border: none; background: transparent; }
QMessageBox  { background: #1e293b; }
""" + _scrollbar("#1e293b", "#475569", "#64748b") + _combobox(
    bg="#1e293b", color="#e2e8f0", border="#334155", radius="4px",
    focus="#3b82f6", arrow="#3b82f6", dropdown_bg="#1e293b", selection="#334155",
)

# ---------------------------------------------------------------------------
# BLISS XP
# Deep sky-blue inspired by the Bliss wallpaper; hill-green buttons.
# ---------------------------------------------------------------------------
_BLISS_XP_QSS = """
QWidget {
    background-color: transparent;
    color: #d8eeff;
    font-family: Tahoma, Arial, sans-serif;
    font-size: 12px;
}
QMainWindow { background-color: transparent; }
QDialog      { background-color: rgba(26, 51, 85, 0.96); }

QTabWidget          { background: transparent; }
QTabWidget::pane {
    background: rgba(18, 37, 64, 0.82);
    border: 1px solid rgba(42, 90, 138, 0.7);
    border-top: none;
}
QTabBar { background: transparent; }
QTabBar::tab {
    background: rgba(30, 61, 102, 0.88);
    color: #7ab0d8;
    padding: 8px 18px;
    border: 1px solid rgba(42, 90, 138, 0.7);
    border-bottom: none;
    margin-right: 2px;
    font-family: Tahoma, Arial, sans-serif;
}
QTabBar::tab:selected {
    background: rgba(18, 37, 64, 0.82);
    color: #d8eeff;
    border-bottom: 3px solid #4aaa4a;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background: rgba(30, 74, 122, 0.92);
    color: #d8eeff;
}

QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a9a4a, stop:1 #2a7a3a);
    color: #d8f4d8;
    border: 1px solid #4aaa5a;
    border-radius: 3px;
    padding: 5px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #4aaa5a, stop:1 #3a9a4a);
    border-color: #6acc6a;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1a6a2a, stop:1 #2a8a3a);
}
QPushButton:disabled { background: rgba(30, 61, 74, 0.7); color: #4a7a5a; border-color: #2a4a3a; }

QLineEdit {
    background: rgba(18, 37, 64, 0.90);
    color: #d8eeff;
    border: 1px solid #3a7abf;
    border-radius: 3px;
    padding: 5px 8px;
    selection-background-color: #4aaa4a;
}
QLineEdit:focus { border-color: #7ab8ee; }

QSplitter::handle          { background: rgba(42, 74, 106, 0.6); }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

QStatusBar {
    background: rgba(18, 37, 64, 0.88);
    color: #6a9abf;
    border-top: 1px solid rgba(42, 80, 128, 0.6);
}

QMenu {
    background: rgba(30, 61, 102, 0.96);
    color: #d8eeff;
    border: 1px solid #2a5a8a;
    padding: 4px;
}
QMenu::item          { padding: 6px 20px; border-radius: 3px; }
QMenu::item:selected { background: #3a9a4a; color: #d8f4d8; }
QMenu::separator     { height: 1px; background: #2a5080; margin: 3px 8px; }

QScrollArea  { border: none; background: transparent; }
QMessageBox  { background: rgba(26, 51, 85, 0.96); }
""" + _scrollbar("transparent", "#3a7abf", "#5a9adf") + _combobox(
    bg="rgba(18, 37, 64, 0.90)", color="#d8eeff", border="#3a7abf", radius="3px",
    focus="#7ab8ee", arrow="#4aaa4a", dropdown_bg="rgba(18, 37, 64, 0.96)", selection="#3a9a4a",
)

# ---------------------------------------------------------------------------
# CLASSIC STEAM  (circa 2005)
# Charcoal grays, metallic gradient buttons, classic Steam green accent.
# ---------------------------------------------------------------------------
_CLASSIC_STEAM_QSS = """
QWidget {
    background-color: #1a1a1a;
    color: #c8c8c8;
    font-family: Tahoma, Arial, sans-serif;
    font-size: 12px;
}
QMainWindow { background-color: #0f0f0f; }
QDialog      { background-color: #1a1a1a; }

QTabWidget::pane {
    background: #1f1f1f;
    border: 1px solid #444444;
    border-top: none;
}
QTabBar { background: #0f0f0f; }
QTabBar::tab {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3d3d3d, stop:1 #2a2a2a);
    color: #a0a0a0;
    padding: 7px 16px;
    border: 1px solid #505050;
    border-bottom: none;
    margin-right: 1px;
    font-family: Tahoma, Arial, sans-serif;
}
QTabBar::tab:selected {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2a2a2a, stop:1 #1f1f1f);
    color: #ffffff;
    border-bottom: 2px solid #8db651;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #4a4a4a, stop:1 #333333);
    color: #c8c8c8;
}

QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #4a4a4a, stop:1 #333333);
    color: #c8c8c8;
    border: 1px solid #606060;
    border-radius: 2px;
    padding: 5px 14px;
    min-height: 20px;
    font-family: Tahoma, Arial, sans-serif;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #5a5a5a, stop:1 #424242);
    border-color: #8db651;
    color: #ffffff;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #222222, stop:1 #2a2a2a);
}
QPushButton:disabled { background: #252525; color: #555555; border-color: #383838; }

QLineEdit {
    background: #0f0f0f;
    color: #c8c8c8;
    border: 1px solid #505050;
    border-radius: 2px;
    padding: 5px 8px;
    selection-background-color: #4a7abf;
    font-family: Tahoma, Arial, sans-serif;
}
QLineEdit:focus { border-color: #8db651; }

QSplitter::handle          { background: #383838; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

QStatusBar {
    background: #0f0f0f;
    color: #707070;
    border-top: 1px solid #383838;
    font-family: Tahoma, Arial, sans-serif;
    font-size: 11px;
}

QMenu {
    background: #2d2d2d;
    color: #c8c8c8;
    border: 1px solid #505050;
    padding: 3px;
    font-family: Tahoma, Arial, sans-serif;
}
QMenu::item          { padding: 5px 20px; }
QMenu::item:selected { background: #4a7abf; color: #ffffff; }
QMenu::separator     { height: 1px; background: #444444; margin: 3px 8px; }

QScrollArea  { border: none; background: transparent; }
QMessageBox  { background: #2d2d2d; }
""" + _scrollbar("#0f0f0f", "#505050", "#8db651") + _combobox(
    bg="#0f0f0f", color="#c8c8c8", border="#505050", radius="2px",
    focus="#8db651", arrow="#8db651", dropdown_bg="#2d2d2d", selection="#4a7abf",
    font="Tahoma, Arial, sans-serif",
)

# ---------------------------------------------------------------------------
# TERMINAL
# ---------------------------------------------------------------------------
_TERMINAL_QSS = """
QWidget {
    background-color: #000000;
    color: #00ff41;
    font-family: "Courier New", Consolas, "Lucida Console", monospace;
    font-size: 12px;
}
QMainWindow { background-color: #000000; }
QDialog      { background-color: #000000; }

QTabWidget::pane {
    background: #000000;
    border: 1px solid #00aa2a;
    border-top: none;
}
QTabBar { background: #000000; }
QTabBar::tab {
    background: #001a00;
    color: #00aa2a;
    padding: 8px 18px;
    border: 1px solid #007a1a;
    border-bottom: none;
    margin-right: 2px;
    font-family: "Courier New", Consolas, monospace;
}
QTabBar::tab:selected {
    background: #000000;
    color: #00ff41;
    border-bottom: 2px solid #00ff41;
    font-weight: bold;
}
QTabBar::tab:hover:!selected { background: #002a00; color: #00ff41; }

QPushButton {
    background: #000000;
    color: #00ff41;
    border: 1px solid #00aa2a;
    border-radius: 0px;
    padding: 5px 14px;
    min-height: 20px;
    font-family: "Courier New", Consolas, monospace;
}
QPushButton:hover   { background: #002a00; border-color: #00ff41; color: #ffffff; }
QPushButton:pressed { background: #001500; }
QPushButton:disabled { color: #005500; border-color: #003300; }

QLineEdit {
    background: #000000;
    color: #00ff41;
    border: 1px solid #00aa2a;
    border-radius: 0px;
    padding: 5px 8px;
    selection-background-color: #00aa2a;
    font-family: "Courier New", Consolas, monospace;
}
QLineEdit:focus { border-color: #00ff41; }

QSplitter::handle          { background: #007a1a; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

QStatusBar {
    background: #000000;
    color: #00aa2a;
    border-top: 1px solid #007a1a;
    font-family: "Courier New", Consolas, monospace;
}

QMenu {
    background: #001a00;
    color: #00ff41;
    border: 1px solid #00aa2a;
    padding: 4px;
    font-family: "Courier New", Consolas, monospace;
}
QMenu::item          { padding: 6px 20px; }
QMenu::item:selected { background: #003300; color: #00ff41; }
QMenu::separator     { height: 1px; background: #007a1a; margin: 3px 8px; }

QScrollArea  { border: none; background: #000000; }
QMessageBox  { background: #001a00; color: #00ff41; }
""" + _scrollbar("#000000", "#00aa2a", "#00ff41") + _combobox(
    bg="#000000", color="#00ff41", border="#00aa2a", radius="0px",
    focus="#00ff41", arrow="#00ff41", dropdown_bg="#001a00", selection="#003300",
    font='"Courier New", Consolas, monospace',
)

# ---------------------------------------------------------------------------
# VAPORWAVE
# ---------------------------------------------------------------------------
_VAPORWAVE_QSS = """
QWidget {
    background-color: transparent;
    color: #e8d5f5;
    font-size: 12px;
}
QMainWindow { background-color: transparent; }
QDialog      { background-color: rgba(13, 2, 33, 0.96); }

QTabWidget::pane {
    background: rgba(13, 2, 33, 0.82);
    border: 1px solid rgba(185, 103, 255, 0.55);
    border-top: none;
}
QTabWidget { background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: rgba(24, 5, 53, 0.90);
    color: #a060c0;
    padding: 8px 18px;
    border: 1px solid rgba(185, 103, 255, 0.45);
    border-bottom: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: rgba(13, 2, 33, 0.86);
    color: #ff71ce;
    border-bottom: 2px solid #ff71ce;
    font-weight: bold;
}
QTabBar::tab:hover:!selected { background: rgba(30, 5, 69, 0.94); color: #d080f0; }

QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #8a1a8a, stop:1 #2a2ab0);
    color: #fffee0;
    border: 1px solid #b967ff;
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #ff71ce, stop:1 #01cdfe);
    border-color: #ff71ce;
    color: #070112;
    font-weight: bold;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #601060, stop:1 #1a1a80);
}
QPushButton:disabled { background: #1e0545; color: #5a3a7a; border-color: #3a1a5a; }

QLineEdit {
    background: #070112;
    color: #e8d5f5;
    border: 1px solid #b967ff;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #ff71ce;
}
QLineEdit:focus { border-color: #ff71ce; }

QSplitter::handle          { background: #3a1055; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

QStatusBar {
    background: rgba(7, 1, 18, 0.88);
    color: #a060c0;
    border-top: 1px solid #3a1055;
}

QMenu {
    background: #180535;
    color: #e8d5f5;
    border: 1px solid #b967ff;
    padding: 4px;
}
QMenu::item          { padding: 6px 20px; border-radius: 3px; }
QMenu::item:selected { background: #3a1a6a; color: #ff71ce; }
QMenu::separator     { height: 1px; background: #3a1055; margin: 3px 8px; }

QScrollArea  { border: none; background: transparent; }
QMessageBox  { background: #180535; }
""" + _scrollbar("#070112", "#b967ff", "#ff71ce") + _combobox(
    bg="#070112", color="#e8d5f5", border="#b967ff", radius="4px",
    focus="#ff71ce", arrow="#ff71ce", dropdown_bg="#180535", selection="#3a1a6a",
)

# ---------------------------------------------------------------------------
# Wallpaper helpers
# ---------------------------------------------------------------------------
_RESOURCES_DIR = __import__("os").path.join(
    __import__("os").path.dirname(__file__), "resources"
)


def _find_wallpaper(*names: str) -> str | None:
    """Return the first matching file in the resources directory, or None."""
    import os
    for name in names:
        path = os.path.join(_RESOURCES_DIR, name)
        if os.path.exists(path):
            return path
    return None


def get_wallpaper_path(theme_key: str) -> str | None:
    """Return absolute path to the wallpaper image for *theme_key*, or None."""
    theme = THEMES.get(theme_key, {})
    wp = theme.get("wallpaper")
    if callable(wp):
        return wp()
    return None


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------
THEMES: dict[str, dict] = {
    "default": {
        "name": "Default Dark",
        "swatches": ["#0f172a", "#1e293b", "#3b82f6"],
        "qss": _DEFAULT_QSS,
    },
    "bliss_xp": {
        "name": "Bliss XP",
        "swatches": ["#122540", "#1a3355", "#4aaa4a"],
        "qss": _BLISS_XP_QSS,
        "wallpaper": lambda: _find_wallpaper("bliss.png", "bliss.jpg", "bliss.bmp"),
    },
    "classic_steam": {
        "name": "Classic Steam",
        "swatches": ["#0f0f0f", "#2d2d2d", "#8db651"],
        "qss": _CLASSIC_STEAM_QSS,
    },
    "terminal": {
        "name": "Terminal",
        "swatches": ["#000000", "#001a00", "#00ff41"],
        "qss": _TERMINAL_QSS,
    },
    "vaporwave": {
        "name": "Vaporwave",
        "swatches": ["#070112", "#180535", "#ff71ce"],
        "qss": _VAPORWAVE_QSS,
        "wallpaper": lambda: _find_wallpaper("vaporwave.gif"),
    },
}

DEFAULT_THEME = "default"


def get_qss(key: str) -> str:
    return THEMES.get(key, THEMES[DEFAULT_THEME])["qss"]

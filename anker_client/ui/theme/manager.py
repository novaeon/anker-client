"""Applies a :class:`Palette` to the whole application.

``ThemeManager.apply(key)``:
* ``palette.set_current(key)``; ``icons.set_default_color(palette.text)``;
* builds the application stylesheet from the palette (``build_qss``) and sets
  it on the ``QApplication``; also sets a matching ``QPalette`` (so native
  dialogs/menus follow the theme) and the application font;
* emits ``theme_changed(key)`` so the main window can swap its wallpaper
  (``palette.wallpaper``) and custom-painted widgets can repaint.

QSS conventions every page relies on (set with ``widget.setProperty(...)``,
then ``widgets.common.repolish(widget)`` when changed at runtime):

* ``QPushButton[variant="primary"|"secondary"|"ghost"|"danger"|"success"|"warning"]`` —
  no property looks like "secondary". ``[size="lg"]`` for the big
  game-page call-to-action, ``[size="sm"]`` for compact row buttons.
  ``QToolButton[variant="icon"]`` — borderless icon-only buttons.
  ``QPushButton[variant="nav"]`` — sidebar navigation entries (checkable).
  ``QPushButton[variant="chip"]`` — checkable filter chips (genres).
* ``QLabel[role="display"]`` (page hero titles, ~26px semibold),
  ``[role="title"]`` (~18px semibold), ``[role="heading"]`` (section headings,
  ~14px semibold), ``[role="muted"]``, ``[role="faint"]``,
  ``[role="caption"]`` (11px muted), ``[role="error"]``, ``[role="success"]``,
  ``[role="warning"]``, ``[role="badge"]`` (pill), ``[role="badge-accent"]``,
  ``[role="badge-success"]``, ``[role="badge-warning"]``, ``[role="badge-danger"]``,
  ``[role="link"]``.
* ``QFrame[role="card"]`` (surface bg, border, radius), ``[role="panel"]``
  (surface, no border), ``[role="sidebar"]``, ``[role="toolbar"]``,
  ``[role="divider"]``, ``[role="hero"]``, ``[role="statusbar"]``,
  ``QWidget[role="page"]`` (transparent page background).
* ``QLineEdit[role="search"]`` — pill search box.
* ``QProgressBar`` default slim (6px, no text); ``[size="lg"]`` 10px.
* ``QListView[role="grid"]`` — transparent background (items painted by delegates).

Shell additions (used by the main window chrome and dialogs, safe to reuse):

* ``QPushButton[variant="notice"]`` — accent-tinted pill (header "3 updates");
  ``[tone="success"|"warning"]`` recolours it.
* ``QPushButton[variant="account"]`` — flat sidebar account chip;
  ``QPushButton[variant="status"]`` — flat status-strip entry (muted text).
* ``QLabel[role="brand"]`` — sidebar product name.
* ``QFrame[role="option"]`` — selectable card (``[selected="true"]`` highlights it).
* ``QFrame[role="notice"]`` — tinted message box; ``[tone="info"|"success"|"warning"|"error"]``.
* ``QPlainTextEdit[role="mono"]`` — read-only technical details (tracebacks).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication

from anker_client.ui import icons
from anker_client.ui.theme import palette as palettes
from anker_client.ui.theme.palette import Palette

log = logging.getLogger(__name__)


def _rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    r = round(ca.red() + (cb.red() - ca.red()) * t)
    g = round(ca.green() + (cb.green() - ca.green()) * t)
    bl = round(ca.blue() + (cb.blue() - ca.blue()) * t)
    return QColor(r, g, bl).name()


def _indicator_dir() -> Path:
    return Path(tempfile.gettempdir()) / "AnkerClient-theme"


def indicator_images(p: Palette) -> dict[str, str]:
    """Tiny SVG files for QSS ``image:`` rules (check mark, chevrons); name → ``url()`` path.

    Qt style sheets cannot draw glyphs and do not accept ``data:`` URLs, so the
    glyphs are written once per colour to a temp folder. Returns ``{}`` when the
    folder is not writable (the QSS then falls back to plain shapes).
    """
    wanted = {
        "check": ("check", p.accent_text),
        "chevron_down": ("chevron_down", p.text_muted),
        "chevron_down_disabled": ("chevron_down", p.text_faint),
        "chevron_up": ("chevron_up", p.text_muted),
    }
    folder = _indicator_dir()
    urls: dict[str, str] = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for key, (icon_name, color) in wanted.items():
            path = folder / f"{icon_name}-{QColor(color).name()[1:]}.svg"
            if not path.exists():
                _write_once(path, icons.svg_markup(icon_name, QColor(color).name()))
            urls[key] = path.as_posix()
    except OSError:
        log.warning("Could not write theme indicator images to %s", folder, exc_info=True)
        return {}
    return urls


def _write_once(path: Path, text: str) -> None:
    """Atomically create ``path``; another AnkerClient process may be writing the same file right now."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows refuses to replace a file another process has open (e.g. Qt reading it): it is
            # the same content, so an existing file is fine.
            if not path.exists():
                raise
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass  # moved into place
        except OSError:
            log.debug("Could not remove %s", tmp, exc_info=True)


def _indicator_qss(p: Palette, images: dict[str, str]) -> str:
    """Combo/spin arrows and the checkbox tick (images), radio dot (radial gradient)."""
    radio_dot = (f"qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {p.accent_text}, "
                 f"stop:0.32 {p.accent_text}, stop:0.42 {p.accent}, stop:1 {p.accent})")
    rules = [
        "QRadioButton::indicator { border-radius: 9px; }",
        f"QRadioButton::indicator:checked {{ background: {radio_dot}; border: 1px solid {p.accent}; }}",
    ]
    if images:
        rules += [
            f'QCheckBox::indicator:checked {{ image: url("{images["check"]}"); }}',
            "QComboBox::down-arrow { border: none; width: 12px; height: 12px; margin-right: 8px; "
            f'image: url("{images["chevron_down"]}"); }}',
            f'QComboBox::down-arrow:disabled {{ image: url("{images["chevron_down_disabled"]}"); }}',
            "QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { border: none; width: 10px; height: 10px; "
            f'image: url("{images["chevron_up"]}"); }}',
            "QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { border: none; width: 10px; height: 10px; "
            f'image: url("{images["chevron_down"]}"); }}',
        ]
    return "\n".join(rules) + "\n"


def build_qss(p: Palette, images: dict[str, str] | None = None) -> str:
    return _build_base_qss(p) + _indicator_qss(p, indicator_images(p) if images is None else images)


def _build_base_qss(p: Palette) -> str:
    r = p.radius
    rs = max(0, r // 2)
    surface = _rgba(p.surface, p.surface_alpha)
    surface_alt = _rgba(p.surface_alt, max(p.surface_alpha, 200))
    hover = _mix(p.surface_alt, p.text, 0.06)
    pressed = _mix(p.surface_alt, p.text, 0.12)
    accent_pressed = _mix(p.accent, "#000000", 0.15)
    danger_hover = _mix(p.danger, "#ffffff", 0.12)
    success_hover = _mix(p.success, "#ffffff", 0.12)
    warning_hover = _mix(p.warning, "#ffffff", 0.12)
    selection = _rgba(p.accent, 90)
    return f"""
* {{
    font-family: {p.font_family};
    font-size: 10pt;
    color: {p.text};
    outline: none;
}}
QMainWindow, QDialog, QWizard {{ background: {p.bg}; }}
QWidget[role="page"], QWidget[role="transparent"] {{ background: transparent; }}
QToolTip {{
    background: {p.elevated}; color: {p.text}; border: 1px solid {p.border};
    border-radius: {rs}px; padding: 5px 8px;
}}

/* ---------- frames ---------- */
QFrame[role="card"] {{ background: {surface}; border: 1px solid {p.border}; border-radius: {r}px; }}
QFrame[role="panel"] {{ background: {surface}; border: none; border-radius: {r}px; }}
QFrame[role="sidebar"] {{ background: {surface}; border: none; border-right: 1px solid {p.border}; }}
QFrame[role="toolbar"] {{ background: transparent; border: none; }}
QFrame[role="statusbar"] {{ background: {surface}; border: none; border-top: 1px solid {p.border}; }}
QFrame[role="hero"] {{ background: {p.surface_alt}; border: none; border-radius: {r}px; }}
QFrame[role="divider"] {{ background: {p.border}; border: none; }}

/* ---------- labels ---------- */
QLabel {{ background: transparent; }}
QLabel[role="display"] {{ font-size: 20pt; font-weight: 600; }}
QLabel[role="title"] {{ font-size: 14pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="muted"] {{ color: {p.text_muted}; }}
QLabel[role="faint"] {{ color: {p.text_faint}; }}
QLabel[role="caption"] {{ color: {p.text_muted}; font-size: 8.5pt; }}
QLabel[role="error"] {{ color: {p.danger}; }}
QLabel[role="success"] {{ color: {p.success}; }}
QLabel[role="warning"] {{ color: {p.warning}; }}
QLabel[role="link"] {{ color: {p.accent}; }}
QLabel[role="link"]:hover {{ text-decoration: underline; }}
QLabel[role="badge"], QLabel[role="badge-accent"], QLabel[role="badge-success"],
QLabel[role="badge-warning"], QLabel[role="badge-danger"] {{
    border-radius: 9px; padding: 2px 9px; font-size: 8.5pt; font-weight: 600;
}}
QLabel[role="badge"] {{ background: {p.surface_alt}; color: {p.text_muted}; border: 1px solid {p.border}; }}
QLabel[role="badge-accent"] {{ background: {p.accent}; color: {p.accent_text}; }}
QLabel[role="badge-success"] {{ background: {p.success}; color: {p.accent_text}; }}
QLabel[role="badge-warning"] {{ background: {p.warning}; color: #1a1300; }}
QLabel[role="badge-danger"] {{ background: {p.danger}; color: #ffffff; }}

/* ---------- buttons ---------- */
QPushButton {{
    background: {surface_alt}; color: {p.text}; border: 1px solid {p.border};
    border-radius: {r}px; padding: 6px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ color: {p.text_faint}; background: {_rgba(p.surface_alt, 120)}; border-color: {_rgba(p.border, 120)}; }}
QPushButton:focus {{ border-color: {p.accent}; }}
QPushButton[size="sm"] {{ padding: 3px 10px; min-height: 16px; font-size: 9pt; }}
QPushButton[size="lg"] {{ padding: 10px 22px; min-height: 26px; font-size: 11.5pt; font-weight: 600; }}
QPushButton[variant="primary"] {{ background: {p.accent}; color: {p.accent_text}; border: 1px solid {p.accent}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {_rgba(p.accent, 90)}; color: {_rgba(p.accent_text, 150)}; border-color: transparent; }}
QPushButton[variant="success"] {{ background: {p.success}; color: {p.accent_text}; border: 1px solid {p.success}; font-weight: 600; }}
QPushButton[variant="success"]:hover {{ background: {success_hover}; }}
QPushButton[variant="warning"] {{ background: {p.warning}; color: #1a1300; border: 1px solid {p.warning}; font-weight: 600; }}
QPushButton[variant="warning"]:hover {{ background: {warning_hover}; }}
QPushButton[variant="danger"] {{ background: transparent; color: {p.danger}; border: 1px solid {_rgba(p.danger, 140)}; }}
QPushButton[variant="danger"]:hover {{ background: {p.danger}; color: #ffffff; border-color: {danger_hover}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {surface_alt}; }}
QPushButton[variant="link"] {{ background: transparent; border: none; color: {p.accent}; padding: 0; }}
QPushButton[variant="link"]:hover {{ text-decoration: underline; }}
QPushButton[variant="nav"] {{
    background: transparent; border: none; border-radius: {r}px; text-align: left;
    padding: 9px 12px; color: {p.text_muted}; font-weight: 500;
}}
QPushButton[variant="nav"]:hover {{ background: {surface_alt}; color: {p.text}; }}
QPushButton[variant="nav"]:checked {{ background: {_rgba(p.accent, 40)}; color: {p.text}; font-weight: 600; }}
QPushButton[variant="chip"] {{
    background: {surface_alt}; border: 1px solid {p.border}; border-radius: 13px;
    padding: 4px 12px; color: {p.text_muted}; min-height: 16px;
}}
QPushButton[variant="chip"]:hover {{ color: {p.text}; border-color: {p.text_faint}; }}
QPushButton[variant="chip"]:checked {{ background: {p.accent}; border-color: {p.accent}; color: {p.accent_text}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 8px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {rs}px; padding: 4px; }}
QToolButton:hover {{ background: {surface_alt}; border-color: {p.border}; }}
QToolButton:pressed {{ background: {pressed}; }}
QToolButton:checked {{ background: {_rgba(p.accent, 50)}; border-color: {p.accent}; }}
QToolButton[variant="icon"] {{ border: none; padding: 5px; border-radius: {rs}px; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {surface_alt}; color: {p.text}; border: 1px solid {p.border};
    border-radius: {rs + 2}px; padding: 6px 9px; selection-background-color: {selection};
    selection-color: {p.text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.text_faint}; }}
QLineEdit[role="search"] {{ border-radius: 17px; padding: 7px 14px 7px 34px; min-height: 20px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{
    width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {p.text_muted}; margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {p.elevated}; border: 1px solid {p.border}; selection-background-color: {selection};
    padding: 4px; outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid {p.text_muted};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    width: 0; height: 0; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {p.text_muted};
}}
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {p.text_faint}; background: {surface_alt};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: none; }}
QRadioButton::indicator:checked {{ background: {p.accent}; border: 4px solid {surface_alt}; outline: 1px solid {p.accent}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -6px 0; border-radius: 7px; background: {p.accent}; }}

/* ---------- progress ---------- */
QProgressBar {{
    background: {_rgba(p.text, 25)}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
QProgressBar[size="lg"] {{ max-height: 10px; min-height: 10px; border-radius: 5px; }}
QProgressBar[size="lg"]::chunk {{ border-radius: 5px; }}
QProgressBar[state="paused"]::chunk {{ background: {p.text_faint}; }}
QProgressBar[state="error"]::chunk {{ background: {p.danger}; }}
QProgressBar[state="success"]::chunk {{ background: {p.success}; }}
QProgressBar[state="install"]::chunk {{ background: {p.warning}; }}

/* ---------- lists / tables ---------- */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{
    background: {surface}; border: 1px solid {p.border}; border-radius: {r}px;
    alternate-background-color: {_rgba(p.surface_alt, 120)}; selection-background-color: {selection};
    selection-color: {p.text};
}}
QListView[role="grid"] {{ background: transparent; border: none; }}
QListView[role="plain"], QListWidget[role="plain"] {{ background: transparent; border: none; }}
QListView::item, QListWidget::item {{ border-radius: {rs}px; padding: 4px; }}
QListWidget::item:hover, QTreeView::item:hover, QTableView::item:hover {{ background: {_rgba(p.text, 14)}; }}
QListWidget::item:selected, QTreeView::item:selected, QTableView::item:selected {{ background: {selection}; }}
QHeaderView::section {{
    background: {surface}; color: {p.text_muted}; border: none; border-bottom: 1px solid {p.border};
    padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {surface}; border: none; }}

/* ---------- scroll ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {_rgba(p.text, 50)}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {_rgba(p.text, 90)}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {_rgba(p.text, 50)}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {_rgba(p.text, 90)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- menus / tabs / misc ---------- */
QMenu {{ background: {p.elevated}; border: 1px solid {p.border}; border-radius: {rs + 2}px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: {rs}px; }}
QMenu::item:selected {{ background: {selection}; }}
QMenu::item:disabled {{ color: {p.text_faint}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}
QMenu::icon {{ padding-left: 6px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; color: {p.text_muted}; padding: 8px 14px; border: none;
    border-bottom: 2px solid transparent; margin-right: 4px;
}}
QTabBar::tab:hover {{ color: {p.text}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; font-weight: 600; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QStatusBar {{ background: {surface}; border-top: 1px solid {p.border}; color: {p.text_muted}; }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: {r}px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {p.text_muted}; }}
QMessageBox {{ background: {p.elevated}; }}
QWizard QWidget {{ background: transparent; }}

/* ---------- shell ---------- */
QLabel[role="brand"] {{ font-size: 12.5pt; font-weight: 700; }}
QPushButton[variant="notice"] {{
    background: {_rgba(p.accent, 34)}; color: {p.accent}; border: 1px solid {_rgba(p.accent, 110)};
    border-radius: 13px; padding: 4px 12px; min-height: 16px; font-weight: 600; font-size: 9pt;
}}
QPushButton[variant="notice"]:hover {{ background: {_rgba(p.accent, 64)}; }}
QPushButton[variant="notice"][tone="success"] {{
    background: {_rgba(p.success, 34)}; color: {p.success}; border-color: {_rgba(p.success, 110)};
}}
QPushButton[variant="notice"][tone="success"]:hover {{ background: {_rgba(p.success, 64)}; }}
QPushButton[variant="notice"][tone="warning"] {{
    background: {_rgba(p.warning, 34)}; color: {p.warning}; border-color: {_rgba(p.warning, 110)};
}}
QPushButton[variant="notice"][tone="warning"]:hover {{ background: {_rgba(p.warning, 64)}; }}
QPushButton[variant="account"] {{
    background: transparent; border: 1px solid transparent; border-radius: {r}px; text-align: left; padding: 6px 8px;
}}
QPushButton[variant="account"]:hover {{ background: {surface_alt}; border-color: {p.border}; }}
QPushButton[variant="status"] {{
    background: transparent; border: none; border-radius: {rs}px; padding: 2px 6px; min-height: 14px;
    color: {p.text_muted}; font-size: 9pt; text-align: left;
}}
QPushButton[variant="status"]:hover {{ color: {p.text}; background: {_rgba(p.text, 16)}; }}
QFrame[role="option"] {{ background: {surface_alt}; border: 1px solid {p.border}; border-radius: {r}px; }}
QFrame[role="option"]:hover {{ border-color: {p.text_faint}; }}
QFrame[role="option"][selected="true"] {{ background: {_rgba(p.accent, 30)}; border-color: {p.accent}; }}
QFrame[role="notice"] {{ background: {_rgba(p.info, 26)}; border: 1px solid {_rgba(p.info, 90)}; border-radius: {rs + 2}px; }}
QFrame[role="notice"][tone="success"] {{ background: {_rgba(p.success, 26)}; border-color: {_rgba(p.success, 90)}; }}
QFrame[role="notice"][tone="warning"] {{ background: {_rgba(p.warning, 26)}; border-color: {_rgba(p.warning, 100)}; }}
QFrame[role="notice"][tone="error"] {{ background: {_rgba(p.danger, 26)}; border-color: {_rgba(p.danger, 100)}; }}
QPlainTextEdit[role="mono"] {{ font-family: {p.mono_family}; font-size: 8.5pt; color: {p.text_muted}; }}
"""


def build_qpalette(p: Palette) -> QPalette:
    pal = QPalette()
    roles = QPalette.ColorRole
    pal.setColor(roles.Window, QColor(p.bg))
    pal.setColor(roles.WindowText, QColor(p.text))
    pal.setColor(roles.Base, QColor(p.surface_alt))
    pal.setColor(roles.AlternateBase, QColor(p.surface))
    pal.setColor(roles.ToolTipBase, QColor(p.elevated))
    pal.setColor(roles.ToolTipText, QColor(p.text))
    pal.setColor(roles.PlaceholderText, QColor(p.text_faint))
    pal.setColor(roles.Text, QColor(p.text))
    pal.setColor(roles.Button, QColor(p.surface_alt))
    pal.setColor(roles.ButtonText, QColor(p.text))
    pal.setColor(roles.BrightText, QColor(p.accent_text))
    pal.setColor(roles.Highlight, QColor(p.accent))
    pal.setColor(roles.HighlightedText, QColor(p.accent_text))
    pal.setColor(roles.Link, QColor(p.accent))
    pal.setColor(roles.LinkVisited, QColor(p.accent_hover))
    pal.setColor(QPalette.ColorGroup.Disabled, roles.Text, QColor(p.text_faint))
    pal.setColor(QPalette.ColorGroup.Disabled, roles.ButtonText, QColor(p.text_faint))
    pal.setColor(QPalette.ColorGroup.Disabled, roles.WindowText, QColor(p.text_faint))
    return pal


class ThemeManager(QObject):
    theme_changed = pyqtSignal(str)

    def __init__(self, app: QApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._key = palettes.DEFAULT_THEME

    @property
    def current_key(self) -> str:
        return self._key

    @property
    def current(self) -> Palette:
        return palettes.current()

    def apply(self, key: str) -> Palette:
        p = palettes.set_current(key)
        self._key = p.key
        icons.set_default_color(p.text)
        self._app.setPalette(build_qpalette(p))
        family = p.font_family.split(",")[0].strip().strip('"')
        font = QFont(family)
        font.setPointSizeF(10)
        self._app.setFont(font)
        self._app.setStyleSheet(build_qss(p))
        self.theme_changed.emit(p.key)
        return p

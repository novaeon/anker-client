# anker_client/ui/settings_tab.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLineEdit, QPushButton,
    QHBoxLayout, QFileDialog, QLabel, QFrame, QApplication, QRadioButton
)
from PyQt6.QtCore import Qt, pyqtSignal
import anker_client.settings as settings
from anker_client.themes import THEMES, get_qss


# ---------------------------------------------------------------------------
# Theme card widget
# ---------------------------------------------------------------------------

class ThemeCard(QFrame):
    selected = pyqtSignal(str)  # theme key

    def __init__(self, key: str, info: dict, is_current: bool, parent=None):
        super().__init__(parent)
        self.key = key
        self.setFixedHeight(54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        # Colour swatches
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(3)
        swatch_row.setContentsMargins(0, 0, 0, 0)
        for colour in info["swatches"]:
            chip = QFrame()
            chip.setFixedSize(14, 30)
            chip.setStyleSheet(f"background: {colour}; border-radius: 3px;")
            swatch_row.addWidget(chip)
        swatch_widget = QWidget()
        swatch_widget.setFixedWidth(54)
        swatch_widget.setLayout(swatch_row)
        layout.addWidget(swatch_widget)

        name_lbl = QLabel(info["name"])
        name_lbl.setStyleSheet("font-size: 12px; font-weight: bold; background: transparent;")
        layout.addWidget(name_lbl)
        layout.addStretch()

        self._dot = QLabel()
        self._dot.setStyleSheet("font-size: 16px; background: transparent;")
        layout.addWidget(self._dot)

        self._set_selected(is_current)

    def _set_selected(self, on: bool) -> None:
        self._dot.setText("●" if on else "○")
        if on:
            self.setStyleSheet(
                "ThemeCard { border: 1px solid #3b82f6; border-radius: 5px; }"
            )
        else:
            self.setStyleSheet(
                "ThemeCard { border: 1px solid #334155; border-radius: 5px; }"
            )

    def mark_selected(self, on: bool) -> None:
        self._set_selected(on)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.key)


# ---------------------------------------------------------------------------
# Settings tab
# ---------------------------------------------------------------------------

class SettingsTab(QWidget):
    theme_changed = pyqtSignal(str)  # emitted on live preview AND on Apply

    def __init__(self, on_saved=None, parent=None):
        super().__init__(parent)
        self._on_saved = on_saved
        self._theme_cards: dict[str, ThemeCard] = {}
        self._pending_theme: str = settings.get_theme()
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        # ---- Paths ----
        paths_lbl = QLabel("Paths")
        paths_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        outer.addWidget(paths_lbl)

        form = QFormLayout()
        form.setSpacing(10)

        games_row = QHBoxLayout()
        self.games_dir_edit = QLineEdit(settings.get_games_dir())
        browse_games = QPushButton("Browse…")
        browse_games.clicked.connect(self._browse_games_dir)
        games_row.addWidget(self.games_dir_edit)
        games_row.addWidget(browse_games)
        form.addRow("Games folder:", games_row)

        zip_row = QHBoxLayout()
        self.seven_zip_edit = QLineEdit(settings.get_seven_zip())
        browse_zip = QPushButton("Browse…")
        browse_zip.clicked.connect(self._browse_seven_zip)
        zip_row.addWidget(self.seven_zip_edit)
        zip_row.addWidget(browse_zip)
        form.addRow("7-Zip path:", zip_row)

        outer.addLayout(form)

        # ---- Divider ----
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(line)

        # ---- Behaviour ----
        behavior_lbl = QLabel("Behaviour")
        behavior_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        outer.addWidget(behavior_lbl)

        close_lbl = QLabel("When closing:")
        outer.addWidget(close_lbl)

        close_row = QHBoxLayout()
        self._close_tray_rb = QRadioButton("Minimize to tray")
        self._close_quit_rb = QRadioButton("Quit")
        close_row.addWidget(self._close_tray_rb)
        close_row.addWidget(self._close_quit_rb)
        close_row.addStretch()
        outer.addLayout(close_row)

        saved_behavior = settings.get_close_behavior()
        if saved_behavior == "tray":
            self._close_tray_rb.setChecked(True)
        elif saved_behavior == "quit":
            self._close_quit_rb.setChecked(True)

        # ---- Second divider ----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(line2)

        # ---- Themes ----
        theme_lbl = QLabel("Theme")
        theme_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        outer.addWidget(theme_lbl)

        current = settings.get_theme()
        grid_rows = [list(THEMES.items())[i:i+2] for i in range(0, len(THEMES), 2)]
        for row_items in grid_rows:
            row = QHBoxLayout()
            row.setSpacing(8)
            for key, info in row_items:
                card = ThemeCard(key, info, key == current)
                card.selected.connect(self._on_theme_selected)
                self._theme_cards[key] = card
                row.addWidget(card)
            if len(row_items) == 1:
                row.addStretch()
            outer.addLayout(row)

        # ---- Status + Apply ----
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #22c55e; font-size: 11px;")
        outer.addWidget(self._status_label)

        apply_btn = QPushButton("Apply")
        apply_btn.setFixedWidth(80)
        apply_btn.clicked.connect(self._save)
        outer.addWidget(apply_btn)
        outer.addStretch()

    # ------------------------------------------------------------------

    def _on_theme_selected(self, key: str) -> None:
        for k, card in self._theme_cards.items():
            card.mark_selected(k == key)
        self._pending_theme = key
        # Live preview
        QApplication.instance().setStyleSheet(get_qss(key))
        self.theme_changed.emit(key)
        self._status_label.setText("Click Apply to save.")

    def _browse_games_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select Games Folder", self.games_dir_edit.text()
        )
        if d:
            self.games_dir_edit.setText(d)

    def _browse_seven_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select 7-Zip Executable",
            self.seven_zip_edit.text(), "Executables (*.exe)"
        )
        if path:
            self.seven_zip_edit.setText(path)

    def _save(self) -> None:
        settings.save(self.games_dir_edit.text(), self.seven_zip_edit.text())
        settings.save_theme(self._pending_theme)
        if self._close_tray_rb.isChecked():
            settings.save_close_behavior("tray")
        elif self._close_quit_rb.isChecked():
            settings.save_close_behavior("quit")
        self._status_label.setText("Saved.")
        self.theme_changed.emit(self._pending_theme)
        if self._on_saved:
            self._on_saved()

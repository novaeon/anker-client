# anker_client/ui/library_tab.py
import os
import subprocess
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QGridLayout,
    QPushButton, QLabel, QMessageBox, QSplitter, QMenu,
    QLineEdit, QComboBox
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PyQt6.QtCore import QUrl
from anker_client.core.scraper import livewire_search
from anker_client.core.installer import find_game_exe, create_shortcut
from anker_client.core.paths import sanitize_windows_name
from anker_client.ui.game_detail import ScreenshotGallery, GamePageWorker
import anker_client.settings as settings
from anker_client.settings import get_library_cache, update_library_cache

_CARD_W = 160
_CARD_GAP = 12


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class LibraryInfoWorker(QThread):
    """Searches ankergames for a game by name and emits the first result."""
    found = pyqtSignal(str, dict)  # path, game_dict

    def __init__(self, session, name: str, path: str):
        super().__init__()
        self._session = session
        self._name = name
        self._path = path

    def run(self) -> None:
        try:
            results = livewire_search(self._session, self._name)
            if results:
                self.found.emit(self._path, results[0])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Library card (grid tile)
# ---------------------------------------------------------------------------

class LibraryCard(QWidget):
    clicked = pyqtSignal(str, str)         # name, path
    action_requested = pyqtSignal(str, str, str)  # action, name, path

    def __init__(self, name: str, path: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._path = path
        self._cover_loading = False
        self.setFixedSize(160, 285)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(152, 200)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(
            "background: #1e293b; border-radius: 4px; "
            "color: #475569; font-size: 32px; font-weight: bold;"
        )
        initials = "".join(w[0].upper() for w in self._name.split()[:2])
        self._cover_label.setText(initials or "?")
        layout.addWidget(self._cover_label)

        self._title_lbl = QLabel(self._name)
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setStyleSheet("font-size: 11px; font-weight: bold;")
        layout.addWidget(self._title_lbl)

        self._genre_lbl = QLabel()
        self._genre_lbl.setWordWrap(True)
        self._genre_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._genre_lbl.setStyleSheet("font-size: 10px; color: #64748b;")
        self._genre_lbl.hide()
        layout.addWidget(self._genre_lbl)

        self._meta_lbl = QLabel()
        self._meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._meta_lbl.setStyleSheet("font-size: 10px; color: #475569;")
        self._meta_lbl.hide()
        layout.addWidget(self._meta_lbl)

    def update_info(self, game: dict) -> None:
        genres = game.get("genres", [])
        if genres:
            self._genre_lbl.setText(", ".join(genres[:2]))
            self._genre_lbl.show()

        meta_parts = []
        size_gb = game.get("size_gb", "")
        if size_gb:
            try:
                meta_parts.append(f"{float(size_gb):.4g} GB")
            except ValueError:
                meta_parts.append(f"{size_gb} GB")
        release_date = game.get("release_date", "")
        if release_date and len(release_date) >= 4:
            meta_parts.append(release_date[:4])
        if meta_parts:
            self._meta_lbl.setText("  ·  ".join(meta_parts))
            self._meta_lbl.show()

        cover_url = game.get("cover_url", "")
        if cover_url:
            self._load_cover(cover_url)

    def _load_cover(self, url: str) -> None:
        if self._cover_loading:
            return
        # Check disk cache first
        path = settings.get_cover_path(self._name)
        if os.path.exists(path):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._cover_label.setText("")
                self._cover_label.setPixmap(
                    pixmap.scaled(152, 200,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
                )
                return
            # Corrupt file — remove so next launch re-downloads cleanly
            try:
                os.remove(path)
            except OSError:
                pass
        # Not cached — download from network
        self._cover_loading = True
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_cover_loaded)
        self._nam.get(QNetworkRequest(QUrl(url)))

    def _on_cover_loaded(self, reply) -> None:
        self._cover_loading = False
        data = reply.readAll()
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if not pixmap.isNull():
            self._cover_label.setText("")
            self._cover_label.setPixmap(
                pixmap.scaled(152, 200,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            )
            # Save to disk cache silently
            try:
                path = settings.get_cover_path(self._name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                pixmap.save(path, "PNG")
            except Exception:
                pass
        reply.deleteLater()

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.setStyleSheet(
                "LibraryCard { background: #1e2d3d; border: 1px solid #2d4a6e; border-radius: 6px; }"
            )
        else:
            self.setStyleSheet("")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._name, self._path)

    def contextMenuEvent(self, event) -> None:
        self.clicked.emit(self._name, self._path)  # select card first
        menu = QMenu(self)
        menu.addAction("Launch").triggered.connect(
            lambda: self.action_requested.emit("launch", self._name, self._path)
        )
        menu.addAction("Re-create Shortcut").triggered.connect(
            lambda: self.action_requested.emit("shortcut", self._name, self._path)
        )
        menu.addSeparator()
        uninstall = menu.addAction("Uninstall")
        uninstall.triggered.connect(
            lambda: self.action_requested.emit("uninstall", self._name, self._path)
        )
        menu.exec(event.globalPos())


# ---------------------------------------------------------------------------
# Detail panel (right side)
# ---------------------------------------------------------------------------

class LibraryDetailPanel(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._name: str | None = None
        self._path: str | None = None
        self._page_worker: GamePageWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color: #94a3b8;")
        layout.addWidget(self.meta_label)

        self.screenshot_strip = ScreenshotGallery(self.session)
        layout.addWidget(self.screenshot_strip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.desc_label = QLabel("")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.desc_label.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(self.desc_label)
        layout.addWidget(scroll)

        actions = QHBoxLayout()
        self.launch_btn = QPushButton("Launch")
        self.launch_btn.setMinimumHeight(36)
        self.launch_btn.setEnabled(False)
        self.launch_btn.clicked.connect(self._launch)

        self.shortcut_btn = QPushButton("Re-create Shortcut")
        self.shortcut_btn.setMinimumHeight(36)
        self.shortcut_btn.setEnabled(False)
        self.shortcut_btn.clicked.connect(self._recreate_shortcut)

        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setMinimumHeight(36)
        self.uninstall_btn.setEnabled(False)
        self.uninstall_btn.clicked.connect(self._uninstall)
        self.uninstall_btn.setStyleSheet("color: #f87171;")

        actions.addWidget(self.launch_btn)
        actions.addWidget(self.shortcut_btn)
        actions.addStretch()
        actions.addWidget(self.uninstall_btn)
        layout.addLayout(actions)

    def load_game(self, name: str, path: str) -> None:
        self._name = name
        self._path = path

        cached = get_library_cache().get(name, {})

        # Populate immediately from cache
        self.title_label.setText(cached.get("title") or name)
        self._update_meta(cached)
        self.desc_label.setText(cached.get("description") or "")
        self.screenshot_strip.load_screenshots(cached.get("screenshots") or [])

        self.launch_btn.setEnabled(True)
        self.shortcut_btn.setEnabled(True)
        self.uninstall_btn.setEnabled(True)

        # If we have a slug but are missing description/screenshots, fetch them
        slug = cached.get("slug")
        if slug and not (cached.get("description") and cached.get("screenshots")):
            self.desc_label.setText("Loading details…")
            self._page_worker = GamePageWorker(self.session, slug)
            self._page_worker.loaded.connect(self._on_page_loaded)
            self._page_worker.error.connect(lambda _: self.desc_label.setText(""))
            self._page_worker.start()

    def _on_page_loaded(self, data: dict) -> None:
        if not self._name:
            return
        # Merge page data into the existing cache entry
        cached = get_library_cache().get(self._name, {})
        cached["description"] = data.get("description", "")
        cached["screenshots"] = data.get("screenshots", [])
        # Also capture file_size from the page if available
        if data.get("file_size"):
            cached["file_size"] = data["file_size"]
        update_library_cache(self._name, cached)

        self.desc_label.setText(cached.get("description") or "")
        self._update_meta(cached)
        self.screenshot_strip.load_screenshots(cached.get("screenshots") or [])

    def _update_meta(self, info: dict) -> None:
        genres_text = ", ".join(info.get("genres", []))
        # Prefer human-readable file_size from game page; fall back to size_gb
        file_size = info.get("file_size") or ""
        if not file_size:
            size_gb = info.get("size_gb", "")
            if size_gb:
                try:
                    file_size = f"{float(size_gb):.4g} GB"
                except ValueError:
                    file_size = f"{size_gb} GB"
        lines = [p for p in [genres_text, file_size] if p]
        self.meta_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _launch(self) -> None:
        if not self._name or not self._path:
            return
        exe = find_game_exe(self._path, self._name)
        if exe:
            try:
                subprocess.Popen([exe], cwd=self._path)
            except Exception as e:
                QMessageBox.warning(self, "Launch Failed", str(e))

    def _recreate_shortcut(self) -> None:
        if not self._name or not self._path:
            return
        exe = find_game_exe(self._path, self._name)
        if not exe:
            from PyQt6.QtWidgets import QFileDialog
            exe, _ = QFileDialog.getOpenFileName(
                self, "Select game exe", self._path, "Executables (*.exe)"
            )
        if not exe:
            return
        safe = sanitize_windows_name(self._name)
        desktop = os.path.join(os.path.expanduser("~"), "Desktop", f"{safe}.lnk")
        start_menu = os.path.join(
            os.environ["APPDATA"],
            "Microsoft", "Windows", "Start Menu", "Programs", f"{safe}.lnk"
        )
        try:
            create_shortcut(exe, desktop, self._path)
            create_shortcut(exe, start_menu, self._path)
        except Exception as e:
            QMessageBox.warning(self, "Shortcut Failed", str(e))

    def _uninstall(self) -> None:
        if not self._name or not self._path:
            return
        reply = QMessageBox.question(
            self, "Uninstall",
            f"Uninstall {self._name}?\n\nThis will permanently delete all game files.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        import shutil
        path = self._path
        name = self._name
        try:
            shutil.rmtree(path)
            safe = sanitize_windows_name(name)
            desktop = os.path.join(os.path.expanduser("~"), "Desktop", f"{safe}.lnk")
            start_menu = os.path.join(
                os.environ["APPDATA"],
                "Microsoft", "Windows", "Start Menu", "Programs", f"{safe}.lnk"
            )
            for shortcut in (desktop, start_menu):
                if os.path.exists(shortcut):
                    os.remove(shortcut)
        except Exception as e:
            QMessageBox.warning(self, "Uninstall Failed", str(e))
            return
        # Clear panel and trigger refresh via parent
        self._name = None
        self._path = None
        self.title_label.setText("")
        self.meta_label.setText("")
        self.desc_label.setText("")
        self.screenshot_strip.load_screenshots([])
        self.launch_btn.setEnabled(False)
        self.shortcut_btn.setEnabled(False)
        self.uninstall_btn.setEnabled(False)
        # Walk up to LibraryTab and call refresh
        parent = self.parent()
        while parent:
            if isinstance(parent, LibraryTab):
                parent.refresh()
                break
            parent = parent.parent()


# ---------------------------------------------------------------------------
# Library tab
# ---------------------------------------------------------------------------

class LibraryTab(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._all_cards: list[LibraryCard] = []
        self._cards: list[LibraryCard] = []
        self._workers: list[LibraryInfoWorker] = []
        self._selected_card: LibraryCard | None = None
        self._current_cols = 0
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: grid
        grid_widget = QWidget()
        grid_layout = QVBoxLayout(grid_widget)
        grid_layout.setContentsMargins(8, 8, 4, 8)
        grid_layout.setSpacing(6)

        header = QHBoxLayout()
        self._status_label = QLabel("Installed Games")
        header.addWidget(self._status_label)
        header.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        grid_layout.addLayout(header)

        # Filter / sort bar
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("🔍  Filter games…")
        self._filter_edit.setMaximumHeight(28)
        self._filter_edit.textChanged.connect(self._apply_filter_sort)
        bar.addWidget(self._filter_edit)

        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Name A–Z", "Name Z–A", "Size (largest first)", "Newest first"])
        self._sort_combo.setFixedWidth(160)
        self._sort_combo.setMaximumHeight(28)
        self._sort_combo.currentIndexChanged.connect(self._apply_filter_sort)
        bar.addWidget(self._sort_combo)
        grid_layout.addLayout(bar)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(_CARD_GAP)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.grid_scroll.setWidget(self.grid_container)
        grid_layout.addWidget(self.grid_scroll)

        splitter.addWidget(grid_widget)

        # Right: detail panel
        self.detail_panel = LibraryDetailPanel(self.session)
        self.detail_panel.setMinimumWidth(280)
        splitter.addWidget(self.detail_panel)

        splitter.setSizes([760, 360])
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Resize / reflow
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._cards:
            QTimer.singleShot(0, self._reflow_grid)

    def _reflow_grid(self) -> None:
        vp_w = self.grid_scroll.viewport().width()
        cols = max(1, (vp_w - 8 + _CARD_GAP) // (_CARD_W + _CARD_GAP))
        if cols == self._current_cols:
            return
        self._current_cols = cols
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for i, card in enumerate(self._cards):
            self.grid_layout.addWidget(card, i // cols, i % cols)

    # ------------------------------------------------------------------
    # Filter / sort
    # ------------------------------------------------------------------

    def _apply_filter_sort(self) -> None:
        query = self._filter_edit.text().lower()
        sort_idx = self._sort_combo.currentIndex()

        filtered = [c for c in self._all_cards if query in c._name.lower()]

        if sort_idx == 0:   # Name A–Z
            filtered.sort(key=lambda c: c._name.lower())
        elif sort_idx == 1:  # Name Z–A
            filtered.sort(key=lambda c: c._name.lower(), reverse=True)
        elif sort_idx == 2:  # Size largest first
            def _size_key(c: LibraryCard) -> float:
                from anker_client.settings import get_library_cache
                cached = get_library_cache().get(c._name, {})
                try:
                    return -float(cached.get("size_gb") or 0)
                except (ValueError, TypeError):
                    return 0.0
            filtered.sort(key=_size_key)
        else:               # Newest first
            def _year_key(c: LibraryCard) -> int:
                from anker_client.settings import get_library_cache
                cached = get_library_cache().get(c._name, {})
                date = cached.get("release_date") or ""
                try:
                    return -int(date[:4])
                except (ValueError, TypeError):
                    return 0
            filtered.sort(key=_year_key)

        self._cards = filtered
        self._current_cols = 0
        self._reflow_grid()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        for w in self._workers:
            w.quit()
        self._workers = []

        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        for card in self._all_cards:
            card.deleteLater()
        self._all_cards = []
        self._cards = []
        self._selected_card = None
        self._current_cols = 0

        games_dir = settings.get_games_dir()
        if not os.path.isdir(games_dir):
            self._status_label.setText("Installed Games")
            return

        for name in sorted(os.listdir(games_dir)):
            path = os.path.join(games_dir, name)
            if os.path.isdir(path) and name != "_temp":
                card = LibraryCard(name, path)
                card.clicked.connect(self._on_card_clicked)
                card.action_requested.connect(self._on_card_action)
                self._all_cards.append(card)

                cached = get_library_cache().get(name)
                if cached:
                    card.update_info(cached)
                elif self.session and self.session.is_logged_in:
                    worker = LibraryInfoWorker(self.session, name, path)
                    worker.found.connect(self._on_info_found)
                    worker.finished.connect(
                        lambda w=worker: self._workers.remove(w) if w in self._workers else None
                    )
                    self._workers.append(worker)
                    worker.start()

        count = len(self._all_cards)
        self._status_label.setText(f"{count} game{'s' if count != 1 else ''} installed")
        QTimer.singleShot(0, self._apply_filter_sort)

    def _on_info_found(self, path: str, game: dict) -> None:
        name = os.path.basename(path)
        update_library_cache(name, game)
        for card in self._all_cards:
            if card._path == path:
                card.update_info(game)
                break

    def _on_card_action(self, action: str, name: str, path: str) -> None:
        if action == "launch":
            self.detail_panel._launch()
        elif action == "shortcut":
            self.detail_panel._recreate_shortcut()
        elif action == "uninstall":
            self.detail_panel._uninstall()

    def _on_card_clicked(self, name: str, path: str) -> None:
        if self._selected_card:
            self._selected_card.set_selected(False)
        for card in self._cards:
            if card._path == path:
                card.set_selected(True)
                self._selected_card = card
                break
        self.detail_panel.load_game(name, path)

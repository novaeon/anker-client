# anker_client/ui/game_card.py
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QMenu
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt, pyqtSignal
from anker_client.ui.image_loader import get_image_loader


class GameCard(QWidget):
    clicked = pyqtSignal(dict)  # emits the game dict

    def __init__(self, game: dict, parent=None):
        super().__init__(parent)
        self.game = game
        self._cover_url = game.get("cover_url", "")
        self._image_loader = get_image_loader()
        self._image_loader.image_loaded.connect(self._on_image_loaded)
        self._image_loader.image_failed.connect(self._on_image_failed)
        self.setFixedSize(160, 285)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(152, 200)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background: #1e293b; border-radius: 4px;")
        layout.addWidget(self.cover_label)

        title_label = QLabel(self.game.get("title", ""))
        title_label.setWordWrap(True)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        layout.addWidget(title_label)

        genres = self.game.get("genres", [])
        if genres:
            genre_lbl = QLabel(", ".join(genres[:2]))
            genre_lbl.setWordWrap(True)
            genre_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            genre_lbl.setStyleSheet("font-size: 10px; color: #64748b;")
            layout.addWidget(genre_lbl)

        meta_parts = []
        size_gb = self.game.get("size_gb", "")
        if size_gb:
            try:
                meta_parts.append(f"{float(size_gb):.4g} GB")
            except ValueError:
                meta_parts.append(f"{size_gb} GB")
        release_date = self.game.get("release_date", "")
        if release_date and len(release_date) >= 4:
            meta_parts.append(release_date[:4])
        if meta_parts:
            meta_lbl = QLabel("  ·  ".join(meta_parts))
            meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            meta_lbl.setStyleSheet("font-size: 10px; color: #475569;")
            layout.addWidget(meta_lbl)

        if self._cover_url:
            self._load_cover(self._cover_url)

    def _load_cover(self, url: str) -> None:
        pixmap = self._image_loader.request(url)
        if pixmap is not None:
            self._set_cover(pixmap)

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        if url == self._cover_url:
            self._set_cover(pixmap)

    def _on_image_failed(self, url: str) -> None:
        if url == self._cover_url:
            self.cover_label.setText("No cover")

    def _set_cover(self, pixmap: QPixmap) -> None:
        self.cover_label.setPixmap(
            pixmap.scaled(
                152,
                200,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.game)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("View Details").triggered.connect(
            lambda: self.clicked.emit(self.game)
        )
        menu.exec(event.globalPos())

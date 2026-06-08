# anker_client/ui/settings_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QPushButton,
    QHBoxLayout, QFileDialog, QDialogButtonBox
)
import anker_client.settings as settings


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

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

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_games_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select Games Folder", self.games_dir_edit.text()
        )
        if d:
            self.games_dir_edit.setText(d)

    def _browse_seven_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select 7-Zip Executable",
            self.seven_zip_edit.text(),
            "Executables (*.exe)"
        )
        if path:
            self.seven_zip_edit.setText(path)

    def _save(self) -> None:
        settings.save(self.games_dir_edit.text(), self.seven_zip_edit.text())
        self.accept()

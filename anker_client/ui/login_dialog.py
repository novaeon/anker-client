# anker_client/ui/login_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QLabel, QCheckBox, QHBoxLayout
)


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log in to AnkerGames")
        self.setMinimumWidth(360)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("email@example.com")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Email:", self.email_input)
        form.addRow("Password:", self.password_input)
        layout.addLayout(form)

        self.remember_check = QCheckBox("Remember me")
        self.remember_check.setChecked(True)
        layout.addWidget(self.remember_check)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: red;")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.login_btn = QPushButton("Log In")
        self.login_btn.setDefault(True)
        cancel_btn = QPushButton("Cancel")
        buttons.addWidget(self.login_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self.login_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

    def get_credentials(self) -> tuple[str, str, bool]:
        return (
            self.email_input.text().strip(),
            self.password_input.text(),
            self.remember_check.isChecked(),
        )

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)

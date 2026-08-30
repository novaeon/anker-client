from __future__ import annotations

import threading

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from anker_client.core.tasks import BackgroundTask, get_task_runner


def _attempt_login(
    cancel_event: threading.Event,
    session,
    email: str,
    password: str,
    remember: bool,
) -> dict:
    if cancel_event.is_set():
        return {"success": False, "warning": ""}
    success = session.login(email, password)
    warning = ""
    if success and remember and not cancel_event.is_set():
        try:
            session.save_credentials(email, password)
        except Exception as exc:
            warning = str(exc).strip() or type(exc).__name__
    return {"success": success, "warning": warning}


class LoginDialog(QDialog):
    """Responsive login dialog; network and keyring calls never block Qt."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._task: BackgroundTask | None = None
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
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #f87171;")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.login_btn = QPushButton("Log In")
        self.login_btn.setDefault(True)
        self.cancel_btn = QPushButton("Cancel")
        buttons.addWidget(self.login_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

        self.login_btn.clicked.connect(self._submit)
        self.cancel_btn.clicked.connect(self.reject)

    def _submit(self) -> None:
        email = self.email_input.text().strip()
        password = self.password_input.text()
        if not email or not password:
            self.show_error("Enter both your email and password.")
            return

        self._set_busy(True)
        self.error_label.setText("Signing in...")
        task = get_task_runner().submit(
            _attempt_login,
            self.session,
            email,
            password,
            self.remember_check.isChecked(),
        )
        self._task = task
        task.signals.result.connect(self._on_result)
        task.signals.error.connect(self._on_error)
        task.signals.finished.connect(lambda task=task: self._task_finished(task))

    def _on_result(self, result: dict) -> None:
        if result.get("success"):
            warning = result.get("warning")
            if warning:
                QMessageBox.warning(
                    self,
                    "Could not remember sign-in",
                    "Login succeeded, but AnkerClient could not save the "
                    f"credentials securely:\n\n{warning}",
                )
            self.accept()
        else:
            self.show_error("Login failed. Check your email and password.")
            self._set_busy(False)

    def _on_error(self, message: str) -> None:
        self.show_error(f"Could not sign in: {message}")
        self._set_busy(False)

    def _task_finished(self, task: BackgroundTask) -> None:
        if self._task is task:
            self._task = None

    def _set_busy(self, busy: bool) -> None:
        self.email_input.setEnabled(not busy)
        self.password_input.setEnabled(not busy)
        self.remember_check.setEnabled(not busy)
        self.login_btn.setEnabled(not busy)
        # A request has a short timeout; keeping the dialog alive prevents a
        # successful late response from changing authentication invisibly.
        self.cancel_btn.setEnabled(not busy)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)

    def reject(self) -> None:
        if self._task:
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._task:
            event.ignore()
        else:
            event.accept()

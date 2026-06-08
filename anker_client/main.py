# anker_client/main.py
import sys
from PyQt6.QtWidgets import QApplication
from anker_client.core.session import AnkerSession
from anker_client.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AnkerClient")

    from anker_client.themes import get_qss
    from anker_client.settings import get_theme
    app.setStyleSheet(get_qss(get_theme()))

    session = AnkerSession()

    # Auto-login from keyring
    email, password = session.load_credentials()
    if email and password:
        try:
            session.login(email, password)
        except Exception:
            pass  # MainWindow will show login dialog when is_logged_in is False

    window = MainWindow(session)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

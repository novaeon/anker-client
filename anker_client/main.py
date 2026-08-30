# anker_client/main.py
import sys
from PyQt6.QtWidgets import QApplication
from anker_client.assets import get_app_icon
from anker_client.core.session import AnkerSession
from anker_client.core.tasks import get_task_runner
from anker_client.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AnkerClient")
    app.setWindowIcon(get_app_icon())

    from anker_client.themes import get_qss
    from anker_client.settings import get_theme
    app.setStyleSheet(get_qss(get_theme()))

    session = AnkerSession()

    window = MainWindow(session)
    window.show()
    exit_code = app.exec()
    # Cooperative cancellation prevents background callbacks from touching Qt
    # objects during interpreter teardown.
    get_task_runner().shutdown(5_000)
    session.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

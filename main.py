import sys

from suppress_console import suppress_console
suppress_console()

from PyQt6.QtWidgets import QApplication, QMessageBox

from handler import ADBNotFoundError, find_adb
from logging_config import setup_logging
from ui.theme import LIGHT
from ui.multi_device_window import MultiDeviceWindow

ADB_HELP_URL = "https://developer.android.com/studio/releases/platform-tools"


def main():
    logger = setup_logging()

    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")

        app.setApplicationName("ADB Explorer")
        app.setApplicationVersion("1.0.0")
        app.setQuitOnLastWindowClosed(True)

        app.setStyleSheet(LIGHT)

        try:
            logger.info(f"Using adb at {find_adb()}")
        except ADBNotFoundError as e:
            logger.error(str(e))
            QMessageBox.critical(
                None,
                "ADB Not Found",
                f"{e}\n\nDownload the Platform Tools:\n{ADB_HELP_URL}",
            )
            return 1

        window = MultiDeviceWindow()
        window.show()

        return app.exec()

    except Exception as e:
        logger.exception("Fatal error in application")
        QMessageBox.critical(
            None,
            "Fatal Error",
            f"A fatal error occurred:\n{str(e)}\n\nCheck the log file for more details."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

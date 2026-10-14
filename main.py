import atexit
import os
import shutil
import sys
import tempfile

from suppress_console import suppress_console
suppress_console()

from PyQt6.QtWidgets import QApplication, QMessageBox

from handler import ADBNotFoundError, find_adb
from logging_config import setup_logging
from ui.theme import stylesheet
from ui.multi_device_window import MultiDeviceWindow

ADB_HELP_URL = "https://developer.android.com/studio/releases/platform-tools"


def clear_transfer_cache():
    """Remove the scratch copies made for drag-outs and the PC clipboard."""
    shutil.rmtree(os.path.join(tempfile.gettempdir(), 'adb_explorer'), ignore_errors=True)


def install_exception_hook(logger):
    """Log unhandled exceptions instead of letting Qt abort the process.

    PyQt turns an uncaught exception inside a slot into a fatal abort, which
    would take down transfers running in other panels, so anything that
    escapes is logged and the app keeps running.
    """
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.exception("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = hook


def main():
    logger = setup_logging()
    install_exception_hook(logger)

    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")

        app.setApplicationName("ADB Explorer")
        app.setApplicationVersion("1.0.0")
        app.setQuitOnLastWindowClosed(True)

        app.setStyleSheet(stylesheet())

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

        # Scratch copies from a previous run are not worth keeping.
        atexit.register(clear_transfer_cache)

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

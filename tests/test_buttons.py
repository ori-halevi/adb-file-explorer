"""Toolbar button tests - no device needed.

    python tests/test_buttons.py

These exist because "To PC" silently did nothing. Qt's clicked() signal hands
a "checked" bool to any slot that can take an argument, so it arrived as
download_selected_items(dest_dir=False): the folder dialog was skipped and
the download then failed on a path made of a bool. The earlier tests called
the method directly and never pressed the button, so they all passed.
"""
import os
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtWidgets import QApplication, QFileDialog, QPushButton, QToolButton
from PyQt6.QtGui import QStandardItem
from PyQt6.QtCore import QItemSelectionModel

from ui import theme
from ui.device_panel import DevicePanel

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(theme.stylesheet())

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  PASS  ' if condition else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))


class FakeHandler:
    """Just enough of ADBHandler to let the panel build and transfer."""

    def __init__(self):
        self.device_serial = "TESTDEVICE"
        self.devices = {"TESTDEVICE": "Test Device"}
        self.device_connected = True
        self.last_error = None
        self.root_mode = None
        self.adb_missing = False
        self.calls = []

    def list_directory(self, path, use_root=False):
        return []

    def paths_exist(self, paths):
        return {p: False for p in paths}

    def pull_items(self, remote_paths, local_dir, line_callback=None):
        self.calls.append(('pull_items', list(remote_paths), local_dir))
        return True

    def pull_file(self, remote, local):
        self.calls.append(('pull_file', remote, local))
        return True

    def push_items(self, local_paths, remote_dir, line_callback=None, sync=False):
        self.calls.append(('push_items', list(local_paths), remote_dir))
        return True

    def push_file(self, local, remote):
        self.calls.append(('push_file', local, remote))
        return True

    def create_folder(self, path):
        return True

    def disk_usage_many(self, paths, timeout=20):
        return 0

    def kill_active(self):
        pass


handler = FakeHandler()
panel = DevicePanel(None, handler, device_info={"model": "Test Device"})
panel.resize(900, 500)
panel.show()
panel.current_path = "/sdcard/Pictures"

# the panel refreshes itself on creation; let that settle and then stop it,
# so the row below is what the test selects
for _ in range(50):
    app.processEvents()
panel.refresh_files = lambda *args, **kwargs: None
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])

# one selectable row, as a real listing would produce
row = [QStandardItem("photo.jpg"), QStandardItem("File"), QStandardItem("1024"),
       QStandardItem("-rw-rw----"), QStandardItem("2026-01-01 10:00")]
panel.tree_model.appendRow(row)
panel.tree_view.selectionModel().select(
    panel.tree_model.index(0, 0),
    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
app.processEvents()

print("\n=== 1. the panel offers the transfer buttons ===")
buttons = {b.text(): b for b in panel.findChildren(QPushButton)}
check("there is a To PC button", any("To PC" in t for t in buttons), str(list(buttons)))
check("there is a From PC button", any("From PC" in t for t in buttons))
check("there is a view button", bool(panel.findChildren(QToolButton)))
check("a row is selected", panel._selected_names() == ["photo.jpg"],
      str(panel._selected_names()))

print("\n=== 2. clicking To PC asks where to save, and downloads there ===")
chosen = tempfile.mkdtemp(prefix='adbx_btn_')
asked = []


def fake_dialog(parent, caption, directory="", *args, **kwargs):
    asked.append((caption, directory))
    return chosen


QFileDialog.getExistingDirectory = staticmethod(fake_dialog)

handler.calls.clear()
panel.to_pc_btn.click()
for _ in range(80):
    app.processEvents()
    if handler.calls:
        break

check("the folder dialog was shown", bool(asked), str(asked))
pulls = [c for c in handler.calls if c[0] == 'pull_items']
check("the download ran", bool(pulls), str(handler.calls))
if pulls:
    _, remote, local = pulls[0]
    check("it pulled the selected file",
          remote == ["/sdcard/Pictures/photo.jpg"], str(remote))
    check("it pulled into the chosen folder, not into a bool",
          isinstance(local, str) and local == chosen, repr(local))

print("\n=== 3. no slot receives Qt's checked bool by mistake ===")
received = {}


def spy(dest_dir=None):
    received['dest_dir'] = dest_dir


probe = QPushButton()
probe.clicked.connect(spy)
probe.click()
check("a bare connection would have passed False", received.get('dest_dir') is False,
      repr(received.get('dest_dir')))

panel._last_download_dir = None
panel.tree_view.selectionModel().select(
    panel.tree_model.index(0, 0),
    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
app.processEvents()
panel.download_selected_items(False)
for _ in range(40):
    app.processEvents()
check("download_selected_items ignores a stray bool and asks instead",
      len(asked) >= 2, f"{len(asked)} dialogs")

print("\n=== 4. clicking From PC asks for files ===")
picked = []


def fake_files(parent, caption, directory="", *args, **kwargs):
    picked.append(caption)
    return ([], "")


QFileDialog.getOpenFileNames = staticmethod(fake_files)
panel.from_pc_btn.click()
app.processEvents()
check("the file picker was shown", bool(picked), str(picked))

print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)

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

from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                             QMenu, QPushButton,
                             QToolButton)
from PyQt6.QtGui import QDrag, QStandardItem
from PyQt6.QtCore import QItemSelectionModel, QPoint, Qt
from PyQt6.QtTest import QTest

from ui import theme
from ui import views
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

def select_by_name(*wanted):
    selection = panel.tree_view.selectionModel()
    selection.clearSelection()
    for row in range(panel.tree_model.rowCount()):
        if panel.tree_model.data(panel.tree_model.index(row, 0)) in wanted:
            selection.select(panel.tree_model.index(row, 0),
                             QItemSelectionModel.SelectionFlag.Select
                             | QItemSelectionModel.SelectionFlag.Rows)
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

print("\n=== 5. selecting with the mouse works in every view ===")
# This is what made "To PC" look dead: a list view selects a cell, not a row,
# so selectedRows() came back empty while items looked selected on screen.
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
panel.all_files = []
for i in range(5):
    panel.tree_model.appendRow([
        QStandardItem(f"shot{i}.jpg"), QStandardItem("File"), QStandardItem("2048"),
        QStandardItem("-rw-rw----"), QStandardItem("2026-01-0%d 10:00" % (i + 1))])
    panel._items_by_name[f"shot{i}.jpg"] = {
        'name': f"shot{i}.jpg", 'path': f"/sdcard/Pictures/shot{i}.jpg",
        'size': 2048, 'modified': "2026-01-01 10:00", 'is_dir': False,
        'item': panel.tree_model.item(i, 0)}
app.processEvents()


def click_row(view, row, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, modifier,
                     view.visualRect(panel.tree_model.index(row, 0)).center())
    app.processEvents()


for mode, label, _ in views.VIEW_MODES:
    panel.set_view_mode(mode)
    app.processEvents()
    view = panel.tree_view
    view.resize(900, 500)
    panel.tree_view.selectionModel().clearSelection()
    click_row(view, 1)
    click_row(view, 3, Qt.KeyboardModifier.ControlModifier)
    picked_names = panel._selected_names()
    check(f"ctrl-click selects two items in {label}",
          picked_names == ["shot1.jpg", "shot3.jpg"], str(picked_names))

panel.set_view_mode(views.ICONS_M)
app.processEvents()
panel.tree_view.selectionModel().clearSelection()
click_row(panel.tree_view, 0)
click_row(panel.tree_view, 3, Qt.KeyboardModifier.ShiftModifier)
check("shift-click selects a range in an icon view",
      panel._selected_names() == ["shot0.jpg", "shot1.jpg", "shot2.jpg", "shot3.jpg"],
      str(panel._selected_names()))

print("\n=== 6. To PC works from an icon view, where it used to do nothing ===")
handler.calls.clear()
asked.clear()
panel.tree_view.selectionModel().clearSelection()
click_row(panel.tree_view, 2)
check("one item is selected in the icon view", panel._selected_names() == ["shot2.jpg"],
      str(panel._selected_names()))
panel.to_pc_btn.click()
for _ in range(80):
    app.processEvents()
    if handler.calls:
        break
pulls = [c for c in handler.calls if c[0] == 'pull_items']
check("the download ran from the icon view", bool(pulls), str(handler.calls))
if pulls:
    check("it pulled the item that was clicked",
          pulls[0][1] == ["/sdcard/Pictures/shot2.jpg"], str(pulls[0][1]))

print("\n=== 7. select all, invert, and the status line ===")
panel.select_all_items()
app.processEvents()
check("select all picks every row", len(panel._selected_names()) == 5,
      str(len(panel._selected_names())))
panel.invert_selection()
app.processEvents()
check("inverting a full selection leaves nothing", panel._selected_names() == [],
      str(panel._selected_names()))
panel.tree_view.selectionModel().clearSelection()
click_row(panel.tree_view, 1)
panel.invert_selection()
app.processEvents()
check("inverting one selection picks the other four",
      len(panel._selected_names()) == 4, str(panel._selected_names()))

panel.tree_view.selectionModel().clearSelection()
click_row(panel.tree_view, 0)
click_row(panel.tree_view, 1, Qt.KeyboardModifier.ControlModifier)
status = panel.status_label.text()
check("the status line counts the selection like a file manager",
      "2 files" in status and "4.0 KB" in status, status)

print("\n=== 8. the context menu follows the selection ===")
captured = []
original_exec = QMenu.exec


def capture(self, *args, **kwargs):
    captured.append([a.text().split(chr(9))[0] for a in self.actions()])
    return None


QMenu.exec = capture

panel.tree_view.selectionModel().clearSelection()
app.processEvents()
last = panel.tree_view.visualRect(panel.tree_model.index(
    panel.tree_model.rowCount() - 1, 0))
empty_spot = QPoint(last.right() + 40, last.bottom() + 40)
assert not panel.tree_view.indexAt(empty_spot).isValid(), "pick a truly empty spot"
panel.show_context_menu(empty_spot)            # empty space
background = captured[-1] if captured else []
check("the background menu offers View and Sort by",
      "View" in background and "Sort by" in background, str(background))
check("the background menu offers New and Upload from PC",
      "New" in background and "Upload from PC" in background, str(background))
check("the background menu does not offer Delete", "Delete" not in background,
      str(background))

click_row(panel.tree_view, 2)
panel.show_context_menu(panel.tree_view.visualRect(
    panel.tree_model.index(2, 0)).center())
single = captured[-1]
check("a single file offers Open on PC and Rename",
      "Open on PC" in single and "Rename" in single, str(single))
check("a single file offers Download to PC", "Download to PC..." in single, str(single))

panel.select_all_items()
app.processEvents()
panel.show_context_menu(panel.tree_view.visualRect(
    panel.tree_model.index(2, 0)).center())
many = captured[-1]
check("several items offer Batch rename instead of Rename",
      "Batch rename..." in many and "Rename" not in many, str(many))

print("\n=== 9. right-clicking outside the selection moves it ===")
panel.tree_view.selectionModel().clearSelection()
click_row(panel.tree_view, 0)
panel.show_context_menu(panel.tree_view.visualRect(
    panel.tree_model.index(4, 0)).center())
check("the menu acts on the item under the cursor",
      panel._selected_names() == ["shot4.jpg"], str(panel._selected_names()))

QMenu.exec = original_exec

print("\n=== 10. sorting ===")
from handler import FileItem
panel.all_files = [
    FileItem("b.txt", "/sdcard/Pictures/b.txt", False, 300, "-rw-", "2026-01-02 10:00"),
    FileItem("a.txt", "/sdcard/Pictures/a.txt", False, 900, "-rw-", "2026-01-03 10:00"),
    FileItem("zz", "/sdcard/Pictures/zz", True, 0, "drw-", "2026-01-01 10:00"),
]
panel.refresh_files = lambda *a, **k: None
panel.set_sort('name', False)
panel.apply_search_filter()
app.processEvents()
order = [panel.tree_model.data(panel.tree_model.index(r, 0))
         for r in range(panel.tree_model.rowCount())]
check("folders come first, then names",
      order == ["..", "zz", "a.txt", "b.txt"], str(order))

panel.set_sort('size', True)
app.processEvents()
order = [panel.tree_model.data(panel.tree_model.index(r, 0))
         for r in range(panel.tree_model.rowCount())]
check("sorting by size, largest first",
      order == ["..", "zz", "a.txt", "b.txt"], str(order))

panel.set_sort('size', False)
app.processEvents()
order = [panel.tree_model.data(panel.tree_model.index(r, 0))
         for r in range(panel.tree_model.rowCount())]
check("sorting by size, smallest first",
      order == ["..", "zz", "b.txt", "a.txt"], str(order))

print("\n=== 11. a thumbnail says what kind of file it is ===")
from PyQt6.QtGui import QColor, QPixmap
from ui import thumbnails

plain = QPixmap(96, 96)
plain.fill(QColor("#808080"))


def pixels(pixmap):
    image = pixmap.toImage()
    return bytes(image.constBits().asarray(image.sizeInBytes()))


untouched = thumbnails.decorate(QPixmap(plain), badge=None, video=False)
check("a photo with no known program is left alone",
      pixels(untouched) == pixels(plain))

as_video = thumbnails.decorate(QPixmap(plain), badge=None, video=True)
check("a video gets a play symbol", pixels(as_video) != pixels(plain))

badge_icon = panel._icon_for("something.jpg")
badged = thumbnails.decorate(QPixmap(plain), badge=badge_icon, video=False)
check("the program's icon is stamped on the thumbnail",
      pixels(badged) != pixels(plain))

tiny = QPixmap(16, 16)
tiny.fill(QColor("#808080"))
small = thumbnails.decorate(QPixmap(tiny), badge=badge_icon, video=True)
check("a tiny icon is not cluttered with marks", pixels(small) == pixels(tiny))

check("videos are recognised by name", thumbnails.is_video("clip.MP4") is True)
check("photos are not", thumbnails.is_video("photo.jpg") is False)

print("\n=== 12. the sort control ===")
check("the toolbar has a sort button", hasattr(panel, 'sort_btn'))
panel.set_sort('name', False)
check("it names the field and the direction",
      "Name" in panel.sort_btn.text() and "↑" in panel.sort_btn.text(),
      panel.sort_btn.text())
panel.set_sort('date', True)
check("it follows the choice",
      "Date modified" in panel.sort_btn.text() and "↓" in panel.sort_btn.text(),
      panel.sort_btn.text())
labels = [a.text() for a in panel.sort_btn.menu().actions() if a.text()]
check("the menu offers the usual fields",
      labels[:4] == ["Name", "Size", "Type", "Date modified"], str(labels))
check("and both directions", "Ascending" in labels and "Descending" in labels, str(labels))
checked = [a.text() for a in panel.sort_btn.menu().actions() if a.isChecked()]
check("what is in force is ticked", sorted(checked) == ["Date modified", "Descending"],
      str(checked))
check("a month-name date sorts chronologically",
      panel._sortable_date("Jan 2 2026") < panel._sortable_date("Feb 1 2026"),
      f"{panel._sortable_date('Jan 2 2026')} vs {panel._sortable_date('Feb 1 2026')}")
check("an ISO date is left as it is",
      panel._sortable_date("2026-01-02 10:00") == "2026-01-02 10:00")

print("\n=== 13. folders stay first, sorted inside each group ===")
from handler import FileItem

panel.all_files = [
    FileItem("m.txt", "/p/m.txt", False, 500, "-rw-", "2026-01-02 10:00"),
    FileItem("a.txt", "/p/a.txt", False, 100, "-rw-", "2026-01-05 10:00"),
    FileItem("zebra", "/p/zebra", True, 0, "drw-", "2026-01-01 10:00"),
    FileItem("alpha", "/p/alpha", True, 0, "drw-", "2026-01-09 10:00"),
]
panel.refresh_files = lambda *a, **k: None
for sort_key, expected in (
        ('name', ["..", "alpha", "zebra", "a.txt", "m.txt"]),
        ('size', ["..", "alpha", "zebra", "a.txt", "m.txt"]),
        ('date', ["..", "zebra", "alpha", "m.txt", "a.txt"])):
    panel.set_sort(sort_key, False)
    panel.apply_search_filter()
    app.processEvents()
    order = [panel.tree_model.data(panel.tree_model.index(row, 0))
             for row in range(panel.tree_model.rowCount())]
    check(f"sorting by {sort_key} keeps folders above files",
          order == expected, str(order))

print("\n=== 14. keeping the place in a long folder ===")
panel.refresh_files = DevicePanel.refresh_files.__get__(panel)
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
from handler import FileItem

many = [FileItem(f"row{i:03d}.txt", f"/sdcard/Pictures/row{i:03d}.txt", False,
                 100 + i, "-rw-", "2026-01-01 10:00") for i in range(300)]
handler.list_directory = lambda path, use_root=False: list(many)
panel.set_view_mode(views.DETAILS)
panel.current_path = "/sdcard/Pictures"
panel.refresh_files()
for _ in range(120):
    app.processEvents()
    if not panel._loading:
        break
for _ in range(20):
    app.processEvents()

view = panel.tree_view
view.resize(900, 400)
app.processEvents()
view.scrollTo(panel.tree_model.index(200, 0),
              QAbstractItemView.ScrollHint.PositionAtTop)
app.processEvents()
before_top = panel.tree_model.data(view.indexAt(view.viewport().rect().topLeft()))
before_scroll = view.verticalScrollBar().value()
select_by_name("row205.txt")
check("the view is scrolled down the folder", before_scroll > 0, str(before_scroll))

panel.refresh_files()
for _ in range(120):
    app.processEvents()
    if not panel._loading:
        break
for _ in range(40):
    app.processEvents()

after_top = panel.tree_model.data(view.indexAt(view.viewport().rect().topLeft()))
check("refreshing the same folder keeps the place",
      after_top == before_top, f"{before_top} -> {after_top}")
check("and keeps the selection", panel._selected_names() == ["row205.txt"],
      str(panel._selected_names()))

# moving to another folder must start at the top again
panel.current_path = "/sdcard/Other"
panel.refresh_files()
for _ in range(120):
    app.processEvents()
    if not panel._loading:
        break
for _ in range(40):
    app.processEvents()
check("a different folder starts at the top",
      panel.tree_view.verticalScrollBar().value() == 0,
      str(panel.tree_view.verticalScrollBar().value()))

print("\n=== 15. a move only deletes once the copy is confirmed ===")
sizes = {"/sdcard/a.txt": 100, "/sdcard/dest/a.txt": 100,
         "/sdcard/b.txt": 100, "/sdcard/dest/b.txt": 7}
handler.sizes_of = lambda paths: {p: sizes.get(p) for p in paths}
handler.checksums_of = lambda paths, timeout=600: {p: "same" for p in paths}

confirmed = panel.verify_arrived([("/sdcard/a.txt", "/sdcard/dest/a.txt")])
check("a copy of the same size is confirmed", confirmed == ["/sdcard/a.txt"],
      str(confirmed))

confirmed = panel.verify_arrived([("/sdcard/b.txt", "/sdcard/dest/b.txt")])
check("a truncated copy is not", confirmed == [], str(confirmed))

confirmed = panel.verify_arrived([("/sdcard/a.txt", "/sdcard/missing.txt")])
check("a copy that is not there is not", confirmed == [], str(confirmed))

settings = panel._settings()
settings.setValue("verify_checksum", True)
handler.checksums_of = lambda paths, timeout=600: {
    p: ("one" if p.endswith("a.txt") and "dest" not in p else "two") for p in paths}
confirmed = panel.verify_arrived([("/sdcard/a.txt", "/sdcard/dest/a.txt")])
check("with checking on, different contents are rejected", confirmed == [],
      str(confirmed))
handler.checksums_of = lambda paths, timeout=600: {p: "same" for p in paths}
confirmed = panel.verify_arrived([("/sdcard/a.txt", "/sdcard/dest/a.txt")])
check("and matching contents are accepted", confirmed == ["/sdcard/a.txt"],
      str(confirmed))
settings.setValue("verify_checksum", False)

print("\n=== 16. a selection too large to carry can be prepared once ===")
import shutil
import time as _time
from ui import transfer as transfer_module

scratch = transfer_module.temp_transfer_dir(handler.device_serial)
shutil.rmtree(scratch, ignore_errors=True)
scratch = transfer_module.temp_transfer_dir(handler.device_serial)

panel.refresh_files = lambda *a, **k: None
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
panel._items_by_name = {}
huge = 400 * 1024 * 1024
for index, (filename, filesize) in enumerate((("clip.mp4", huge), ("small.jpg", 1024))):
    panel.tree_model.appendRow([
        QStandardItem(filename), QStandardItem("File"), QStandardItem(str(filesize)),
        QStandardItem("-rw-"), QStandardItem("2026-01-01 10:00")])
    panel._items_by_name[filename] = {
        'name': filename, 'path': f"/sdcard/Pictures/{filename}", 'size': filesize,
        'modified': "2026-01-01 10:00", 'is_dir': False,
        'item': panel.tree_model.item(index, 0)}
app.processEvents()

def drag_payload():
    """What a receiver gets when it reads the drag's files."""
    return panel.fetch_for_drag(panel._selected_names())


select_by_name("clip.mp4")
payload = drag_payload()
check("a huge item is not fetched while the drag is in hand", payload == [],
      str(payload))
check("and the panel explains what will happen",
      "drag again" in panel.status_label.text().lower(), panel.status_label.text())


def fake_pull(remote_paths, local_dir, line_callback=None):
    handler.calls.append(('pull_items', list(remote_paths), local_dir))
    for remote in remote_paths:
        name = remote.rstrip('/').split('/')[-1]
        # a sparse file of the right length, rather than writing 400 MB
        with open(os.path.join(local_dir, name), 'wb') as handle:
            handle.truncate(panel._items_by_name[name]['size'])
    return True


handler.pull_items = fake_pull
handler.calls.clear()
panel.prepare_for_dragging(["clip.mp4"])
deadline = _time.time() + 20
while _time.time() < deadline and "ready" not in panel.status_label.text().lower():
    app.processEvents()
    _time.sleep(0.02)

check("preparing fetches it once", bool(handler.calls), str(handler.calls))
check("the panel says it is ready", "ready" in panel.status_label.text().lower(),
      panel.status_label.text())

handler.calls.clear()
payload = drag_payload()
check("the prepared item now drags whatever its size",
      payload and payload[0].endswith("clip.mp4"), str(payload))
check("and no second fetch was needed", handler.calls == [], str(handler.calls))

# a file that changed on the device must not be served from the old copy
panel._items_by_name["clip.mp4"]['size'] = huge + 17
payload = drag_payload()
check("a stale copy is not reused", payload == [], str(payload))

select_by_name("small.jpg")
handler.calls.clear()
payload = drag_payload()
check("a small item is still fetched on the spot",
      payload and payload[0].endswith("small.jpg"), str(payload))
shutil.rmtree(scratch, ignore_errors=True)

print("\n=== 17. a move out still deletes the original, even though the copy is gone ===")
# Windows performs a move by taking the copy and deleting it. Checking for
# that copy afterwards therefore finds nothing, which is what used to leave
# every moved file sitting on the device.
import shutil as _shutil
from ui import transfer as _transfer

scratch = _transfer.temp_transfer_dir(handler.device_serial)
_shutil.rmtree(scratch, ignore_errors=True)
scratch = _transfer.temp_transfer_dir(handler.device_serial)

panel.refresh_files = lambda *a, **k: None
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
panel._items_by_name = {}
panel.current_path = "/sdcard/Pictures"
panel.tree_model.appendRow([
    QStandardItem("gone.jpg"), QStandardItem("File"), QStandardItem("2048"),
    QStandardItem("-rw-"), QStandardItem("2026-01-01 10:00")])
panel._items_by_name["gone.jpg"] = {
    'name': "gone.jpg", 'path': "/sdcard/Pictures/gone.jpg", 'size': 2048,
    'modified': "2026-01-01 10:00", 'is_dir': False,
    'item': panel.tree_model.item(0, 0)}
panel.all_files = []
app.processEvents()

# the move asks before it deletes; answer yes without a dialog
from PyQt6.QtWidgets import QMessageBox as _QMessageBox
_QMessageBox.question = staticmethod(
    lambda *args, **kwargs: _QMessageBox.StandardButton.Yes)

deleted = []
handler.sizes_of = lambda paths: {p: 2048 for p in paths}
handler.delete_item = lambda path, is_dir=False, missing_ok=False: (
    deleted.append(path) or True)

with open(os.path.join(scratch, "gone.jpg"), 'wb') as handle:
    handle.write(b'x' * 2048)
select_by_name("gone.jpg")
payload = drag_payload()
check("the file was handed to the drag", bool(payload), str(payload))

os.remove(os.path.join(scratch, "gone.jpg"))       # the receiver moved it away
panel.confirm_move_to_pc(["/sdcard/Pictures/gone.jpg"])
deadline = _time.time() + 15
while _time.time() < deadline and not deleted:
    app.processEvents()
    _time.sleep(0.02)
check("the original is removed once the copy has been taken",
      deleted == ["/sdcard/Pictures/gone.jpg"], str(deleted))

# nothing was ever handed over: the original must stay
deleted.clear()
panel._handed_over = {}
panel.confirm_move_to_pc(["/sdcard/Pictures/gone.jpg"])
deadline = _time.time() + 10
while _time.time() < deadline and "kept" not in panel.status_label.text() \
        and "Could not" not in panel.status_label.text():
    app.processEvents()
    _time.sleep(0.02)
check("an unproven copy leaves the original alone", deleted == [], str(deleted))

# a copy that arrived truncated must also leave it alone
deleted.clear()
with open(os.path.join(scratch, "gone.jpg"), 'wb') as handle:
    handle.write(b'x' * 7)
panel._handed_over = {}
panel.confirm_move_to_pc(["/sdcard/Pictures/gone.jpg"])
deadline = _time.time() + 10
while _time.time() < deadline and "kept" not in panel.status_label.text() \
        and "Could not" not in panel.status_label.text():
    app.processEvents()
    _time.sleep(0.02)
check("a truncated copy leaves the original alone", deleted == [], str(deleted))
_shutil.rmtree(scratch, ignore_errors=True)

print("\n=== 18. what is kept on disk stays within bounds ===")
import shutil as _sh
from ui import transfer as _tr
from ui import thumbnails as _th

cap_dir = os.path.join(tempfile.gettempdir(), "adbx_cap_test")
_sh.rmtree(cap_dir, ignore_errors=True)
os.makedirs(cap_dir)
for index in range(6):
    with open(os.path.join(cap_dir, f"f{index}.bin"), 'wb') as handle:
        handle.write(b'x' * 1024)
    os.utime(os.path.join(cap_dir, f"f{index}.bin"), (1000 + index, 1000 + index))

removed = _tr.trim_folder(cap_dir, 3 * 1024)
left = sorted(os.listdir(cap_dir))
check("trimming brings a folder under its cap", len(left) <= 3, str(left))
check("and it drops the least recently used first",
      left == ["f3.bin", "f4.bin", "f5.bin"], str(left))
check("it reports what it freed", removed >= 3 * 1024, str(removed))

for index in range(6):
    path = os.path.join(cap_dir, f"g{index}.bin")
    with open(path, 'wb') as handle:
        handle.write(b'x' * 1024)
    os.utime(path, (1000 + index, 1000 + index))
keep = [os.path.join(cap_dir, "g0.bin")]
_tr.trim_folder(cap_dir, 1024, keep=keep)
check("what is about to be used is never thrown away",
      os.path.exists(keep[0]), str(sorted(os.listdir(cap_dir))))
_sh.rmtree(cap_dir, ignore_errors=True)

check("the scratch folder has a cap", _tr.SCRATCH_LIMIT > 0, str(_tr.SCRATCH_LIMIT))
check("the thumbnail cache has one too", _th.CACHE_LIMIT > 0, str(_th.CACHE_LIMIT))
check("trimming the thumbnail cache is safe to call",
      isinstance(_th.trim_cache(_th.CACHE_LIMIT), int))

print("\n=== 19. a refused drag prepares by itself ===")
scratch = _tr.temp_transfer_dir(handler.device_serial)
_sh.rmtree(scratch, ignore_errors=True)
scratch = _tr.temp_transfer_dir(handler.device_serial)

panel.refresh_files = lambda *a, **k: None
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
panel._items_by_name = {}
panel.current_path = "/sdcard/Pictures"
big = 300 * 1024 * 1024
panel.tree_model.appendRow([
    QStandardItem("movie.mp4"), QStandardItem("File"), QStandardItem(str(big)),
    QStandardItem("-rw-"), QStandardItem("2026-01-01 10:00")])
panel._items_by_name["movie.mp4"] = {
    'name': "movie.mp4", 'path': "/sdcard/Pictures/movie.mp4", 'size': big,
    'modified': "2026-01-01 10:00", 'is_dir': False,
    'item': panel.tree_model.item(0, 0)}
app.processEvents()


def fake_pull_big(remote_paths, local_dir, line_callback=None):
    handler.calls.append(('pull_items', list(remote_paths), local_dir))
    for remote in remote_paths:
        name = remote.rstrip('/').split('/')[-1]
        with open(os.path.join(local_dir, name), 'wb') as handle:
            handle.truncate(panel._items_by_name[name]['size'])
    return True


handler.pull_items = fake_pull_big
handler.calls.clear()
select_by_name("movie.mp4")

check("the drag cannot carry it", drag_payload() == [])
check("and the panel says it will be fetched",
      "drag again" in panel.status_label.text().lower(), panel.status_label.text())

# this is what letting go over the PC now does: no question, just the work
panel.offer_download_after_drag(["/sdcard/Pictures/movie.mp4"])
deadline = _time.time() + 20
while _time.time() < deadline and "ready" not in panel.status_label.text().lower():
    app.processEvents()
    _time.sleep(0.02)
check("letting go fetches it without asking", bool(handler.calls), str(handler.calls))
check("and then says it is ready", "ready" in panel.status_label.text().lower(),
      panel.status_label.text())

handler.calls.clear()
payload = drag_payload()
check("the next drag carries it", payload and payload[0].endswith("movie.mp4"),
      str(payload))
check("with no second fetch", handler.calls == [], str(handler.calls))
_sh.rmtree(scratch, ignore_errors=True)

print("\n=== 20. the drag hands over files only when they are asked for ===")
# Windows wants paths before the drag starts, which is why a large selection
# could not be dragged out at all. The data is produced on demand instead,
# so the drag always begins at once and the bytes are fetched at the drop.
import shutil as _sh2
from ui import transfer as _tr2
from ui.views import DeviceFiles

scratch = _tr2.temp_transfer_dir(handler.device_serial)
_sh2.rmtree(scratch, ignore_errors=True)
scratch = _tr2.temp_transfer_dir(handler.device_serial)

panel.refresh_files = lambda *a, **k: None
panel.tree_model.clear()
panel.tree_model.setHorizontalHeaderLabels(
    ['Name', 'Type', 'Size', 'Permissions', 'Modified'])
panel._items_by_name = {}
panel.current_path = "/sdcard/Pictures"
enormous = 900 * 1024 * 1024
panel.tree_model.appendRow([
    QStandardItem("huge.mp4"), QStandardItem("File"), QStandardItem(str(enormous)),
    QStandardItem("-rw-"), QStandardItem("2026-01-01 10:00")])
panel._items_by_name["huge.mp4"] = {
    'name': "huge.mp4", 'path': "/sdcard/Pictures/huge.mp4", 'size': enormous,
    'modified': "2026-01-01 10:00", 'is_dir': False,
    'item': panel.tree_model.item(0, 0)}
app.processEvents()


def fake_pull_huge(remote_paths, local_dir, line_callback=None):
    handler.calls.append(('pull_items', list(remote_paths), local_dir))
    for remote in remote_paths:
        name = remote.rstrip('/').split('/')[-1]
        with open(os.path.join(local_dir, name), 'wb') as handle:
            handle.truncate(panel._items_by_name[name]['size'])
    return True


handler.pull_items = fake_pull_huge
handler.calls.clear()
select_by_name("huge.mp4")

carried = {}
real_exec = QDrag.exec


def capture(self, *args, **kwargs):
    mime = self.mimeData()
    carried['advertises_files'] = mime.hasFormat("text/uri-list")
    carried['fetched_during_drag'] = bool(handler.calls)
    carried['mime'] = mime
    return Qt.DropAction.CopyAction


QDrag.exec = capture
panel.tree_view.startDrag(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
QDrag.exec = real_exec

check("the drag advertises files however large they are",
      carried.get('advertises_files') is True, str(carried.get('advertises_files')))
check("nothing is fetched while the drag is still moving",
      carried.get('fetched_during_drag') is False, str(handler.calls))
check("so a 900 MB video can be dragged at all",
      isinstance(carried.get('mime'), DeviceFiles))

# The receiver reads the data while the drag is still in the user's hand,
# so 900 MB must not be pulled here: that is what froze the machine. It is
# left for the moment the gesture ends instead.
mime = carried['mime']
urls = mime.urls()
check("a huge selection is not pulled inside the gesture",
      handler.calls == [], str(handler.calls))
check("so nothing is handed over yet", urls == [] and mime.fetched is False,
      str([u.toLocalFile() for u in urls]))
check("and the fetch is queued for when the drag is over",
      panel.take_pending_fetch() is True)
check("which only happens once", panel.take_pending_fetch() is False)

# A selection small enough to fetch in the moment still just works.
handler.calls.clear()
small = 2 * 1024 * 1024
panel._items_by_name["clip.mp4"] = dict(panel._items_by_name["huge.mp4"],
                                        name="clip.mp4", size=small,
                                        path="/sdcard/Pictures/clip.mp4")
light = DeviceFiles(panel, ["clip.mp4"], panel.fetch_for_drag)
urls = light.urls()
check("a small selection is fetched on the spot",
      [u.toLocalFile().replace('/', os.sep) for u in urls]
      == [os.path.join(scratch, "clip.mp4")],
      str([u.toLocalFile() for u in urls]))
check("which is when the fetch ran", bool(handler.calls), str(handler.calls))
check("and the drag knows it handed something over", light.fetched is True)
check("with nothing left queued", panel.take_pending_fetch() is False)

handler.calls.clear()
check("reading again does not fetch again",
      bool(light.urls()) and handler.calls == [], str(handler.calls))

# an internal drop must never pull anything
handler.calls.clear()
second = DeviceFiles(panel, ["huge.mp4"], panel.fetch_for_drag)
second.setData("application/x-adb-paths", b"TESTDEVICE\n/sdcard/Pictures/huge.mp4")
check("an internal drag reads the device format, not the files",
      second.hasFormat("application/x-adb-paths") and second.fetched is False)
check("and asking what it offers fetches nothing",
      "text/uri-list" in second.formats() and handler.calls == [], str(handler.calls))
_sh2.rmtree(scratch, ignore_errors=True)

print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)

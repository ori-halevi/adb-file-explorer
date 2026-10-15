"""Drag-and-drop tests - no device needed.

    python tests/test_dragdrop.py

The rules these check are the ones Windows Explorer uses:

* a drag within one storage moves, and between two storages copies
* Ctrl forces a copy and Shift forces a move, wherever it is dropped
* the accepted action is what paints the cursor, so it has to be set while
  the drag is still moving, not only when it lands
* a drag made with the right button asks on release
* files coming from the PC are only ever copied, so Windows never deletes
  the user's originals
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtCore import QEvent, QItemSelectionModel
from PyQt6.QtGui import (QDragMoveEvent, QDropEvent, QMouseEvent,
                         QStandardItem, QStandardItemModel)
from PyQt6.QtWidgets import QApplication, QMenu

from ui.views import ADB_MIME, ADB_RIGHT_MIME, DropTreeView, FileListView, ICONS_M

app = QApplication(sys.argv)
sys.stdout.reconfigure(line_buffering=True)

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  PASS  ' if condition else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))


class FakeSignal:
    """Stands in for the panel's cross_device_drop signal."""

    def __init__(self, calls):
        self._calls = calls

    def emit(self, *args):
        self._calls.append(('signal',) + args)


class FakePanel:
    """Stands in for the device panel the views talk to."""

    def __init__(self, serial):
        self.device_serial = serial
        self.current_path = "/sdcard/Pictures"
        self.adb_handler = None
        self.calls = []
        # Without this the view falls back to a modal warning, which would
        # block the test instead of failing it.
        self.cross_device_drop = FakeSignal(self.calls)

    def handle_drop_event(self, event, dest_dir=None):
        self.calls.append(('upload', dest_dir))

    def move_or_copy_items(self, paths, dest, move=False):
        self.calls.append(('move' if move else 'copy', list(paths), dest))

    def send_items_to_device(self, items, target, move=False, dest_dir=None):
        self.calls.append(('send-move' if move else 'send-copy', dest_dir))

    def is_dir_for_path(self, path):
        return False

    def drag_out_payload(self):
        return None

    def hint_drag_to_pc(self):
        pass

    def confirm_move_to_pc(self, paths):
        self.calls.append(('confirm-move-out', list(paths)))


MINE = "DEVICE-A"
OTHER = "DEVICE-B"

panel = FakePanel(MINE)
model = QStandardItemModel()
model.setHorizontalHeaderLabels(['Name', 'Type', 'Size', 'Permissions', 'Modified'])
for name, kind in (("Backup", "Directory"), ("a.jpg", "File"), ("b.jpg", "File")):
    model.appendRow([QStandardItem(name), QStandardItem(kind), QStandardItem("10"),
                     QStandardItem("-rw-"), QStandardItem("2026")])

view = DropTreeView(None, panel)
view.setModel(model)
view.resize(600, 400)
view.show()
app.processEvents()


# An event does not own its mime data, so these have to outlive the events
# that point at them - letting one be collected early crashes the process.
ALIVE = []


def adb_mime(serial, paths, right=False):
    mime = QMimeData()
    mime.setData(ADB_MIME, "\n".join([serial] + paths).encode("utf-8"))
    if right:
        mime.setData(ADB_RIGHT_MIME, b"1")
    ALIVE.append(mime)
    return mime


def pc_mime(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    ALIVE.append(mime)
    return mime


def move_event(mime, modifiers=Qt.KeyboardModifier.NoModifier, point=QPointF(10, 60)):
    event = QDragMoveEvent(
        point.toPoint(), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
        mime, Qt.MouseButton.LeftButton, modifiers)
    ALIVE.append(event)
    return event


def drop_event(mime, modifiers=Qt.KeyboardModifier.NoModifier, point=QPointF(10, 60)):
    event = QDropEvent(
        point, Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
        mime, Qt.MouseButton.LeftButton, modifiers)
    ALIVE.append(event)
    return event


print("\n=== 1. what a plain drag means, by where it came from ===")
check("a drag within one device moves",
      view.default_action_for(MINE) == Qt.DropAction.MoveAction)
check("a drag from another device copies",
      view.default_action_for(OTHER) == Qt.DropAction.CopyAction)
check("a drag from nowhere known copies",
      view.default_action_for("") == Qt.DropAction.CopyAction)

print("\n=== 2. Ctrl and Shift override it, as in Explorer ===")
for serial, label in ((MINE, "same device"), (OTHER, "another device")):
    event = move_event(adb_mime(serial, ["/sdcard/DCIM/a.jpg"]),
                       Qt.KeyboardModifier.ControlModifier)
    view.dragMoveEvent(event)
    check(f"Ctrl forces a copy ({label})",
          event.dropAction() == Qt.DropAction.CopyAction and event.isAccepted())

    event = move_event(adb_mime(serial, ["/sdcard/DCIM/a.jpg"]),
                       Qt.KeyboardModifier.ShiftModifier)
    view.dragMoveEvent(event)
    check(f"Shift forces a move ({label})",
          event.dropAction() == Qt.DropAction.MoveAction and event.isAccepted())

event = move_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"]))
view.dragMoveEvent(event)
check("with no modifier the cursor shows the default (move, same device)",
      event.dropAction() == Qt.DropAction.MoveAction)

event = move_event(adb_mime(OTHER, ["/sdcard/DCIM/a.jpg"]))
view.dragMoveEvent(event)
check("with no modifier across devices the cursor shows copy",
      event.dropAction() == Qt.DropAction.CopyAction)

print("\n=== 3. the drop does what the cursor promised ===")
panel.calls.clear()
view.dropEvent(drop_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"])))
check("a plain drop within the device moves",
      panel.calls and panel.calls[0][0] == 'move', str(panel.calls))

panel.calls.clear()
view.dropEvent(drop_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"]),
                          Qt.KeyboardModifier.ControlModifier))
check("Ctrl makes the same drop a copy",
      panel.calls and panel.calls[0][0] == 'copy', str(panel.calls))

panel.calls.clear()
view.dropEvent(drop_event(adb_mime(OTHER, ["/sdcard/DCIM/a.jpg"])))
check("a drop from another device copies across",
      panel.calls and panel.calls[0][0] in ('send-copy', 'signal')
      and panel.calls[0][-1] is False, str(panel.calls))

panel.calls.clear()
view.dropEvent(drop_event(adb_mime(OTHER, ["/sdcard/DCIM/a.jpg"]),
                          Qt.KeyboardModifier.ShiftModifier))
check("Shift makes it a move across devices",
      panel.calls and panel.calls[0][0] in ('send-move', 'signal')
      and panel.calls[0][-1] is True, str(panel.calls))

print("\n=== 4. dropping onto a folder row uses that folder ===")
panel.calls.clear()
folder_point = QPointF(view.visualRect(model.index(0, 0)).center())
view.dropEvent(drop_event(adb_mime(OTHER, ["/sdcard/DCIM/a.jpg"]), point=folder_point))
check("the destination is the folder under the cursor",
      panel.calls and "/sdcard/Pictures/Backup" in panel.calls[0], str(panel.calls))

print("\n=== 5. files from the PC are only ever copied ===")
event = move_event(pc_mime([__file__]), Qt.KeyboardModifier.ShiftModifier)
view.dragMoveEvent(event)
check("even with Shift held, an incoming drop is a copy",
      event.dropAction() == Qt.DropAction.CopyAction, event.dropAction().name)

panel.calls.clear()
drop = drop_event(pc_mime([__file__]), Qt.KeyboardModifier.ShiftModifier)
view.dropEvent(drop)
check("the upload runs", panel.calls and panel.calls[0][0] == 'upload', str(panel.calls))
check("and the action reported back is copy, so nothing is deleted on the PC",
      drop.dropAction() == Qt.DropAction.CopyAction)

print("\n=== 6. a right-button drag asks on release ===")
chosen = {'label': "Move here"}
original_exec = QMenu.exec


def fake_menu(self, *args, **kwargs):
    chosen['offered'] = [a.text() for a in self.actions() if a.text()]
    chosen['default'] = self.defaultAction().text() if self.defaultAction() else None
    for action in self.actions():
        if action.text() == chosen['label']:
            return action
    return None


QMenu.exec = fake_menu

panel.calls.clear()
view.dropEvent(drop_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"], right=True)))
check("the menu offers copy, move and cancel",
      chosen.get('offered') == ["Copy here", "Move here", "Cancel"], str(chosen.get('offered')))
check("the action that would have happened is the bold one",
      chosen.get('default') == "Move here", str(chosen.get('default')))
check("choosing Move here moves", panel.calls and panel.calls[0][0] == 'move',
      str(panel.calls))

chosen['label'] = "Copy here"
panel.calls.clear()
view.dropEvent(drop_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"], right=True)))
check("choosing Copy here copies", panel.calls and panel.calls[0][0] == 'copy',
      str(panel.calls))

chosen['label'] = "Cancel"
panel.calls.clear()
view.dropEvent(drop_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"], right=True)))
check("choosing Cancel does nothing at all", panel.calls == [], str(panel.calls))

QMenu.exec = original_exec

print("\n=== 7. the icon views behave the same way ===")
list_view = FileListView(None, panel)
list_view.setModel(model)
list_view.apply_mode(ICONS_M)
list_view.resize(600, 400)
list_view.show()
app.processEvents()

event = move_event(adb_mime(MINE, ["/sdcard/DCIM/a.jpg"]), Qt.KeyboardModifier.ControlModifier)
list_view.dragMoveEvent(event)
check("Ctrl copies in an icon view too",
      event.dropAction() == Qt.DropAction.CopyAction)
check("an icon view reports selected rows",
      hasattr(list_view, 'selected_rows'))

print("\n=== 8. a drag starts from the selection, with either button ===")
# Pressing and sweeping is how several items get picked, so a drag may only
# begin on something already selected. Otherwise a quick flick across a few
# files would drag one of them instead of selecting them.
from PyQt6.QtCore import QEvent, QItemSelectionModel
from PyQt6.QtGui import QMouseEvent

started = []
view.startDrag = lambda actions, right_button=False: started.append(
    {'right': right_button})


def press(button, point, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(point), QPointF(point),
                        button, button, modifiers)
    ALIVE.append(event)
    view.mousePressEvent(event)


def move(button, point):
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(point),
                        Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    ALIVE.append(event)
    view.mouseMoveEvent(event)


def release(button, point):
    event = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(point), QPointF(point),
                        button, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    ALIVE.append(event)
    view.mouseReleaseEvent(event)


def select_rows(*rows):
    selection = view.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(model.index(row, 0),
                         QItemSelectionModel.SelectionFlag.Select
                         | QItemSelectionModel.SelectionFlag.Rows)
    app.processEvents()


def drag_from(button, row, selected_rows):
    """Run the whole gesture, reporting what happened while it was running."""
    select_rows(*selected_rows)
    started.clear()
    point = view.visualRect(model.index(row, 0)).center()
    press(button, point)
    move(button, point + QPoint(70, 70))
    outcome = list(started)
    could_drag = view.dragEnabled()
    release(button, point + QPoint(70, 70))
    return outcome, could_drag


for button, label, asks in ((Qt.MouseButton.LeftButton, "left", False),
                            (Qt.MouseButton.RightButton, "right", True)):
    outcome, _ = drag_from(button, 1, (1, 2))
    check(f"a {label}-drag from a selected item starts a drag",
          len(outcome) == 1, str(outcome))
    check(f"and it {'asks on release' if asks else 'does not ask'}",
          outcome and outcome[0]['right'] is asks, str(outcome))
    check(f"the whole selection travels with a {label}-drag",
          [model.data(i) for i in view.selected_rows()] == ["a.jpg", "b.jpg"],
          str([model.data(i) for i in view.selected_rows()]))

    outcome, could_drag = drag_from(button, 1, (2,))
    check(f"a {label}-drag from an unselected item starts no drag",
          outcome == [], str(outcome))
    check(f"the view sweeps instead, with its own dragging held off",
          could_drag is False)
    check(f"dragging is available again once a {label} sweep ends",
          view.dragEnabled() is True)

select_rows(1)
started.clear()
point = view.visualRect(model.index(1, 0)).center()
press(Qt.MouseButton.LeftButton, point)
release(Qt.MouseButton.LeftButton, point)
check("a click that does not move starts no drag", started == [], str(started))

del view.startDrag

print("\n=== 9. only a right-drag carries the marker that asks ===")
plain = QMimeData()
plain.setData(ADB_MIME, b"DEVICE-A\n/sdcard/a.jpg")
ALIVE.append(plain)
check("a left-drag's data has no asking marker", not plain.hasFormat(ADB_RIGHT_MIME))
asking = adb_mime(MINE, ["/sdcard/a.jpg"], right=True)
check("a right-drag's data does", asking.hasFormat(ADB_RIGHT_MIME))

print("\n=== 10. a gesture leaves nothing painted behind it ===")
# The sweep rectangle is drawn by the view that handled the press, and it
# is only erased by the matching release. A drag that swallows the release
# used to leave the rectangle on screen for the rest of the session.
from PyQt6.QtWidgets import QAbstractItemView

for label, view in (("tree", DropTreeView(None, panel)), ("list", FileListView(None, panel))):
    model = QStandardItemModel()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        model.appendRow([QStandardItem(name)])
    view.setModel(model)
    view.resize(400, 300)
    painted = []
    view.viewport().update = lambda *a, **k: painted.append(True)

    view._drag_took_over = True        # as it is when a drag ate the release
    view.setState(QAbstractItemView.State.DragSelectingState)
    view.selectionModel().select(
        model.index(0, 0),
        QItemSelectionModel.SelectionFlag.Select
        | QItemSelectionModel.SelectionFlag.Rows)
    clicks = []
    view.clicked.connect(lambda index: clicks.append(index))
    view.mouseReleaseEvent(
        QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(10, 10),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier))
    check(f"{label}: a swallowed release still ends the sweep",
          view.state() == QAbstractItemView.State.NoState, str(view.state()))
    check(f"{label}: and repaints, so the rectangle goes", bool(painted))
    check(f"{label}: without costing the dragged selection",
          [i.row() for i in view.selectionModel().selectedRows()] == [0],
          str([i.row() for i in view.selectionModel().selectedRows()]))
    check(f"{label}: and without reading as a click", clicks == [], str(clicks))

print("\n=== 11. a drag too large to fetch is only deferred ===")
# Preparing it is all that happens here. Finishing is the second drag,
# which is a real Windows drag: Explorer does the copying, with its own
# progress, and a move still ends in the usual question about the
# originals - neither of which an in-app dialog can stand in for.
from ui.transfer_actions import TransferActionsMixin


class Deferred(TransferActionsMixin):
    """Just the parts of a panel the deferral touches."""

    def __init__(self):
        self._notes = []
        self.device_serial = "TESTDEVICE"

    def _note(self, text):
        self._notes.append(text)


holder = Deferred()
holder._fetch_after_gesture(["a.jpg", "b.jpg"])
check("the names are kept for the fetch",
      holder._pending_fetch == ["a.jpg", "b.jpg"], str(holder._pending_fetch))
check("and the user is told what is happening",
      any("drag again" in note for note in holder._notes), str(holder._notes))
check("a drop inside the app forgets it",
      holder.take_pending_fetch(run=False) is False
      and holder._pending_fetch is None)
holder._fetch_after_gesture(["a.jpg"])
check("a drop outside runs it, once",
      holder.take_pending_fetch() is True and holder.take_pending_fetch() is False)

print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)

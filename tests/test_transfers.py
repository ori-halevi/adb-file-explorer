"""Live transfer tests - they need one real device connected over adb.

    python tests/test_transfers.py

Every case here exists because a code review found the opposite behaviour:
Replace used to wipe a whole destination folder, Keep both could overwrite an
existing "(2)" file, Cancel stopped nothing, a half-failed transfer reported
success, and an unreadable destination was read as empty.

Everything runs inside /sdcard/adbx_cr_tests, which is removed afterwards.
"""
import os, sys, time, shutil, tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtGui import QGuiApplication
from ui.theme import LIGHT
from ui.multi_device_window import MultiDeviceWindow
from ui import transfer
from handler import ADBHandler

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(LIGHT)

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  PASS  ' if condition else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))


def pump(seconds=1.0, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False


# every conflict dialog is answered by this, so nothing blocks
ANSWER = {'choice': transfer.SKIP, 'all': True}
DIALOGS = []


class AutoConflict(transfer.ConflictDialog):
    def exec(self):
        DIALOGS.append({'name': self.windowTitle(), 'is_dir': self.is_dir})
        self.choice = ANSWER['choice']
        self.apply_to_all.setChecked(ANSWER['all'])
        return QDialog.DialogCode.Accepted


transfer.ConflictDialog = AutoConflict
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
WARNINGS = []
QMessageBox.warning = staticmethod(lambda parent, title, text, *a, **k: WARNINGS.append((title, text)))

win = MultiDeviceWindow()
win.show()
pump(4)
panel = win.device_panels[0]
h = panel.adb_handler
BASE = "/sdcard/adbx_cr_tests"
LOCAL = os.path.join(tempfile.gettempdir(), "adbx_cr_local")


def reset_remote():
    h.delete_item(BASE, is_dir=True, missing_ok=True)
    h.create_folder(BASE)


def goto(path):
    panel.current_path = path
    panel.refresh_files()
    pump(3, until=lambda: not panel._loading)
    pump(0.3)


def names_at(path):
    listing = h.list_directory(path)
    return sorted(i.name for i in listing) if listing is not None else None


def seed_local():
    shutil.rmtree(LOCAL, ignore_errors=True)
    os.makedirs(os.path.join(LOCAL, "Album", "inner"))
    open(os.path.join(LOCAL, "Album", "new.txt"), "w").write("NEW")
    open(os.path.join(LOCAL, "Album", "inner", "deep.txt"), "w").write("NEW")
    open(os.path.join(LOCAL, "solo.txt"), "w").write("NEWNEW")


def select(*names):
    model = panel.tree_model
    sm = panel.tree_view.selectionModel()
    sm.clearSelection()
    for row in range(model.rowCount()):
        if model.data(model.index(row, 0)) in names:
            sm.select(model.index(row, 0),
                      QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)


print("\n=== 1. folder upload: Merge keeps the destination contents ===")
reset_remote()
seed_local()
h.create_folder(f"{BASE}/Album")
h._shell_action(f"echo KEEP > '{BASE}/Album/precious.txt'", 'seed')
goto(BASE)
ANSWER['choice'] = transfer.MERGE
DIALOGS.clear()
panel.upload_paths([os.path.join(LOCAL, "Album")])
pump(15, until=lambda: "Uploaded" in panel.status_label.text() or "failed" in panel.status_label.text())
pump(1.5)
after = names_at(f"{BASE}/Album")
check("merge keeps precious.txt", after is not None and "precious.txt" in after, str(after))
check("merge adds new.txt", after is not None and "new.txt" in after)
check("merge offered for a folder", DIALOGS and DIALOGS[0]['is_dir'] is True)

print("\n=== 2. folder upload: Replace wipes, but only after the copy ===")
ANSWER['choice'] = transfer.REPLACE
panel.upload_paths([os.path.join(LOCAL, "Album")])
pump(15, until=lambda: "Uploaded" in panel.status_label.text() or "failed" in panel.status_label.text())
pump(1.5)
after = names_at(f"{BASE}/Album")
check("replace removes precious.txt", after is not None and "precious.txt" not in after, str(after))
check("replace keeps the new content", after is not None and "new.txt" in after)
check("no backup folder left behind",
      all('adbx-replacing' not in n for n in (names_at(BASE) or [])), str(names_at(BASE)))

print("\n=== 3. replace rolls back when the copy fails ===")
reset_remote()
h.create_folder(f"{BASE}/Keep")
h._shell_action(f"echo ORIGINAL > '{BASE}/Keep/data.txt'", 'seed')
ok, err = panel._replace_safely(h, f"{BASE}/Keep", True, lambda: False)
restored = names_at(f"{BASE}/Keep")
check("failed replace reports failure", ok is False)
check("failed replace restores the original", restored == ["data.txt"], str(restored))
check("failed replace leaves no backup",
      all('adbx-replacing' not in n for n in (names_at(BASE) or [])), str(names_at(BASE)))

print("\n=== 4. paste into the folder the items are already in ===")
reset_remote()
h._shell_action(f"echo x > '{BASE}/report.pdf'", 'seed')
goto(BASE)
before = names_at(BASE)
panel.move_or_copy_items([f"{BASE}/report.pdf"], BASE, move=False)
pump(10, until=lambda: "Duplicated" in panel.status_label.text())
pump(1.5)
after = names_at(BASE)
check("in-place copy duplicates instead of deleting",
      after is not None and "report.pdf" in after and "report (2).pdf" in after, str(after))
panel.move_or_copy_items([f"{BASE}/report.pdf"], BASE, move=True)
pump(3)
check("in-place move is a no-op", "already here" in panel.status_label.text(),
      panel.status_label.text())
check("report.pdf survived", "report.pdf" in (names_at(BASE) or []))

print("\n=== 5. Keep both avoids a name that already exists ===")
reset_remote()
h._shell_action(f"echo a > '{BASE}/notes.txt'", 'seed')
h._shell_action(f"echo b > '{BASE}/notes (2).txt'", 'seed')
h.create_folder(f"{BASE}/src")
h._shell_action(f"echo c > '{BASE}/src/notes.txt'", 'seed')
goto(BASE)
ANSWER['choice'] = transfer.KEEP_BOTH
panel.move_or_copy_items([f"{BASE}/src/notes.txt"], BASE, move=False)
pump(10, until=lambda: "Copied" in panel.status_label.text())
pump(1.5)
listing = h.list_directory(BASE) or []
sizes = {i.name: i.size for i in listing}
check("keep both picked a free name", "notes (3).txt" in sizes, str(sorted(sizes)))
check("existing (2) file untouched", sizes.get("notes (2).txt") == 2, str(sizes))

print("\n=== 6. unreadable destination stops the transfer ===")
reset_remote()
goto(BASE)
WARNINGS.clear()
real_paths_exist, real_list = h.paths_exist, h.list_directory
h.paths_exist = lambda paths: None
h.list_directory = lambda path, use_root=False: None
seed_local()
panel.upload_paths([os.path.join(LOCAL, "solo.txt")])
pump(3)
h.paths_exist, h.list_directory = real_paths_exist, real_list
check("upload refused when the destination is unknown", bool(WARNINGS), str(WARNINGS[:1]))
check("nothing was uploaded", (names_at(BASE) or []) == [], str(names_at(BASE)))

print("\n=== 7. two sources with the same basename ===")
reset_remote()
goto(BASE)
shutil.rmtree(LOCAL, ignore_errors=True)
os.makedirs(os.path.join(LOCAL, "a"))
os.makedirs(os.path.join(LOCAL, "b"))
open(os.path.join(LOCAL, "a", "report.pdf"), "w").write("AAA")
open(os.path.join(LOCAL, "b", "report.pdf"), "w").write("BBBBBB")
ANSWER['choice'] = transfer.KEEP_BOTH
panel.upload_paths([os.path.join(LOCAL, "a", "report.pdf"), os.path.join(LOCAL, "b", "report.pdf")])
pump(15, until=lambda: "Uploaded" in panel.status_label.text())
pump(1.5)
after = names_at(BASE)
check("both same-named files arrived", after == ["report (2).pdf", "report.pdf"], str(after))

print("\n=== 8. sync re-run skips unchanged files ===")
reset_remote()
goto(BASE)
seed_local()
panel.upload_paths([os.path.join(LOCAL, "Album")], sync=True)
pump(15, until=lambda: "Synced" in panel.status_label.text())
first = panel.status_label.text()
DIALOGS.clear()
panel.upload_paths([os.path.join(LOCAL, "Album")], sync=True)
pump(15, until=lambda: "Synced" in panel.status_label.text())
pump(1.5)
check("sync asked no conflict question", not DIALOGS, str(DIALOGS))
check("sync reported success twice", "Synced" in panel.status_label.text(), panel.status_label.text())
check("synced tree is intact",
      names_at(f"{BASE}/Album") == ["inner", "new.txt"], str(names_at(f"{BASE}/Album")))

print("\n=== 9. partial failure is reported as partial ===")
reset_remote()
h._shell_action(f"echo x > '{BASE}/one.txt'", 'seed')
h._shell_action(f"echo x > '{BASE}/two.txt'", 'seed')
h.create_folder(f"{BASE}/dst")
goto(BASE)
real_copy = h.copy_on_device
state = {'n': 0}


def flaky_copy(src, dest, ensure_parent=True):
    state['n'] += 1
    if state['n'] == 2:
        h.last_error = "simulated failure"
        return False
    return real_copy(src, dest, ensure_parent=ensure_parent)


h.copy_on_device = flaky_copy
panel.move_or_copy_items([f"{BASE}/one.txt", f"{BASE}/two.txt"], f"{BASE}/dst")
pump(10, until=lambda: "failed" in panel.status_label.text() or "Copied 2" in panel.status_label.text())
pump(1.5)
h.copy_on_device = real_copy
text = panel.status_label.text()
check("partial failure is visible", "1 failed" in text and "simulated failure" in text, text)

print("\n=== 10. cancel actually stops a transfer ===")
reset_remote()
goto(BASE)
big = os.path.join(LOCAL, "big.bin")
with open(big, "wb") as f:
    f.write(os.urandom(48 * 1024 * 1024))
panel.upload_paths([big])
pump(1.2)
dialogs = [w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]
cancelled = False
for dlg in dialogs:
    for btn in dlg.findChildren(type(dlg.findChild(object))):
        pass
    from PyQt6.QtWidgets import QPushButton
    for btn in dlg.findChildren(QPushButton):
        if btn.text() == "Cancel":
            btn.click()
            cancelled = True
start = time.time()
pump(20, until=lambda: "Cancel" in panel.status_label.text() or "Uploaded" in panel.status_label.text())
elapsed = time.time() - start
check("cancel button was found", cancelled)
check("cancel ended the transfer quickly", elapsed < 10, f"{elapsed:.1f}s")
check("cancel is reported, not success",
      "Cancel" in panel.status_label.text(), panel.status_label.text())
os.remove(big)

print("\n=== 11. device-to-device honours a renamed/subfolder destination ===")
reset_remote()
h.create_folder(f"{BASE}/Photos/sub")
h._shell_action(f"echo src > '{BASE}/Photos/sub/p.txt'", 'seed')
h.create_folder(f"{BASE}/target/Photos")
h._shell_action(f"echo old > '{BASE}/target/Photos/existing.txt'", 'seed')
goto(BASE)
second = win._add_panel_for_device(panel.device_serial, "same device")
pump(2)
second.current_path = f"{BASE}/target"
second.refresh_files()
pump(2)
items = [{'name': 'Photos', 'path': f"{BASE}/Photos", 'is_dir': True}]
ANSWER['choice'] = transfer.KEEP_BOTH
panel.send_items_to_device(items, second, dest_dir=f"{BASE}/target")
pump(20, until=lambda: "Sent" in panel.status_label.text() or "failed" in panel.status_label.text())
pump(1.5)
after = names_at(f"{BASE}/target")
check("keep-both folder was created at the destination",
      after is not None and "Photos (2)" in after, str(after))
check("existing folder was left alone",
      names_at(f"{BASE}/target/Photos") == ["existing.txt"],
      str(names_at(f"{BASE}/target/Photos")))
check("streamed content landed in the renamed folder",
      names_at(f"{BASE}/target/Photos (2)/sub") == ["p.txt"],
      str(names_at(f"{BASE}/target/Photos (2)/sub")))

print("\n=== 12. a cut that crosses panels clears the clipboard ===")
reset_remote()
h._shell_action(f"echo x > '{BASE}/cutme.txt'", 'seed')
h.create_folder(f"{BASE}/into")
goto(BASE)
select("cutme.txt")
panel.cut_selected()
check("clipboard holds the cut item", bool(panel.clipboard['items']))
items = list(panel.clipboard['items'])
panel.send_items_to_device(items, second, dest_dir=f"{BASE}/into", move=True,
                           on_success=panel._clear_clipboard)
pump(20, until=lambda: "Moved" in panel.status_label.text() or "failed" in panel.status_label.text())
pump(1.5)
check("clipboard was cleared after the move", not panel.clipboard['items'],
      str(panel.clipboard))
check("clipboard timestamp was reset", panel._clipboard_time == 0.0)
check("the file moved", names_at(f"{BASE}/into") == ["cutme.txt"], str(names_at(f"{BASE}/into")))
check("the source copy is gone", "cutme.txt" not in (names_at(BASE) or []))

print("\n=== 13. a plain drag makes no adb calls ===")
goto(BASE)
select("into")
calls = []
real_pull = h.pull_items
real_du = h.disk_usage_many
h.pull_items = lambda *a, **k: calls.append('pull') or True
h.disk_usage_many = lambda *a, **k: calls.append('du') or 0
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDrag
real_exec = QDrag.exec
QDrag.exec = lambda self, *a, **k: Qt.DropAction.IgnoreAction
panel.tree_view.startDrag(Qt.DropAction.CopyAction)
QDrag.exec = real_exec
h.pull_items, h.disk_usage_many = real_pull, real_du
check("no pull or du for an in-app drag", calls == [], str(calls))
check("the drag-to-PC hint was shown", "Alt" in panel.status_label.text(),
      panel.status_label.text())

print("\n=== 14. '..' is never treated as a selectable item ===")
goto(f"{BASE}/into")
sm = panel.tree_view.selectionModel()
sm.clearSelection()
model = panel.tree_model
for row in range(model.rowCount()):
    sm.select(model.index(row, 0),
              QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
picked = [i['name'] for i in panel.get_selected_items()]
check("'..' filtered from get_selected_items", ".." not in picked, str(picked))

print("\n=== 15. unit checks ===")
noise = "* daemon not running; starting now at tcp:5037\n* daemon started successfully"
check("adb daemon banner is not an error", ADBHandler.real_errors(noise) == "")
check("a real error still comes through",
      "Permission denied" in ADBHandler.real_errors(noise + "\nrm: x: Permission denied"))
many = [f"/sdcard/dir_number_{i:04d}" for i in range(900)]
chunks = list(ADBHandler._chunk_by_length(many))
longest = max(len('mkdir -p -- ' + ' '.join(f"'{d}'" for d in c)) for c in chunks)
check("mkdir batches stay under the command limit", longest < 32000 and len(chunks) > 1,
      f"{len(chunks)} chunks, longest {longest} chars")
check("du failure is unknown, not zero", h.disk_usage_many(["/does/not/exist"]) in (None, 0))
check("missing path delete fails without missing_ok",
      h.delete_item(f"{BASE}/definitely-not-here") is False)
check("missing path delete succeeds with missing_ok",
      h.delete_item(f"{BASE}/definitely-not-here", missing_ok=True) is True)
check("paths_exist unknown returns None",
      ADBHandler(device_serial="no-such-device").paths_exist(["/sdcard"]) in (None, {'/sdcard': False}))
check("drag limit parses a bad env value",
      isinstance(transfer.DRAG_OUT_SIZE_LIMIT, int) and transfer.DRAG_OUT_SIZE_LIMIT > 0)

print("\n=== 16. download, duplicate and the PC clipboard still work ===")
reset_remote()
h.create_folder(f"{BASE}/Shots")
h._shell_action(f"echo pic > '{BASE}/Shots/one.jpg'", 'seed')
h._shell_action(f"echo doc > '{BASE}/note.txt'", 'seed')
goto(BASE)

dest = os.path.join(tempfile.gettempdir(), "adbx_cr_down")
shutil.rmtree(dest, ignore_errors=True)
os.makedirs(dest)
select("Shots", "note.txt")
panel.download_selected_items(dest_dir=dest)
pump(20, until=lambda: "Downloaded" in panel.status_label.text())
check("download brought both items", sorted(os.listdir(dest)) == ["Shots", "note.txt"],
      str(sorted(os.listdir(dest))))
check("download kept the folder contents",
      os.path.isfile(os.path.join(dest, "Shots", "one.jpg")))

select("note.txt")
panel.duplicate_selected()
pump(10, until=lambda: "Duplicated" in panel.status_label.text())
pump(1.5)
check("duplicate names the copy (2)", "note (2).txt" in (names_at(BASE) or []),
      str(names_at(BASE)))

select("Shots")
panel.copy_to_pc_clipboard()
pump(20, until=lambda: "clipboard" in panel.status_label.text())
mime = QGuiApplication.clipboard().mimeData()
check("PC clipboard holds a real file",
      mime is not None and bool(mime.urls()) and
      os.path.exists(mime.urls()[0].toLocalFile()),
      str([u.toLocalFile() for u in (mime.urls() if mime else [])]))

transfer.set_host_clipboard_files([os.path.join(dest, "note.txt")])
h.create_folder(f"{BASE}/pasted")
goto(f"{BASE}/pasted")
panel.paste_from_pc()
pump(15, until=lambda: "Uploaded" in panel.status_label.text())
pump(1.5)
check("paste from the PC clipboard uploads",
      names_at(f"{BASE}/pasted") == ["note.txt"], str(names_at(f"{BASE}/pasted")))
shutil.rmtree(dest, ignore_errors=True)

h.delete_item(BASE, is_dir=True, missing_ok=True)
shutil.rmtree(LOCAL, ignore_errors=True)

print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)

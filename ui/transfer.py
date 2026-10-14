"""Shared transfer helpers: conflict resolution, naming and host clipboard.

Keeping these out of DevicePanel lets paste, drag-and-drop, upload and the
device-to-device sender behave identically.

The rules every caller inherits:

* nothing at the destination is deleted until the replacement is in place
* a folder clash offers Merge (keep what is there) before Replace (wipe it)
* "Keep both" is checked against the real destination listing
* if the destination cannot be read, the transfer stops instead of guessing
"""

import os
import re
import shutil
import tempfile

from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout,
)

# What to do with one clashing item.
KEEP_BOTH = 'keep_both'
MERGE = 'merge'
REPLACE = 'replace'
SKIP = 'skip'
CANCEL = 'cancel'

# A drag has to finish fetching before it can start, so only a selection this
# small is prepared inline. Larger ones go through Download to PC.
try:
    DRAG_OUT_SIZE_LIMIT = int(os.environ.get('ADB_EXPLORER_DRAG_LIMIT_MB', 24)) * 1024 * 1024
except (TypeError, ValueError):
    DRAG_OUT_SIZE_LIMIT = 24 * 1024 * 1024


class ConflictDialog(QDialog):
    """Ask what to do about an item that already exists at the destination.

    Folders offer Merge as well, and say plainly what Replace throws away,
    because "Overwrite" on a folder used to mean deleting everything in it.
    """

    def __init__(self, parent, name, is_dir=False, remaining=0, dest_dir=None):
        super().__init__(parent)
        self.setWindowTitle("Item Already Exists")
        self.setMinimumWidth(460)
        self.choice = CANCEL
        self.is_dir = is_dir

        layout = QVBoxLayout(self)
        where = f" in {dest_dir}" if dest_dir else ""
        kind = "folder" if is_dir else "file"
        headline = QLabel(f"A {kind} named '{name}' already exists{where}.")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        choices = [
            ("Keep both", KEEP_BOTH, "Add a number to the new item's name"),
        ]
        if is_dir:
            choices.append(
                ("Merge", MERGE,
                 "Add the new contents to the existing folder, replacing only "
                 "files with the same name"))
            choices.append(
                ("Replace", REPLACE,
                 "Delete everything currently in the existing folder, then copy"))
        else:
            choices.append(("Replace", REPLACE, "Overwrite the existing file"))
        choices.append(("Skip", SKIP, "Leave the existing item alone"))

        if is_dir:
            warning = QLabel("Replace deletes the existing folder's current "
                             "contents. Merge keeps them.")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #b45309; font-size: 11px;")
            layout.addWidget(warning)

        self.apply_to_all = QCheckBox("Apply this to the remaining items")
        self.apply_to_all.setVisible(remaining > 0)
        layout.addWidget(self.apply_to_all)

        buttons = QHBoxLayout()
        for text, value, tip in choices:
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, v=value: self._choose(v))
            buttons.addWidget(button)
        layout.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _choose(self, value):
        self.choice = value
        self.accept()


class PlannedItem:
    """One source item and what should happen to it at the destination."""

    def __init__(self, source, name, dest_name, action=None, is_dir=False):
        self.source = source          # remote path, or local path for uploads
        self.name = name              # name at the source
        self.dest_name = dest_name    # name to use at the destination
        self.action = action          # None when there was no clash
        self.is_dir = is_dir

    @property
    def merging(self):
        return self.action == MERGE

    @property
    def replacing(self):
        return self.action == REPLACE

    def __repr__(self):
        return f"PlannedItem({self.name!r} -> {self.dest_name!r}, {self.action})"


def unique_name(name, taken):
    """Return a name not in `taken`, in the familiar "file (2).txt" style."""
    if name not in taken:
        return name

    stem, ext = os.path.splitext(name)
    # Reuse an existing counter rather than producing "file (2) (2).txt".
    match = re.match(r'^(.*) \((\d+)\)$', stem)
    if match:
        stem, start = match.group(1), int(match.group(2)) + 1
    else:
        start = 2

    for counter in range(start, start + 10000):
        candidate = f"{stem} ({counter}){ext}"
        if candidate not in taken:
            return candidate
    return f"{stem} ({os.getpid()}){ext}"


def destination_names(handler, dest_dir, probe_names):
    """Every name already in dest_dir, or None if that cannot be established.

    A full listing is preferred because "Keep both" must avoid names that are
    there but were not among the items being transferred. If the directory
    cannot be listed, fall back to testing just the names involved.
    """
    listing = handler.list_directory(dest_dir)
    if listing is not None:
        return {item.name for item in listing}

    existing = handler.paths_exist([f"{dest_dir.rstrip('/')}/{n}" for n in probe_names])
    if existing is None:
        return None
    return {n for n in probe_names if existing.get(f"{dest_dir.rstrip('/')}/{n}")}


def plan_transfer(parent, handler, dest_dir, sources, is_dir_of=None):
    """Decide what to do with each source item, asking the user about clashes.

    `sources` maps the name an item would take at the destination to the item
    itself (a remote path, or a local path for uploads). Returns a list of
    PlannedItem, or None if the user cancelled or the destination is unknown.
    """
    dest_dir = dest_dir.rstrip('/') or '/'
    names = list(sources)
    if not names:
        return []

    taken = destination_names(handler, dest_dir, names)
    if taken is None:
        QMessageBox.warning(
            parent, "Destination Unreadable",
            f"Could not check what is already in {dest_dir}, so nothing was "
            f"transferred.\n\n{handler.last_error or 'The device did not answer.'}")
        return None

    is_dir_of = is_dir_of or (lambda name: False)
    plan = []
    remembered = None
    clashes = [n for n in names if n in taken]

    for name in names:
        source = sources[name]
        item_is_dir = bool(is_dir_of(name))

        if name not in taken:
            plan.append(PlannedItem(source, name, name, None, item_is_dir))
            continue

        choice = remembered
        if choice is None:
            remaining = len([n for n in clashes[clashes.index(name) + 1:]])
            dialog = ConflictDialog(parent, name, is_dir=item_is_dir,
                                    remaining=remaining, dest_dir=dest_dir)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            choice = dialog.choice
            if dialog.apply_to_all.isChecked():
                remembered = choice

        if choice in (SKIP, CANCEL):
            if choice == CANCEL:
                return None
            continue

        if choice == KEEP_BOTH:
            new_name = unique_name(name, taken)
            taken.add(new_name)
            plan.append(PlannedItem(source, name, new_name, KEEP_BOTH, item_is_dir))
            continue

        if choice == MERGE and not item_is_dir:
            # Merging a file is just replacing it.
            choice = REPLACE
        plan.append(PlannedItem(source, name, name, choice, item_is_dir))

    return plan


def local_unique_path(directory, name):
    """Pick a free local filename, so a download never clobbers a file."""
    taken = set(os.listdir(directory)) if os.path.isdir(directory) else set()
    return os.path.join(directory, unique_name(name, taken))


def temp_transfer_dir(serial, clear=False):
    """A per-device scratch directory for clipboard and drag payloads."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', serial or 'device')
    path = os.path.join(tempfile.gettempdir(), 'adb_explorer', safe)
    if clear:
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def set_host_clipboard_files(local_paths):
    """Put real files on the system clipboard so Explorer/Finder can paste them."""
    if not local_paths:
        return False
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in local_paths])
    # Many file managers read the plain-text form instead of the URL list.
    mime.setText(os.linesep.join(local_paths))
    QGuiApplication.clipboard().setMimeData(mime)
    return True


def set_host_clipboard_text(text):
    QGuiApplication.clipboard().setText(text)


def host_clipboard_files():
    """Local files currently on the system clipboard, if any."""
    mime = QGuiApplication.clipboard().mimeData()
    if not mime or not mime.hasUrls():
        return []
    paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
    return [p for p in paths if p and os.path.exists(p)]

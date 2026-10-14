"""Shared transfer helpers: conflict resolution, naming and host clipboard.

Keeping these out of DevicePanel lets paste, drag-and-drop, upload and the
device-to-device sender all behave the same way.
"""

import os
import re
import tempfile

from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

OVERWRITE = 'overwrite'
SKIP = 'skip'
KEEP_BOTH = 'keep_both'
CANCEL = 'cancel'

# Sizes above this are not pre-fetched for a drag-out, because the pull has to
# finish before the drag can start and a multi-second pause is worse than
# telling the user to use "Download to PC". Roughly a second over USB 2.
DRAG_OUT_SIZE_LIMIT = int(os.environ.get('ADB_EXPLORER_DRAG_LIMIT_MB', 24)) * 1024 * 1024


class ConflictDialog(QDialog):
    """Ask what to do about an item that already exists at the destination."""

    def __init__(self, parent, name, remaining=0):
        super().__init__(parent)
        self.setWindowTitle("Item Already Exists")
        self.setMinimumWidth(420)
        self.choice = CANCEL

        layout = QVBoxLayout(self)
        label = QLabel(f"'{name}' already exists in the destination.")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.apply_to_all = QCheckBox("Apply this to the remaining items")
        self.apply_to_all.setVisible(remaining > 0)
        layout.addWidget(self.apply_to_all)

        buttons = QHBoxLayout()
        for text, value, tip in (
            ("Keep both", KEEP_BOTH, "Add a number to the new item's name"),
            ("Overwrite", OVERWRITE, "Replace the existing item"),
            ("Skip", SKIP, "Leave the existing item alone"),
        ):
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

    for counter in range(start, start + 1000):
        candidate = f"{stem} ({counter}){ext}"
        if candidate not in taken:
            return candidate
    return f"{stem} ({os.getpid()}){ext}"


def resolve_conflicts(parent, handler, dest_dir, names):
    """Map each source name to the destination name to use, asking if needed.

    Returns (plan, overwrites) where plan is a list of (name, dest_name) and
    overwrites holds the destination names that must be deleted first, or
    (None, None) if the user cancelled.
    """
    dest_dir = dest_dir.rstrip('/') or '/'
    existing = handler.paths_exist([f"{dest_dir}/{n}" for n in names])
    taken = {n for n in names if existing.get(f"{dest_dir}/{n}")}

    plan = []
    overwrites = []
    remembered = None

    for position, name in enumerate(names):
        if name not in taken:
            plan.append((name, name))
            continue

        choice = remembered
        if choice is None:
            dialog = ConflictDialog(parent, name, remaining=len(names) - position - 1)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None, None
            choice = dialog.choice
            if dialog.apply_to_all.isChecked():
                remembered = choice

        if choice == SKIP:
            continue
        if choice == CANCEL:
            return None, None
        if choice == KEEP_BOTH:
            new_name = unique_name(name, taken)
            taken.add(new_name)
            plan.append((name, new_name))
            continue

        overwrites.append(name)
        plan.append((name, name))

    return plan, overwrites


def local_unique_path(directory, name):
    """Pick a free local filename, so a download never clobbers a file."""
    taken = set(os.listdir(directory)) if os.path.isdir(directory) else set()
    return os.path.join(directory, unique_name(name, taken))


def temp_transfer_dir(serial):
    """A stable per-device scratch directory for clipboard and drag payloads."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', serial or 'device')
    path = os.path.join(tempfile.gettempdir(), 'adb_explorer', safe)
    os.makedirs(path, exist_ok=True)
    return path


def set_host_clipboard_files(local_paths):
    """Put real files on the system clipboard so Explorer/Finder can paste them."""
    if not local_paths:
        return False
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in local_paths])
    # Many file managers read the plain-text form instead of the URL list.
    mime.setText("\n".join(local_paths))
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

"""Copy, move and transfer actions for a device panel.

Everything here is mixed into DevicePanel. It is kept separate so the panel
file stays about the view, and so paste, drag-and-drop, the clipboard and the
device-to-device sender share one set of rules:

* conflicts are resolved before anything is written (see ui/transfer.py)
* an existing item is only removed once its replacement is in place
* every long operation reports progress, can be cancelled, and reports what
  actually happened - including partial failure
"""

import os
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QDialog, QFileDialog, QMessageBox

from ui import transfer


class TransferContext(QObject):
    """Progress channel and cancel switch shared by a worker and its dialog."""

    message = pyqtSignal(str)

    def __init__(self, handlers=()):
        super().__init__()
        self.cancelled = False
        # Handlers whose adb child may be killed on cancel. Only the handlers
        # taking part in this transfer are listed.
        self.handlers = [h for h in handlers if h is not None]

    def cancel(self):
        self.cancelled = True
        for handler in self.handlers:
            try:
                handler.kill_active()
            except Exception:
                pass

    def is_cancelled(self):
        return self.cancelled

    def say(self, text):
        if not self.cancelled:
            self.message.emit(text)


class TransferOutcome:
    """What a transfer worker actually managed to do."""

    def __init__(self, total=0):
        self.total = total
        self.done = 0
        self.skipped = 0
        self.failed = []
        self.cancelled = False
        self.error = None

    def record(self, name, ok, error=None):
        if ok:
            self.done += 1
        else:
            self.failed.append(name)
            self.error = self.error or error

    @property
    def partial(self):
        return bool(self.failed) or (self.done + self.skipped) < self.total

    def __bool__(self):
        # Falsy only when nothing at all got through, which routes the modal
        # dialog to its error handler.
        return self.done > 0


class TransferActionsMixin:

    # ----------------------------------------------------------------- helpers

    def _dest_dir(self):
        return self.current_path.rstrip('/') or '/'

    def _remote_path(self, name, directory=None):
        return f"{(directory or self._dest_dir()).rstrip('/')}/{name}"

    def selected_rows(self):
        """The rows the user has selected, in whichever view is showing."""
        view = self.tree_view
        getter = getattr(view, 'selected_rows', None)
        if callable(getter):
            return getter()
        return view.selectionModel().selectedRows()

    def _selected_names(self):
        names = []
        for index in self.selected_rows():
            name = self.tree_model.data(index.siblingAtColumn(0))
            if name and name != "..":
                names.append(name)
        return names

    def _note(self, message):
        """Set the status line, tolerating a panel that is going away."""
        if getattr(self, '_closing', False):
            return
        try:
            self.status_label.setText(message)
            self.status_label.setToolTip("")
        except RuntimeError:
            pass

    def _note_and_refresh(self, message):
        """Report an outcome that survives the refresh that follows it."""
        if getattr(self, '_closing', False):
            return
        self._post_refresh_note = message
        self._note(message)
        try:
            self.refresh_files()
        except RuntimeError:
            pass

    def _report_outcome(self, outcome, verb, refresh=True):
        """Say what happened, including when only some items made it."""
        if outcome.cancelled:
            message = f"Canceled - {verb.lower()} {outcome.done} of {outcome.total}"
        elif outcome.partial:
            detail = outcome.error or self.adb_handler.last_error or "see the log"
            message = (f"{verb} {outcome.done} of {outcome.total}, "
                       f"{len(outcome.failed)} failed: {detail.splitlines()[0][:120]}")
        elif outcome.skipped and not outcome.done:
            message = "Nothing to do - everything was skipped"
        else:
            message = f"{verb} {outcome.done} item(s)"
        if refresh:
            self._note_and_refresh(message)
        else:
            self._note(message)

    def _confirm(self, title, question):
        return QMessageBox.question(
            self, title, question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _connected(self):
        if self.adb_handler.device_connected:
            return True
        self._note("No ADB device")
        return False

    @staticmethod
    def _backup_path(path):
        return f"{path.rstrip('/')}.adbx-replacing-{os.getpid()}"

    def _replace_safely(self, handler, dest_path, is_dir, write):
        """Put the new item in place before removing the old one.

        The existing item is renamed aside, the replacement is written, and
        only then is the old copy deleted. If the write fails the old copy is
        put back, so a replace can never leave the destination empty.
        """
        backup = self._backup_path(dest_path)
        if not handler.rename_item(dest_path, backup):
            return False, handler.last_error

        ok = False
        error = None
        try:
            ok = write()
            error = None if ok else handler.last_error
        finally:
            if ok:
                handler.delete_item(backup, is_dir=is_dir, missing_ok=True)
            else:
                handler.delete_item(dest_path, is_dir=is_dir, missing_ok=True)
                handler.rename_item(backup, dest_path)
        return ok, error

    # --------------------------------------------------------- device clipboard

    def copy_selected(self):
        items = self.get_selected_items()
        if not items:
            return
        self.clipboard = {'items': items, 'operation': 'copy'}
        self._clipboard_time = time.monotonic()
        self._note(f"Copied {len(items)} item(s) - paste into any panel")

    def cut_selected(self):
        items = self.get_selected_items()
        if not items:
            return
        self.clipboard = {'items': items, 'operation': 'cut'}
        self._clipboard_time = time.monotonic()
        self._note(f"Cut {len(items)} item(s) - paste into any panel")

    def _clear_clipboard(self):
        self.clipboard = {'items': [], 'operation': None}
        self._clipboard_time = 0.0

    def _panel_clipboard(self):
        """The most recent device clipboard across all panels.

        Copying in one panel and pasting in another is the obvious
        expectation, so the clipboard is shared rather than per panel.
        """
        best, best_time, best_panel = None, 0.0, self
        candidates = [self] + [p for p in getattr(self.window(), 'device_panels', [])
                               if p is not self]
        for panel in candidates:
            clip = getattr(panel, 'clipboard', None) or {}
            stamp = getattr(panel, '_clipboard_time', 0.0)
            if clip.get('items') and stamp > best_time:
                best, best_time, best_panel = clip, stamp, panel
        return best, best_time, best_panel

    def paste_items(self):
        """Paste whatever was copied last: device items or files from the PC."""
        clip, clip_time, source_panel = self._panel_clipboard()
        host_files = transfer.host_clipboard_files()

        if host_files and self._host_clipboard_time >= clip_time:
            self.upload_paths(host_files)
            return

        if not clip:
            if host_files:
                self.upload_paths(host_files)
            else:
                self._note("Clipboard is empty")
            return

        items = clip['items']
        move = clip.get('operation') == 'cut'
        clear = source_panel._clear_clipboard if move else None

        if source_panel.device_serial != self.device_serial:
            source_panel.send_items_to_device(items, self, move=move,
                                              dest_dir=self._dest_dir(),
                                              on_success=clear)
            return

        self.move_or_copy_items([i['path'] for i in items], self._dest_dir(),
                                move=move, on_success=clear)

    def paste_from_pc(self):
        files = transfer.host_clipboard_files()
        if not files:
            self._note("No files on the PC clipboard")
            return
        self.upload_paths(files)

    def copy_to_pc_clipboard(self):
        """Pull the selection to a temp folder and put it on the PC clipboard."""
        names = self._selected_names()
        if not names or not self._connected():
            return

        target = transfer.temp_transfer_dir(self.device_serial, clear=True)
        remote = [self._remote_path(n) for n in names]
        ctx = TransferContext([self.adb_handler])

        def run():
            outcome = TransferOutcome(len(names))
            ok = self.adb_handler.pull_items(remote, target, line_callback=ctx.say)
            outcome.done = len(names) if ok else 0
            if not ok:
                outcome.failed = list(names)
                outcome.error = self.adb_handler.last_error
            outcome.cancelled = ctx.cancelled
            return outcome

        def on_done(outcome):
            local = [p for p in (os.path.join(target, n) for n in names) if os.path.exists(p)]
            if local and transfer.set_host_clipboard_files(local):
                self._note(f"{len(local)} item(s) on the PC clipboard - paste in Explorer")
            else:
                self._report_outcome(outcome, "Copied", refresh=False)

        self._run_modal(f"Copying {len(names)} item(s) to the PC clipboard", run,
                        on_done=on_done,
                        on_error=lambda: self._report_error("Copy failed"), ctx=ctx)

    def copy_paths_to_clipboard(self):
        names = self._selected_names()
        paths = [self._remote_path(n) for n in names] or [self._dest_dir()]
        transfer.set_host_clipboard_text(os.linesep.join(paths))
        self._note(f"Copied {len(paths)} path(s) as text")

    # ----------------------------------------------------------- on one device

    def move_or_copy_items(self, paths, dest_dir, move=False, on_success=None):
        """Copy or move items inside one device, resolving conflicts first."""
        paths = [p for p in dict.fromkeys(p for p in paths if p)]
        if not paths or not self._connected():
            return
        dest_dir = dest_dir.rstrip('/') or '/'

        for path in paths:
            source = path.rstrip('/')
            if dest_dir == source or dest_dir.startswith(source + '/'):
                QMessageBox.warning(self, "Invalid Destination",
                                    f"'{source.split('/')[-1]}' cannot be "
                                    f"{'moved' if move else 'copied'} into itself.")
                return

        # Items already sitting in the destination need no dialog: a move is a
        # no-op and a copy is a duplicate, which is what Keep both means.
        in_place = [p for p in paths
                    if '/'.join(p.rstrip('/').split('/')[:-1]) == dest_dir]
        elsewhere = [p for p in paths if p not in in_place]

        if in_place and move:
            self._note(f"{len(in_place)} item(s) are already here")
            if not elsewhere:
                return

        if in_place and not move:
            self.duplicate_names([p.rstrip('/').split('/')[-1] for p in in_place])
            if not elsewhere:
                return

        sources = {p.rstrip('/').split('/')[-1]: p for p in elsewhere}
        plan = transfer.plan_transfer(self, self.adb_handler, dest_dir, sources,
                                      is_dir_of=self._is_dir_of_name)
        if plan is None:
            self._note("Canceled")
            return
        if not plan:
            self._note("Nothing to do - everything was skipped")
            return

        verb = "Moving" if move else "Copying"
        ctx = TransferContext([self.adb_handler])
        handler = self.adb_handler
        total = len(plan)
        # The shared parent is created once instead of once per item.
        handler.create_folder(dest_dir)

        def run():
            outcome = TransferOutcome(total)
            for item in plan:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                dst = f"{dest_dir}/{item.dest_name}"
                ctx.say(f"{verb} {item.name}")

                if item.merging:
                    ok = handler.merge_into_folder(item.source, dst)
                    if ok and move:
                        ok = handler.delete_item(item.source, is_dir=True)
                    error = None if ok else handler.last_error
                elif item.replacing:
                    def write(source=item.source, target=dst):
                        return (handler.move_on_device(source, target, ensure_parent=False)
                                if move else
                                handler.copy_on_device(source, target, ensure_parent=False))
                    ok, error = self._replace_safely(handler, dst, item.is_dir, write)
                else:
                    ok = (handler.move_on_device(item.source, dst, ensure_parent=False)
                          if move else
                          handler.copy_on_device(item.source, dst, ensure_parent=False))
                    error = None if ok else handler.last_error

                outcome.record(item.name, ok, error)
            return outcome

        def on_done(outcome):
            if on_success and outcome.done and not outcome.partial:
                on_success()
            self._report_outcome(outcome, "Moved" if move else "Copied")

        self._run_modal(f"{verb} {total} item(s)", run, on_done=on_done,
                        on_error=lambda: self._report_error(f"{verb} failed"), ctx=ctx)

    def copy_selected_to(self):
        self._transfer_selected_to_folder(move=False)

    def move_selected_to(self):
        self._transfer_selected_to_folder(move=True)

    def _transfer_selected_to_folder(self, move):
        """Copy or move the selection to another folder on the same device."""
        names = self._selected_names()
        if not names or not self._connected():
            return

        from select_directory_dialog import SelectDirectoryDialog
        dialog = SelectDirectoryDialog(
            self, self.adb_handler, self.current_path, self.root_path, self.use_root)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target = dialog.get_selected_path()
        if not target:
            return

        verb = "Move" if move else "Copy"
        if not self._confirm(verb, f"{verb} {len(names)} item(s) to '{target}'?"):
            return
        self.move_or_copy_items([self._remote_path(n) for n in names], target, move=move)

    def duplicate_selected(self):
        self.duplicate_names(self._selected_names())

    def duplicate_names(self, names):
        """Make a copy beside each original, named like "file (2).txt"."""
        names = [n for n in names if n]
        if not names or not self._connected():
            return

        listing = self.adb_handler.list_directory(self._dest_dir())
        if listing is None:
            self._report_error("Could not read this folder")
            return

        existing = {item.name for item in listing}
        dir_names = {item.name for item in listing if item.is_dir}
        pairs = []
        for name in names:
            new_name = transfer.unique_name(name, existing)
            existing.add(new_name)
            pairs.append((name, new_name))

        ctx = TransferContext([self.adb_handler])
        handler = self.adb_handler

        def run():
            outcome = TransferOutcome(len(pairs))
            for name, new_name in pairs:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                ctx.say(f"Duplicating {name}")
                ok = handler.copy_on_device(self._remote_path(name),
                                            self._remote_path(new_name),
                                            ensure_parent=False)
                outcome.record(name, ok, handler.last_error)
            return outcome

        self._run_modal(f"Duplicating {len(pairs)} item(s)", run,
                        on_done=lambda outcome: self._report_outcome(outcome, "Duplicated"),
                        on_error=lambda: self._report_error("Duplicate failed"), ctx=ctx)

    def _is_dir_of_name(self, name):
        for item in (self.all_files or []):
            if item.name == name:
                return item.is_dir
        return False

    # ------------------------------------------------------------ PC to device

    def upload_paths(self, local_paths, sync=False, dest_dir=None):
        """Upload files and folders from the PC into a folder on the device."""
        local_paths = [p for p in dict.fromkeys(local_paths) if p and os.path.exists(p)]
        if not local_paths or not self._connected():
            return

        dest_dir = (dest_dir or self._dest_dir()).rstrip('/') or '/'
        if sync:
            self._sync_paths(local_paths, dest_dir)
            return

        # Two sources can share a basename (two Explorer folders, or a search
        # result), so each source is planned separately and later ones are
        # renamed rather than dropped.
        names = []
        used = set()
        for path in local_paths:
            name = os.path.basename(path.rstrip('/' + os.sep)) or path
            if name in used:
                name = transfer.unique_name(name, used)
            used.add(name)
            names.append(name)

        sources = dict(zip(names, local_paths))
        plan = transfer.plan_transfer(
            self, self.adb_handler, dest_dir, sources,
            is_dir_of=lambda name: os.path.isdir(sources[name]))
        if plan is None:
            self._note("Upload canceled")
            return
        if not plan:
            self._note("Nothing to upload - everything was skipped")
            return

        # Only items that keep their own basename can share one adb push,
        # because a batched push names each file after its source.
        def keeps_name(item):
            return item.action is None and item.dest_name == os.path.basename(
                item.source.rstrip('/' + os.sep))

        batch = [item.source for item in plan if keeps_name(item)]
        individual = [item for item in plan if not keeps_name(item)]
        ctx = TransferContext([self.adb_handler])
        handler = self.adb_handler
        total = len(plan)

        def run():
            outcome = TransferOutcome(total)
            if batch:
                ctx.say(f"Uploading {len(batch)} item(s)...")
                ok = handler.push_items(batch, dest_dir, line_callback=ctx.say)
                error = None if ok else handler.last_error
                for source in batch:
                    outcome.record(os.path.basename(source), ok, error)

            for item in individual:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                dst = f"{dest_dir}/{item.dest_name}"
                ctx.say(f"Uploading {item.dest_name}")

                if item.merging:
                    # Push the folder's children so what is already there survives.
                    children = [os.path.join(item.source, c) for c in os.listdir(item.source)]
                    ok = handler.push_items(children, dst, line_callback=ctx.say) if children \
                        else handler.create_folder(dst)
                    error = None if ok else handler.last_error
                elif item.replacing and item.is_dir:
                    def write(source=item.source, target=dst):
                        return handler.push_file(source, target)
                    ok, error = self._replace_safely(handler, dst, True, write)
                else:
                    # adb push overwrites a file in place, so a replace needs
                    # no delete; keep-both just pushes under the new name.
                    ok = handler.push_file(item.source, dst)
                    error = None if ok else handler.last_error

                outcome.record(item.dest_name, ok, error)
            return outcome

        self._run_modal(f"Uploading {total} item(s)", run,
                        on_done=lambda outcome: self._report_outcome(outcome, "Uploaded"),
                        on_error=lambda: self._report_error("Upload failed"), ctx=ctx)

    def _sync_paths(self, local_paths, dest_dir):
        """Upload only what changed, merging into whatever is already there.

        Sync deliberately skips the conflict dialog: every answer it offers
        would defeat the point, since the folder existing is the normal case.
        """
        # Sync mirrors by name, so two sources with the same name would
        # overwrite one another silently.
        seen = {}
        for source in local_paths:
            name = os.path.basename(source.rstrip('/' + os.sep)) or source
            if name in seen:
                QMessageBox.warning(
                    self, "Duplicate Name",
                    f"Two selected items are both named '{name}'. Sync mirrors "
                    f"by name, so sync them one at a time.")
                return
            seen[name] = source

        ctx = TransferContext([self.adb_handler])
        handler = self.adb_handler
        total = len(local_paths)

        def run():
            outcome = TransferOutcome(total)
            for source in local_paths:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                name = os.path.basename(source.rstrip('/' + os.sep)) or source
                ctx.say(f"Syncing {name}")
                target = f"{dest_dir}/{name}"
                existing = handler.paths_exist([target])

                if os.path.isdir(source) and existing and existing.get(target):
                    children = [os.path.join(source, c) for c in os.listdir(source)]
                    ok = handler.push_items(children, target,
                                            line_callback=ctx.say, sync=True) \
                        if children else True
                else:
                    ok = handler.push_items([source], dest_dir,
                                            line_callback=ctx.say, sync=True)
                outcome.record(name, ok, handler.last_error)
            return outcome

        self._run_modal(f"Syncing {total} item(s)", run,
                        on_done=lambda outcome: self._report_outcome(outcome, "Synced"),
                        on_error=lambda: self._report_error("Sync failed"), ctx=ctx)

    def upload_file_to_device(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files to Upload")
        if files:
            self.upload_paths(files)

    def upload_folder_to_device(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Upload")
        if folder:
            self.upload_paths([folder])

    def sync_folder_to_device(self):
        """Upload a folder but skip files the device already has."""
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Sync")
        if folder:
            self.upload_paths([folder], sync=True)

    def handle_drop_event(self, event, dest_dir=None):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.upload_paths(paths, dest_dir=dest_dir)

    # ------------------------------------------------------------ device to PC

    def download_selected_items(self, dest_dir=None):
        names = self._selected_names()
        if not names or not self._connected():
            return

        # Anything that is not a real path means "ask", including the bool a
        # button's clicked() signal would otherwise pass in here.
        if not isinstance(dest_dir, str) or not dest_dir:
            dest_dir = QFileDialog.getExistingDirectory(
                self, "Download Location", self._last_download_dir or "")
            if not dest_dir:
                return
        self._last_download_dir = dest_dir

        batch, renamed = [], []
        remembered = None
        clashes = [n for n in names if os.path.exists(os.path.join(dest_dir, n))]
        for name in names:
            local = os.path.join(dest_dir, name)
            if not os.path.exists(local):
                batch.append(name)
                continue

            choice = remembered
            if choice is None:
                remaining = len(clashes) - clashes.index(name) - 1
                dialog = transfer.ConflictDialog(
                    self, name, is_dir=os.path.isdir(local),
                    remaining=remaining, dest_dir=dest_dir)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    self._note("Download canceled")
                    return
                choice = dialog.choice
                if dialog.apply_to_all.isChecked():
                    remembered = choice

            if choice in (transfer.SKIP, transfer.CANCEL):
                if choice == transfer.CANCEL:
                    self._note("Download canceled")
                    return
                continue
            if choice == transfer.KEEP_BOTH:
                renamed.append((name, transfer.local_unique_path(dest_dir, name)))
            else:
                # Replace and Merge both let adb pull write over what is there;
                # pull never deletes local files it is not replacing.
                batch.append(name)

        total = len(batch) + len(renamed)
        if not total:
            self._note("Nothing to download - everything was skipped")
            return

        ctx = TransferContext([self.adb_handler])
        handler = self.adb_handler

        def run():
            outcome = TransferOutcome(total)
            if batch:
                ok = handler.pull_items([self._remote_path(n) for n in batch],
                                        dest_dir, line_callback=ctx.say)
                error = None if ok else handler.last_error
                for name in batch:
                    outcome.record(name, ok, error)
            for name, local in renamed:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                ctx.say(f"Downloading {name}")
                ok = handler.pull_file(self._remote_path(name), local)
                outcome.record(name, ok, handler.last_error)
            return outcome

        self._run_modal(f"Downloading {total} item(s)", run,
                        on_done=lambda outcome: self._report_outcome(
                            outcome, f"Downloaded to {dest_dir} -", refresh=False),
                        on_error=lambda: self._report_error("Download failed"), ctx=ctx)

    def is_dir_for_path(self, path):
        return self._is_dir_of_name(path.rstrip('/').split('/')[-1])

    def selection_size(self, timeout=20):
        """Bytes in the current selection, or None if a folder cannot be measured."""
        total = 0
        folders = []
        for index in self.selected_rows():
            name = self.tree_model.data(index.siblingAtColumn(0))
            if not name or name == "..":
                continue
            if self.is_dir_from_row(index.row()):
                folders.append(self._remote_path(name))
            else:
                total += self.get_file_size_from_row(index.row())
        if folders:
            measured = self.adb_handler.disk_usage_many(folders, timeout=timeout)
            if measured is None:
                return None
            total += measured
        return total

    def drag_out_payload(self):
        """Fetch the selection so the drag can also be dropped on the PC.

        This runs as a drag begins, so it must be quick and silent: the files
        have to be on disk before the drag starts. A selection small enough is
        fetched, and anything larger simply drags within the app while the
        status bar says which command to use instead.
        """
        names = self._selected_names()
        if not names or not self.adb_handler.device_connected:
            return None

        size = self.selection_size(timeout=5)
        limit_mb = transfer.DRAG_OUT_SIZE_LIMIT // (1024 * 1024)
        if size is None or size > transfer.DRAG_OUT_SIZE_LIMIT:
            measured = "too large" if size else "an unknown size"
            self._note(f"Dragging to the PC is limited to {limit_mb} MB and this "
                       f"selection is {measured} - use \u2193 To PC (Ctrl+E)")
            return None

        target = transfer.temp_transfer_dir(self.device_serial, clear=True)
        if not self.adb_handler.pull_items([self._remote_path(n) for n in names], target):
            self._report_error("Could not prepare the drag")
            return None

        local = [p for p in (os.path.join(target, n) for n in names) if os.path.exists(p)]
        if local:
            self._note(f"Dragging {len(local)} item(s) - drop them in a panel, "
                       f"or anywhere on the PC")
        return local

    def hint_drag_to_pc(self):
        """Kept for callers that only want the tip, without fetching anything."""
        if getattr(self, '_drag_hint_shown', False):
            return
        self._drag_hint_shown = True
        limit_mb = transfer.DRAG_OUT_SIZE_LIMIT // (1024 * 1024)
        self._note(f"Drag items anywhere on the PC (up to {limit_mb} MB), or use "
                   f"\u2193 To PC for more")

    # -------------------------------------------------------- device to device

    def send_items_to_device(self, items, target_panel, move=False, dest_dir=None,
                             on_success=None):
        """Stream items to another device, honouring the drop target."""
        if not items:
            return
        if not self.adb_handler.device_connected or not target_panel.adb_handler.device_connected:
            self._note("No ADB device")
            return

        dest_dir = (dest_dir or target_panel.current_path).rstrip('/') or '/'
        sources = {i['name']: i for i in items}
        kinds = {i['name']: i.get('is_dir', False) for i in items}
        plan = transfer.plan_transfer(target_panel, target_panel.adb_handler, dest_dir,
                                      sources, is_dir_of=lambda name: kinds.get(name, False))
        if plan is None:
            self._note("Canceled")
            return
        if not plan:
            self._note("Nothing to send - everything was skipped")
            return

        verb = "Moving" if move else "Sending"
        ctx = TransferContext([self.adb_handler, target_panel.adb_handler])
        source_handler = self.adb_handler
        target_handler = target_panel.adb_handler
        target_name = target_panel.device_name
        total = len(plan)

        def stream(item, dest):
            if item.is_dir:
                return source_handler.stream_directory(
                    self.device_serial, item.source['path'],
                    target_panel.device_serial, dest,
                    line_callback=ctx.say, cancel_check=ctx.is_cancelled)
            return source_handler.stream_file(
                self.device_serial, item.source['path'],
                target_panel.device_serial, dest,
                cancel_check=ctx.is_cancelled)

        def run():
            outcome = TransferOutcome(total)
            for item in plan:
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
                dest = f"{dest_dir}/{item.dest_name}"
                ctx.say(f"{verb} {item.name}")

                if item.replacing:
                    ok, error = self._replace_safely(
                        target_handler, dest, item.is_dir,
                        lambda i=item, d=dest: stream(i, d))
                else:
                    # Merge and a plain send are the same stream: files with
                    # the same name are replaced, the rest are left alone.
                    ok = stream(item, dest)
                    error = None if ok else source_handler.last_error

                if ok and move:
                    # delete_item without missing_ok fails loudly if the
                    # source could not actually be removed.
                    ok = source_handler.delete_item(item.source['path'],
                                                    is_dir=item.is_dir)
                    error = None if ok else source_handler.last_error

                outcome.record(item.name, ok, error)
                if ctx.cancelled:
                    outcome.cancelled = True
                    break
            return outcome

        def on_done(outcome):
            if on_success and outcome.done and not outcome.partial:
                on_success()
            try:
                if not getattr(target_panel, '_closing', False):
                    target_panel._note_and_refresh(
                        f"Received {outcome.done} item(s) from {self.device_name}")
            except RuntimeError:
                pass
            self._report_outcome(outcome, "Moved" if move else "Sent", refresh=move)

        self._run_modal(f"{verb} {total} item(s) to {target_name}", run, on_done=on_done,
                        on_error=lambda: self._report_error("Transfer failed"), ctx=ctx)

    def _other_panels(self):
        return [p for p in getattr(self.window(), 'device_panels', [])
                if p is not self and p.adb_handler.device_connected]

    def _populate_send_to_menu(self, menu, indexes, position):
        items = self.get_selected_items() if indexes else []
        if not items:
            menu.addAction("No items selected").setEnabled(False)
            return

        targets = self._other_panels()
        if not targets:
            menu.addAction("No other devices").setEnabled(False)
            return

        for panel in targets:
            action = menu.addAction(f"Copy to {panel.device_name}")
            action.triggered.connect(
                lambda checked=False, p=panel, its=items: self.send_items_to_device(its, p))
        menu.addSeparator()
        for panel in targets:
            action = menu.addAction(f"Move to {panel.device_name}")
            action.triggered.connect(
                lambda checked=False, p=panel, its=items: self._confirm_move_to(its, p))

    def _confirm_move_to(self, items, panel):
        if self._confirm("Move to Device",
                         f"Move {len(items)} item(s) to {panel.device_name}?\n\n"
                         f"They will be deleted from {self.device_name} afterwards."):
            self.send_items_to_device(items, panel, move=True)

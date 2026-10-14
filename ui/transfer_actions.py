"""Copy, move and transfer actions for a device panel.

Everything here is mixed into DevicePanel. It is kept separate so the panel
file stays about the view, and so paste, drag-and-drop, the clipboard and the
device-to-device sender all share one set of rules:

* conflicts are always resolved up front, with keep-both / overwrite / skip
* transfers go through as few adb invocations as possible
* every long operation reports progress and stays cancellable
"""

import os
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from ui import transfer


class ProgressRelay(QObject):
    """Carries progress lines from a worker thread to the dialog label."""
    message = pyqtSignal(str)


class TransferActionsMixin:

    # ----------------------------------------------------------------- helpers

    def _dest_dir(self):
        return self.current_path.rstrip('/') or '/'

    def _remote_path(self, name, directory=None):
        return f"{(directory or self._dest_dir()).rstrip('/')}/{name}"

    def _selected_names(self):
        names = []
        for index in self.tree_view.selectionModel().selectedRows():
            name = self.tree_model.data(index.siblingAtColumn(0))
            if name and name != "..":
                names.append(name)
        return names

    def _note(self, message):
        self.status_label.setText(message)
        self.status_label.setToolTip("")

    def _note_and_refresh(self, message):
        """Report an outcome that survives the refresh that follows it."""
        self._post_refresh_note = message
        self.status_label.setText(message)
        self.status_label.setToolTip("")
        self.refresh_files()

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

    def _panel_clipboard(self):
        """The most recent device clipboard across all panels.

        Copying in one panel and pasting in another is the obvious expectation,
        so the clipboard is shared rather than per panel.
        """
        best, best_time, best_panel = self.clipboard, self._clipboard_time, self
        window = self.window()
        for panel in getattr(window, 'device_panels', []):
            if panel is self:
                continue
            stamp = getattr(panel, '_clipboard_time', 0)
            items = getattr(panel, 'clipboard', None) or {}
            if items.get('items') and stamp > best_time:
                best, best_time, best_panel = items, stamp, panel
        return best, best_time, best_panel

    def paste_items(self):
        """Paste whatever was copied last: device items or files from the PC."""
        clip, clip_time, source_panel = self._panel_clipboard()
        host_files = transfer.host_clipboard_files()

        if host_files and self._host_clipboard_time >= clip_time:
            self.upload_paths(host_files)
            return

        if not clip or not clip.get('items'):
            if host_files:
                self.upload_paths(host_files)
            else:
                self._note("Clipboard is empty")
            return

        items = clip['items']
        move = clip.get('operation') == 'cut'

        if source_panel is not self and source_panel.device_serial != self.device_serial:
            self.receive_items_from(source_panel, items, move=move)
            return

        def clear_clipboard():
            if move:
                source_panel.clipboard = {'items': [], 'operation': None}

        self.move_or_copy_items([i['path'] for i in items], self._dest_dir(),
                                move=move, on_success=clear_clipboard)

    def paste_from_pc(self):
        files = transfer.host_clipboard_files()
        if not files:
            self._note("No files on the PC clipboard")
            return
        self.upload_paths(files)

    def copy_to_pc_clipboard(self):
        """Pull the selection to a temp folder and put it on the PC clipboard."""
        names = self._selected_names()
        if not names:
            return

        target = transfer.temp_transfer_dir(self.device_serial)
        remote = [self._remote_path(n) for n in names]
        relay = ProgressRelay()

        def run():
            return self.adb_handler.pull_items(remote, target, line_callback=relay.message.emit)

        def on_done(_):
            local = [os.path.join(target, n) for n in names]
            local = [p for p in local if os.path.exists(p)]
            if transfer.set_host_clipboard_files(local):
                self._note(f"{len(local)} item(s) on the PC clipboard - paste in Explorer")
            else:
                self._note("Nothing to copy")

        self._run_modal(f"Copying {len(names)} item(s) to the PC clipboard", run,
                        on_done=on_done, on_error=lambda: self._report_error("Copy failed"),
                        relay=relay)

    def copy_paths_to_clipboard(self):
        names = self._selected_names()
        paths = [self._remote_path(n) for n in names] or [self._dest_dir()]
        transfer.set_host_clipboard_text(os.linesep.join(paths))
        self._note(f"Copied {len(paths)} path(s) as text")

    # ----------------------------------------------------------- on one device

    def move_or_copy_items(self, paths, dest_dir, move=False, on_success=None):
        """Copy or move items inside one device, resolving conflicts first."""
        paths = [p for p in paths if p]
        if not paths:
            return
        dest_dir = dest_dir.rstrip('/') or '/'

        for path in paths:
            if dest_dir == path.rstrip('/') or dest_dir.startswith(path.rstrip('/') + '/'):
                QMessageBox.warning(self, "Invalid Destination",
                                    "A folder cannot be copied into itself.")
                return

        by_name = {p.rstrip('/').split('/')[-1]: p for p in paths}
        plan, overwrites = transfer.resolve_conflicts(
            self, self.adb_handler, dest_dir, list(by_name))
        if plan is None:
            self._note("Canceled")
            return
        if not plan:
            self._note("Nothing to do - all items skipped")
            return

        verb = "Moving" if move else "Copying"
        relay = ProgressRelay()

        def run():
            done = 0
            for name, dest_name in plan:
                src = by_name[name]
                dst = f"{dest_dir}/{dest_name}"
                relay.message.emit(f"{verb} {name}")
                if name in overwrites:
                    self.adb_handler.delete_item(dst, is_dir=True)
                ok = (self.adb_handler.move_on_device(src, dst) if move
                      else self.adb_handler.copy_on_device(src, dst))
                if ok:
                    done += 1
            return done

        def on_done(count):
            if on_success:
                on_success()
            self._note_and_refresh(f"{'Moved' if move else 'Copied'} {count} item(s)")

        self._run_modal(f"{verb} {len(plan)} item(s)", run, on_done=on_done,
                        on_error=lambda: self._report_error(f"{verb} failed"), relay=relay)

    def copy_selected_to(self):
        self._transfer_selected_to_folder(move=False)

    def move_selected_to(self):
        self._transfer_selected_to_folder(move=True)

    def _transfer_selected_to_folder(self, move):
        """Copy or move the selection to another folder on the same device."""
        names = self._selected_names()
        if not names:
            return

        from select_directory_dialog import SelectDirectoryDialog
        dialog = SelectDirectoryDialog(
            self, self.adb_handler, self.current_path, self.root_path, self.use_root)
        if dialog.exec() != transfer.QDialog.DialogCode.Accepted:
            return

        target = dialog.get_selected_path()
        if not target:
            return
        self.move_or_copy_items([self._remote_path(n) for n in names], target, move=move)

    def duplicate_selected(self):
        """Make a copy beside the original, named like "file (2).txt"."""
        names = self._selected_names()
        if not names:
            return

        existing = {f.name for f in (self.all_files or [])} | set(names)
        pairs = []
        for name in names:
            new_name = transfer.unique_name(name, existing)
            existing.add(new_name)
            pairs.append((name, new_name))

        relay = ProgressRelay()

        def run():
            done = 0
            for name, new_name in pairs:
                relay.message.emit(f"Duplicating {name}")
                if self.adb_handler.copy_on_device(self._remote_path(name),
                                                   self._remote_path(new_name)):
                    done += 1
            return done

        self._run_modal(f"Duplicating {len(pairs)} item(s)", run,
                        on_done=lambda count: self._note_and_refresh(f"Duplicated {count} item(s)"),
                        on_error=lambda: self._report_error("Duplicate failed"), relay=relay)

    # ------------------------------------------------------------ PC to device

    def upload_paths(self, local_paths, sync=False):
        """Upload files and folders from the PC into the current folder."""
        local_paths = [p for p in local_paths if p and os.path.exists(p)]
        if not local_paths:
            return
        if not self.adb_handler.device_connected:
            self._note("No ADB device")
            return

        dest_dir = self._dest_dir()
        by_name = {}
        for path in local_paths:
            by_name[os.path.basename(path.rstrip('/' + os.sep))] = path

        plan, overwrites = transfer.resolve_conflicts(
            self, self.adb_handler, dest_dir, list(by_name))
        if plan is None:
            self._note("Upload canceled")
            return
        if not plan:
            self._note("Nothing to upload - all items skipped")
            return

        # Items keeping their name go up in one adb push; renamed ones need
        # their own call because adb can only rename a single source.
        batch = [by_name[name] for name, dest_name in plan if name == dest_name]
        renamed = [(by_name[name], dest_name) for name, dest_name in plan if name != dest_name]
        relay = ProgressRelay()

        def run():
            for name in overwrites:
                self.adb_handler.delete_item(f"{dest_dir}/{name}", is_dir=True)

            ok = True
            if batch:
                relay.message.emit(f"Uploading {len(batch)} item(s)...")
                ok = self.adb_handler.push_items(batch, dest_dir,
                                                 line_callback=relay.message.emit, sync=sync)
            for src, dest_name in renamed:
                relay.message.emit(f"Uploading {dest_name}")
                ok = self.adb_handler.push_file(src, f"{dest_dir}/{dest_name}") and ok
            return ok

        def on_done(_):
            self._note_and_refresh(f"Uploaded {len(plan)} item(s)")

        self._run_modal(f"Uploading {len(plan)} item(s)", run, on_done=on_done,
                        on_error=lambda: self._report_error("Upload failed"), relay=relay)

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

    def upload_files_and_folders(self, files):
        """Kept for callers that pass (path, relative_dir) pairs."""
        paths = []
        for entry in files:
            paths.append(entry[0] if isinstance(entry, (tuple, list)) else entry)
        self.upload_paths(paths)

    def handle_drop_event(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self.upload_paths(paths)

    # ------------------------------------------------------------ device to PC

    def download_selected_items(self, dest_dir=None):
        names = self._selected_names()
        if not names:
            return

        if dest_dir is None:
            dest_dir = QFileDialog.getExistingDirectory(
                self, "Download Location", self._last_download_dir or "")
            if not dest_dir:
                return
        self._last_download_dir = dest_dir

        batch, renamed = [], []
        remembered = None
        for position, name in enumerate(names):
            local = os.path.join(dest_dir, name)
            if not os.path.exists(local):
                batch.append(name)
                continue

            choice = remembered
            if choice is None:
                dialog = transfer.ConflictDialog(self, name, remaining=len(names) - position - 1)
                if dialog.exec() != transfer.QDialog.DialogCode.Accepted:
                    self._note("Download canceled")
                    return
                choice = dialog.choice
                if dialog.apply_to_all.isChecked():
                    remembered = choice

            if choice == transfer.SKIP:
                continue
            if choice == transfer.KEEP_BOTH:
                renamed.append((name, transfer.local_unique_path(dest_dir, name)))
            else:
                batch.append(name)

        if not batch and not renamed:
            self._note("Nothing to download - all items skipped")
            return

        relay = ProgressRelay()

        def run():
            ok = True
            if batch:
                ok = self.adb_handler.pull_items(
                    [self._remote_path(n) for n in batch], dest_dir,
                    line_callback=relay.message.emit)
            for name, local in renamed:
                relay.message.emit(f"Downloading {name}")
                ok = self.adb_handler.pull_file(self._remote_path(name), local) and ok
            return ok

        total = len(batch) + len(renamed)

        def on_done(_):
            self._note(f"Downloaded {total} item(s) to {dest_dir}")

        self._run_modal(f"Downloading {total} item(s)", run, on_done=on_done,
                        on_error=lambda: self._report_error("Download failed"), relay=relay)

    def is_dir_for_path(self, path):
        name = path.rstrip('/').split('/')[-1]
        for item in (self.all_files or []):
            if item.name == name:
                return item.is_dir
        return False

    def selection_size(self):
        """Bytes in the current selection, measuring folders with one du call."""
        total = 0
        folders = []
        for index in self.tree_view.selectionModel().selectedRows():
            name = self.tree_model.data(index.siblingAtColumn(0))
            if not name or name == "..":
                continue
            if self.is_dir_from_row(index.row()):
                folders.append(self._remote_path(name))
            else:
                total += self.get_file_size_from_row(index.row())
        if folders:
            total += self.adb_handler.disk_usage_many(folders)
        return total

    def drag_out_payload(self):
        """Pull the selection to a temp folder so it can be dropped on the PC.

        The files must exist before the drag starts, so only a selection small
        enough to fetch without a noticeable pause is prepared. Anything larger
        goes through "Download to PC" or the PC clipboard instead.
        """
        names = self._selected_names()
        if not names:
            return None
        if self.selection_size() > transfer.DRAG_OUT_SIZE_LIMIT:
            self._note("Too large to drag to the PC - use Download to PC (Ctrl+E)")
            return None

        target = transfer.temp_transfer_dir(self.device_serial)
        ok = self.adb_handler.pull_items([self._remote_path(n) for n in names], target)
        if not ok:
            self._report_error("Could not prepare the drag")
            return None
        return [p for p in (os.path.join(target, n) for n in names) if os.path.exists(p)]

    # -------------------------------------------------------- device to device

    def receive_items_from(self, source_panel, items, move=False):
        """Pull items from another panel's device into this one."""
        source_panel.send_items_to_device(items, self, move=move)

    def send_items_to_device(self, items, target_panel, move=False):
        if not items:
            return
        dest_dir = target_panel.current_path.rstrip('/') or '/'
        by_name = {i['name']: i for i in items}
        plan, overwrites = transfer.resolve_conflicts(
            target_panel, target_panel.adb_handler, dest_dir, list(by_name))
        if plan is None:
            self._note("Canceled")
            return
        if not plan:
            self._note("Nothing to send - all items skipped")
            return

        relay = ProgressRelay()
        verb = "Moving" if move else "Sending"

        def run():
            done = 0
            for name, dest_name in plan:
                item = by_name[name]
                dest = f"{dest_dir}/{dest_name}"
                relay.message.emit(f"{verb} {name}")
                if name in overwrites:
                    target_panel.adb_handler.delete_item(dest, is_dir=True)
                if item['is_dir']:
                    ok = self.adb_handler.stream_directory(
                        self.device_serial, item['path'],
                        target_panel.device_serial, dest,
                        line_callback=relay.message.emit,
                    )
                else:
                    ok = self.adb_handler.stream_file(
                        self.device_serial, item['path'],
                        target_panel.device_serial, dest,
                    )
                if not ok:
                    return done
                if move and not self.adb_handler.delete_item(item['path'], is_dir=item['is_dir']):
                    return done
                done += 1
            return done

        def on_done(count):
            target_panel._note_and_refresh(
                f"Received {count} item(s) from {self.device_name}")
            if move:
                self._note_and_refresh(f"Moved {count} item(s) to {target_panel.device_name}")
            else:
                self._note(f"Sent {count} item(s) to {target_panel.device_name}")

        self._run_modal(f"{verb} {len(plan)} item(s) to {target_panel.device_name}",
                        run, on_done=on_done,
                        on_error=lambda: self._report_error("Transfer failed"), relay=relay)

    # kept for the older call site in the context menu
    def _send_items_to_device(self, items, target_panel, move=False):
        self.send_items_to_device(items, target_panel, move=move)

    def _other_panels(self):
        window = self.window()
        return [p for p in getattr(window, 'device_panels', [])
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
                lambda checked=False, p=panel, its=items: self.send_items_to_device(its, p, move=True))

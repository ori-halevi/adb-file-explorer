from PyQt6.QtWidgets import QTreeView, QMessageBox
from PyQt6.QtCore import Qt, QMimeData, QUrl
from PyQt6.QtGui import QDrag, QGuiApplication


ADB_MIME = "application/x-adb-paths"


class DropTreeView(QTreeView):
    """File view with drag-and-drop in every familiar direction.

    * drop files from the PC   -> upload here
    * drag items out to the PC -> download wherever they are dropped
    * drag between panels      -> copy, or move while holding Shift
    * drag inside one panel    -> move, or copy while holding Ctrl
    """

    def __init__(self, parent_widget, main_window):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.main_window = main_window
        self.setAcceptDrops(True)
        self.setDragDropMode(QTreeView.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setAlternatingRowColors(True)
        self.setDropIndicatorShown(True)

    def _get_device_serial(self):
        if hasattr(self.main_window, 'device_serial'):
            return self.main_window.device_serial
        if hasattr(self.main_window, 'adb_handler') and self.main_window.adb_handler:
            return self.main_window.adb_handler.device_serial or ""
        return ""

    # --------------------------------------------------------------- accepting

    def _acceptable(self, event):
        md = event.mimeData()
        return (md.hasUrls() and not event.source()) or md.hasFormat(ADB_MIME)

    def dragEnterEvent(self, event):
        if self._acceptable(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._acceptable(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()

        if md.hasUrls() and not event.source():
            self.main_window.handle_drop_event(event)
            event.acceptProposedAction()
            return

        if md.hasFormat(ADB_MIME):
            raw = bytes(md.data(ADB_MIME)).decode("utf-8")
            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            if len(lines) < 2:
                event.ignore()
                return

            src_serial, paths = lines[0], lines[1:]
            dest = self._get_drop_destination(event)
            modifiers = QGuiApplication.keyboardModifiers()
            my_serial = self._get_device_serial()

            if src_serial != my_serial and my_serial:
                # Across devices the default is a copy; Shift turns it into a move.
                move = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                self._handle_cross_device_drop(src_serial, paths, dest, move)
            else:
                # Within one device the default is a move, as in a file manager;
                # Ctrl turns it into a copy.
                move = not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
                self.main_window.move_or_copy_items(paths, dest, move=move)

            event.acceptProposedAction()
            return

        event.ignore()

    def _get_drop_destination(self, event):
        dest = self.main_window.current_path
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            name = self.model().data(index.siblingAtColumn(0))
            is_dir = self.model().data(index.siblingAtColumn(1)) == "Directory"
            if name == "..":
                dest = "/".join(self.main_window.current_path.rstrip("/").split("/")[:-1]) or "/"
            elif is_dir:
                dest = f"{self.main_window.current_path.rstrip('/')}/{name}"
        return dest

    def _handle_cross_device_drop(self, src_serial, paths, dest_path, move=False):
        panel = self._source_panel(src_serial)
        if panel is not None:
            items = [{
                'name': p.rstrip('/').split('/')[-1],
                'path': p,
                'is_dir': panel.is_dir_for_path(p),
            } for p in paths]
            panel.send_items_to_device(items, self.main_window, move=move)
            return

        if hasattr(self.main_window, 'cross_device_drop'):
            self.main_window.cross_device_drop.emit(src_serial, paths, dest_path)
            return

        QMessageBox.warning(self, "Cross-Device Drop",
                            "Cross-device streaming not available from this view.")

    def _source_panel(self, serial):
        window = self.window()
        for panel in getattr(window, 'device_panels', []):
            if panel.device_serial == serial:
                return panel
        return None

    # ---------------------------------------------------------------- dragging

    def startDrag(self, supportedActions):
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return

        serial = self._get_device_serial()
        paths = []
        for index in indexes:
            name = self.model().data(index.siblingAtColumn(0))
            if name and name != "..":
                paths.append(f"{self.main_window.current_path.rstrip('/')}/{name}")

        if not paths:
            return

        md = QMimeData()
        md.setData(ADB_MIME, "\n".join([serial] + paths).encode("utf-8"))

        # Attaching real file URLs is what lets the selection be dropped on the
        # desktop or in Explorer. The files have to exist before the drag
        # starts, so the panel only fetches a selection small enough to pull
        # without a noticeable pause.
        local_paths = self.main_window.drag_out_payload()
        if local_paths:
            md.setUrls([QUrl.fromLocalFile(p) for p in local_paths])

        drag = QDrag(self)
        drag.setMimeData(md)
        # Always a copy as far as the drag protocol is concerned; whether an
        # internal drop moves or copies is decided from the modifier keys.
        drag.exec(Qt.DropAction.CopyAction)

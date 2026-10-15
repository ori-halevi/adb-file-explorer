"""File views: the details table, and the list/icon/tile/content views.

Both view widgets share one model, one selection model and one set of
drag-and-drop rules (DropMixin), so switching how files are shown never
changes what the panel can do with them.
"""

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QListView, QMenu, QMessageBox, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTreeView,
)
from PyQt6.QtCore import QItemSelectionModel, QMimeData, QSize, Qt, QUrl
from PyQt6.QtGui import (QColor, QCursor, QDrag, QFontMetrics, QGuiApplication, QIcon,
                         QPainter, QPixmap)

ADB_MIME = "application/x-adb-paths"
# Marks a drag started with the right button, which asks on release.
ADB_RIGHT_MIME = "application/x-adb-right-drag"

# key, label, keyboard number, icon pixels
DETAILS = 'details'
LIST = 'list'
TILES = 'tiles'
CONTENT = 'content'
ICONS_XL = 'icons_xl'
ICONS_L = 'icons_l'
ICONS_M = 'icons_m'
ICONS_S = 'icons_s'

VIEW_MODES = (
    (ICONS_XL, "Extra large icons", 256),
    (ICONS_L, "Large icons", 96),
    (ICONS_M, "Medium icons", 48),
    (ICONS_S, "Small icons", 16),
    (LIST, "List", 16),
    (DETAILS, "Details", 16),
    (TILES, "Tiles", 48),
    (CONTENT, "Content", 32),
)

ICON_MODES = (ICONS_XL, ICONS_L, ICONS_M, ICONS_S)
# Zooming with Ctrl+wheel walks this ladder, as it does in Explorer.
ZOOM_ORDER = (ICONS_S, LIST, DETAILS, CONTENT, TILES, ICONS_M, ICONS_L, ICONS_XL)

# Thumbnails are only worth fetching where they can actually be seen.
THUMBNAIL_PX = {
    ICONS_XL: 256,
    ICONS_L: 96,
    ICONS_M: 48,
    TILES: 48,
    CONTENT: 32,
}


def mode_label(mode):
    for key, label, _ in VIEW_MODES:
        if key == mode:
            return label
    return mode


def icon_px(mode):
    for key, _, px in VIEW_MODES:
        if key == mode:
            return px
    return 16


def thumbnail_px(mode):
    """The thumbnail size this view needs, or None if it shows none."""
    return THUMBNAIL_PX.get(mode)


class DeviceFiles(QMimeData):
    """Drag data that fetches the files only if the receiver asks for them.

    Windows wants a list of real paths before a drag begins, which is why a
    large selection could not be dragged out: fetching it inside the gesture
    was not possible. Qt asks a QMimeData subclass for its data the moment a
    receiver reads it, so the drag can advertise the files immediately - the
    cursor behaves normally, Ctrl and Shift do what they always do - and the
    bytes are fetched when the drop happens, which is the point at which the
    user has asked for them.

    Dropping back into the app never touches this: those drops read the
    internal format instead, so nothing is fetched.
    """

    URI_LIST = "text/uri-list"

    def __init__(self, panel, names, payload):
        super().__init__()
        self._panel = panel
        self._names = list(names)
        self._payload = payload          # callable(names) -> list of local paths
        self._urls = None
        self.fetched = False

    def formats(self):
        formats = list(super().formats())
        if self.URI_LIST not in formats:
            formats.append(self.URI_LIST)
        return formats

    def hasFormat(self, mime_type):
        if mime_type == self.URI_LIST:
            return bool(self._names)
        return super().hasFormat(mime_type)

    def retrieveData(self, mime_type, preferred_type):
        if mime_type != self.URI_LIST:
            return super().retrieveData(mime_type, preferred_type)

        if self._urls is None:
            self._urls = []
            try:
                for path in self._payload(self._names) or []:
                    self._urls.append(QUrl.fromLocalFile(path))
            except Exception:
                self._urls = []
            # Only count as handed over if something actually went out.
            self.fetched = bool(self._urls)
        return self._urls


class DropMixin:
    """Drag-and-drop shared by every file view.

    * drop files from the PC    -> upload here
    * Alt-drag items out        -> drop them on the PC
    * drag between panels       -> copy, or move while holding Shift
    * drag inside one panel     -> move, or copy while holding Ctrl
    """

    def init_drops(self, parent_widget, main_window):
        self.parent_widget = parent_widget
        self.main_window = main_window
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)

    def _get_device_serial(self):
        if hasattr(self.main_window, 'device_serial'):
            return self.main_window.device_serial
        if getattr(self.main_window, 'adb_handler', None):
            return self.main_window.adb_handler.device_serial or ""
        return ""

    # ----------------------------------------------------------- actions

    def _dragged_serial(self, mime):
        raw = bytes(mime.data(ADB_MIME)).decode("utf-8", errors="replace")
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        return lines[0] if lines else ""

    def default_action_for(self, source_serial):
        """What a plain drag means, following the rule Windows uses.

        Within one storage a drag moves, and between two different storages
        it copies. A phone and the PC, or two phones, are different storages.
        """
        mine = self._get_device_serial()
        if source_serial and mine and source_serial == mine:
            return Qt.DropAction.MoveAction
        return Qt.DropAction.CopyAction

    @staticmethod
    def action_for_modifiers(modifiers, default):
        """Ctrl copies and Shift moves, whatever the default would be."""
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return Qt.DropAction.CopyAction
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return Qt.DropAction.MoveAction
        return default

    def _intended_action(self, event):
        md = event.mimeData()
        if md.hasFormat(ADB_MIME):
            default = self.default_action_for(self._dragged_serial(md))
            return self.action_for_modifiers(event.modifiers(), default)
        # Files arriving from the PC are always copied: a move would have
        # Windows delete the user's own files once the upload finished.
        return Qt.DropAction.CopyAction

    # --------------------------------------------------------------- accepting

    def _acceptable(self, event):
        md = event.mimeData()
        return (md.hasUrls() and not event.source()) or md.hasFormat(ADB_MIME)

    def _accept(self, event):
        """Accept with the action we intend, which is what draws the cursor.

        Qt turns the accepted action into the platform's own drag cursor, so
        a copy shows the plus badge and a move does not.
        """
        action = self._intended_action(event)
        event.setDropAction(action)
        event.accept()

    def dragEnterEvent(self, event):
        if self._acceptable(event):
            self._accept(event)
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._acceptable(event):
            self._accept(event)
            return
        event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()

        if md.hasUrls() and not event.source():
            event.setDropAction(Qt.DropAction.CopyAction)
            # Dropping onto a folder row puts the files in that folder, as it
            # would in a file manager.
            self.main_window.handle_drop_event(event, self._get_drop_destination(event))
            event.accept()
            return

        if md.hasFormat(ADB_MIME):
            raw = bytes(md.data(ADB_MIME)).decode("utf-8")
            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            if len(lines) < 2:
                event.ignore()
                return

            src_serial, paths = lines[0], lines[1:]
            dest = self._get_drop_destination(event)
            action = self._intended_action(event)

            if md.hasFormat(ADB_RIGHT_MIME):
                # A drag with the right button asks on release, as Windows does.
                action = self._ask_drop_action(event, action)
                if action is None:
                    event.ignore()
                    return

            move = action == Qt.DropAction.MoveAction
            if src_serial != self._get_device_serial() and self._get_device_serial():
                self._handle_cross_device_drop(src_serial, paths, dest, move)
            else:
                self.main_window.move_or_copy_items(paths, dest, move=move)

            event.setDropAction(action)
            event.accept()
            return

        event.ignore()

    def _ask_drop_action(self, event, default):
        """The menu Windows shows when a drag is made with the right button."""
        menu = QMenu(self)
        copy_action = menu.addAction("Copy here")
        move_action = menu.addAction("Move here")
        menu.addSeparator()
        menu.addAction("Cancel")

        bold = copy_action if default == Qt.DropAction.CopyAction else move_action
        font = bold.font()
        font.setBold(True)
        bold.setFont(font)
        menu.setDefaultAction(bold)

        chosen = menu.exec(self.viewport().mapToGlobal(event.position().toPoint()))
        if chosen is copy_action:
            return Qt.DropAction.CopyAction
        if chosen is move_action:
            return Qt.DropAction.MoveAction
        return None

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
            # dest_path is the row the items were dropped on, which may be a
            # subfolder rather than the panel's current directory.
            panel.send_items_to_device(items, self.main_window, move=move,
                                       dest_dir=dest_path)
            return

        if hasattr(self.main_window, 'cross_device_drop'):
            self.main_window.cross_device_drop.emit(src_serial, paths, dest_path, move)
            return

        QMessageBox.warning(self, "Cross-Device Drop",
                            "Cross-device streaming not available from this view.")

    def _source_panel(self, serial):
        for panel in getattr(self.window(), 'device_panels', []):
            if panel.device_serial == serial:
                return panel
        return None

    def selected_rows(self):
        """Selected rows, however the view happens to report them."""
        model = self.selectionModel()
        if model is None:
            return []
        rows = model.selectedRows()
        if rows:
            return rows
        seen = {}
        for index in model.selectedIndexes():
            seen.setdefault(index.row(), index.siblingAtColumn(0))
        return [seen[row] for row in sorted(seen)]

    # ---------------------------------------------------------------- dragging

    def mousePressEvent(self, event):
        """Both buttons drag only what is already selected.

        Pressing on an item that is part of the selection begins a drag of
        the whole selection. Pressing anywhere else leaves the button to its
        other job, sweeping out a selection, which is how several items get
        picked in the first place.

        The press itself still goes to the view, so clicking, Ctrl-clicking
        and Shift-clicking keep working - including the way a click on a
        selected item only collapses the selection when the button comes back
        up, which is what lets a drag start from several items.
        """
        self._end_sweep()
        self._press_button = event.button()
        self._press_pos = event.position().toPoint()

        index = self.indexAt(self._press_pos)
        self._press_on_selection = (
            index.isValid() and index.row() in {i.row() for i in self.selected_rows()})

        if self._press_button == Qt.MouseButton.RightButton:
            if self._press_on_selection:
                # The view has no use for a right press on the selection, and
                # handing it over would only move the current item about.
                event.accept()
                return
            self._begin_sweep()
        elif not self._press_on_selection:
            self._begin_sweep()

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        button = getattr(self, '_press_button', None)

        if button is not None and event.buttons():
            moved = (event.position().toPoint() - self._press_pos).manhattanLength()
            far_enough = moved >= QApplication.startDragDistance()

            if self._press_on_selection and far_enough:
                # Start the drag here rather than leaving it to the view,
                # which only begins one while the cursor is still over the
                # item that was pressed - a quick flick would otherwise turn
                # into a selection sweep.
                self._press_button = None
                self._drag_took_over = True
                self.startDrag(
                    Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                    right_button=(button == Qt.MouseButton.RightButton))
                self.setState(QAbstractItemView.State.NoState)
                return

            if button == Qt.MouseButton.RightButton and self._press_on_selection:
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_button = None
        self._press_on_selection = False
        self._end_sweep()

        if getattr(self, '_drag_took_over', False):
            # A drag took this gesture over. The release still has to reach
            # the view, because that is the only thing that erases a
            # selection rectangle - a repaint on its own draws it again.
            # Handing it on plainly would collapse the selection to whatever
            # sits under the cursor now, so it goes through with selection
            # turned off and the signals of a click held back.
            self._drag_took_over = False
            mode = self.selectionMode()
            blocked = self.signalsBlocked()
            self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.blockSignals(True)
            try:
                super().mouseReleaseEvent(event)
            finally:
                self.blockSignals(blocked)
                self.setSelectionMode(mode)
        else:
            super().mouseReleaseEvent(event)

        # Whichever way it ended, nothing of the gesture should still show.
        self.setState(QAbstractItemView.State.NoState)
        self.viewport().update()

    def _begin_sweep(self):
        """Let the view select freely, without starting a drag part-way."""
        self._sweeping = True
        self.setDragEnabled(False)

    def _end_sweep(self):
        if getattr(self, '_sweeping', False):
            self._sweeping = False
            self.setDragEnabled(True)

    def startDrag(self, supportedActions, right_button=False):
        indexes = self.selected_rows()
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

        names = [p.rstrip('/').split('/')[-1] for p in paths]
        md = DeviceFiles(self.main_window, names, self.main_window.fetch_for_drag)
        md.setData(ADB_MIME, "\n".join([serial] + paths).encode("utf-8"))
        if right_button:
            md.setData(ADB_RIGHT_MIME, b"1")

        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(self._drag_pixmap(indexes))

        default = self.default_action_for(serial)
        result = drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                           default)

        # A drop inside the app reads the device format, so anything the
        # system asked for on the way is not wanted.
        landed_here = drag.target() is not None
        if self.main_window.take_pending_fetch(run=not landed_here):
            # There was too much to fetch inside the gesture, so it is being
            # fetched now instead; the drag can be repeated once it is ready.
            pass
        elif md.fetched and drag.target() is None:
            # The receiver took the files. A move means it has deleted its
            # copy, so only the originals are left to remove.
            if result == Qt.DropAction.MoveAction:
                self.main_window.confirm_move_to_pc(paths)
        elif not md.fetched and drag.target() is None and self._ended_outside_the_app():
            # Nothing was ever asked for, so the drop was refused or the drag
            # was abandoned; offer the transfer that always works.
            self.main_window.offer_download_after_drag(paths)

    @staticmethod
    def _ended_outside_the_app():
        """True when the pointer finished over something that is not ours."""
        where = QCursor.pos()
        for window in QApplication.topLevelWidgets():
            if window.isVisible() and window.frameGeometry().contains(where):
                return False
        return True

    def _drag_pixmap(self, indexes):
        """What the cursor carries: the first icon, with a count beside it."""
        size = max(32, min(self.iconSize().width(), 64))
        pixmap = QPixmap(size + 54, size + 8)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        icon = self.model().data(indexes[0].siblingAtColumn(0),
                                 Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            painter.drawPixmap(4, 4, icon.pixmap(size, size))
        if len(indexes) > 1:
            badge = f"{len(indexes)}"
            painter.setPen(self.palette().highlightedText().color())
            painter.fillRect(size + 8, size // 2 - 10, 40, 20,
                             self.palette().highlight())
            painter.drawText(size + 14, size // 2 + 5, badge)
        painter.end()
        return pixmap


class DropTreeView(DropMixin, QTreeView):
    """The Details view."""

    def __init__(self, parent_widget, main_window):
        super().__init__(parent_widget)
        self.init_drops(parent_widget, main_window)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)


class RowDelegate(QStyledItemDelegate):
    """Draws a name with its details beside it, for Tiles and Content."""

    def __init__(self, parent, width, height, show_date=False):
        super().__init__(parent)
        self._width = width
        self._height = height
        self._show_date = show_date

    def sizeHint(self, option, index):
        return QSize(self._width, self._height)

    def paint(self, painter, option, index):
        model = index.model()
        name = model.data(index.siblingAtColumn(0)) or ""
        kind = model.data(index.siblingAtColumn(1)) or ""
        size = model.data(index.siblingAtColumn(2)) or ""
        modified = model.data(index.siblingAtColumn(4)) or ""

        view_option = QStyleOptionViewItem(option)
        self.initStyleOption(view_option, index)
        # The text and icon are drawn by hand, so the style paints only the
        # background and the selection frame.
        view_option.text = ""
        view_option.icon = QIcon()
        widget = view_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, view_option,
                          painter, widget)

        rect = option.rect
        icon = model.data(index.siblingAtColumn(0), Qt.ItemDataRole.DecorationRole)
        icon_box = min(rect.height() - 8, 48)
        painter.save()
        if icon is not None and not icon.isNull():
            pixmap = icon.pixmap(icon_box, icon_box)
            x = rect.left() + 6 + max(0, (icon_box - pixmap.width()) // 2)
            y = rect.top() + max(0, (rect.height() - pixmap.height()) // 2)
            painter.drawPixmap(x, y, pixmap)

        text_left = rect.left() + icon_box + 14
        text_width = rect.width() - (text_left - rect.left()) - 8
        metrics = QFontMetrics(option.font)
        line = metrics.height()

        if view_option.state & QStyle.StateFlag.State_Selected:
            name_colour = option.palette.highlightedText().color()
        else:
            name_colour = option.palette.text().color()
        painter.setPen(name_colour)
        top = rect.top() + (rect.height() - line * 2) // 2
        painter.drawText(text_left, top + metrics.ascent(),
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, text_width))

        detail = kind if not size else f"{kind}, {self._pretty(size)}"
        if self._show_date and modified:
            detail = f"{detail}    {modified}" if detail else modified
        # Derived from the name colour rather than taken from the palette, so
        # the second line stays readable whichever theme is in use.
        faded = QColor(name_colour)
        faded.setAlpha(165)
        painter.setPen(faded)
        painter.drawText(text_left, top + line + metrics.ascent(),
                         metrics.elidedText(detail, Qt.TextElideMode.ElideRight, text_width))
        painter.restore()

    @staticmethod
    def _pretty(size_text):
        try:
            value = float(size_text)
        except (TypeError, ValueError):
            return str(size_text)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024:
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"


class FileListView(DropMixin, QListView):
    """One widget that covers List, the four icon sizes, Tiles and Content."""

    def __init__(self, parent_widget, main_window):
        super().__init__(parent_widget)
        self.init_drops(parent_widget, main_window)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # A list view shows one column, so by default a click selects a cell
        # rather than a row and selectedRows() comes back empty - which left
        # every action that works on the selection doing nothing here.
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Dragging from empty space draws a selection rectangle, as in a file
        # manager; dragging from an item still starts a drag.
        self.setSelectionRectVisible(True)
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._mode = ICONS_M
        self._row_delegate = None

    def _match_explorer_scrolling(self, step):
        """Scroll a wheel notch the distance Explorer would.

        Windows turns one notch into however many lines the user has set,
        three by default, and Qt multiplies that by the scroll bar's step.
        Per-item scrolling ignores all of it and jumps a whole row at a
        time, which is what made this feel unlike every other file list.
        """
        step = max(1, int(step))
        for mode_setter in (self.setVerticalScrollMode,
                            self.setHorizontalScrollMode):
            mode_setter(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(step)
        self.horizontalScrollBar().setSingleStep(step)

    def apply_mode(self, mode):
        self._mode = mode
        px = icon_px(mode)
        self.setIconSize(QSize(px, px))
        self._row_delegate = None
        self.setItemDelegate(QStyledItemDelegate(self))

        if mode in ICON_MODES:
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
            self.setSpacing(6)
            # Room under the icon for one or two lines of file name.
            self.setGridSize(QSize(max(px + 36, 76), px + 42))
            self.setUniformItemSizes(True)
        elif mode == LIST:
            self.setViewMode(QListView.ViewMode.ListMode)
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(True)
            self.setSpacing(1)
            self.setGridSize(QSize(240, px + 8))
            self.setUniformItemSizes(True)
        elif mode == TILES:
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
            self.setSpacing(4)
            self.setGridSize(QSize(290, 64))
            self.setUniformItemSizes(True)
            self._row_delegate = RowDelegate(self, 286, 60)
            self.setItemDelegate(self._row_delegate)
        elif mode == CONTENT:
            self.setViewMode(QListView.ViewMode.ListMode)
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(False)
            self.setSpacing(2)
            self.setGridSize(QSize())
            self.setUniformItemSizes(True)
            self._row_delegate = RowDelegate(self, 600, 48, show_date=True)
            self.setItemDelegate(self._row_delegate)

        # A notch should carry the eye about one row, as it does in a folder
        # window; the three lines Windows sends make that a third of a row.
        cell = self.gridSize()
        across = mode == LIST
        reach = (cell.width() if across else cell.height()) or (px + 42)
        self._match_explorer_scrolling(reach / 3)

        self.doItemsLayout()

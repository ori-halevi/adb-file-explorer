"""File views: the details table, and the list/icon/tile/content views.

Both view widgets share one model, one selection model and one set of
drag-and-drop rules (DropMixin), so switching how files are shown never
changes what the panel can do with them.
"""

from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QListView, QMessageBox, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTreeView,
)
from PyQt6.QtCore import QMimeData, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDrag, QFontMetrics, QGuiApplication, QIcon

ADB_MIME = "application/x-adb-paths"

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
            # The event carries the modifiers held at drop time; a global
            # snapshot can be stale and would turn a Ctrl-copy into a move.
            modifiers = event.modifiers()
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

    def startDrag(self, supportedActions):
        indexes = self.selectionModel().selectedRows() or self.selectionModel().selectedIndexes()
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

        # Real file URLs are what let a selection be dropped on the desktop or
        # in Explorer, but they only exist once the files have been pulled, and
        # that pull blocks the window. So it happens only for an explicit
        # Alt-drag; a drag between panels stays instant and costs no adb calls.
        if QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            local_paths = self.main_window.drag_out_payload()
            if local_paths:
                md.setUrls([QUrl.fromLocalFile(p) for p in local_paths])
        else:
            self.main_window.hint_drag_to_pc()

        drag = QDrag(self)
        drag.setMimeData(md)
        # Always a copy as far as the drag protocol is concerned; whether an
        # internal drop moves or copies is decided from the modifier keys.
        drag.exec(Qt.DropAction.CopyAction)


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
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._mode = ICONS_M
        self._row_delegate = None

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

        self.doItemsLayout()

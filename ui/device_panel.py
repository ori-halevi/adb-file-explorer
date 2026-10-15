import os
import sys
import tempfile
import subprocess
import time

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QMessageBox, QLineEdit, QMenu, QFileDialog, QInputDialog,
    QLabel, QCheckBox, QFileIconProvider, QProgressBar, QDialog, QTreeView,
    QStackedWidget, QToolButton, QAbstractItemView,
)
from PyQt6.QtGui import (QStandardItemModel, QStandardItem, QAction, QKeySequence,
                         QShortcut, QIcon)
from PyQt6.QtCore import QStringListModel
from PyQt6.QtWidgets import QCompleter
from PyQt6.QtCore import (Qt, QTimer, QFileInfo, pyqtSignal, QPoint, QEvent,
                          QItemSelectionModel)

from PyQt6.QtGui import QGuiApplication

from PyQt6.QtCore import QSettings, QSize

from handler import ADBHandler
from ui.task_manager import WorkerThread
from ui import transfer, thumbnails, views
from ui.views import DropTreeView, FileListView
from ui.transfer_actions import TransferActionsMixin


class PathInput(QLineEdit):

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self._completer = QCompleter([], self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(self._completer)
        self.returnPressed.connect(self._navigate)

    def update_completions(self, items):
        names = [f.name for f in items] if items else []
        
        parent = "/".join(self._panel.current_path.rstrip("/").split("/")[:-1]) or "/"
        names.append("..")
        model = QStringListModel(names)
        self._completer.setModel(model)

    def _navigate(self):
        text = self.text().strip()
        if not text:
            return
        
        if text.startswith("/"):
            target = text
        else:
            target = f"{self._panel.current_path.rstrip('/')}/{text}"
        self._panel.current_path = target
        self._panel.path_history.append(target)
        self._panel.clear_search_on_navigation()
        self._panel.update_path_display()
        self._panel.refresh_files()


class DevicePanel(TransferActionsMixin, QWidget):

    # src_serial, remote paths, destination dir, move instead of copy
    cross_device_drop = pyqtSignal(str, list, str, bool)
    close_requested = pyqtSignal()

    def __init__(self, parent, adb_handler, device_info=None):
        super().__init__(parent)
        self.adb_handler = adb_handler
        self.device_info = device_info or {}
        self._active_process = None

        
        self.clipboard = {'items': [], 'operation': None}
        self._clipboard_time = 0.0
        self._host_clipboard_time = 0.0
        self._last_download_dir = None
        self._post_refresh_note = None
        self._pending_restore = None
        self._loaded_path = None
        self._drag_hint_shown = False
        self.sort_key = 'name'
        self.sort_desc = False
        self._items_by_name = {}
        self._thumb_keys = {}
        self._thumb_generation = 0
        # Transfer workers that must finish before the panel may be deleted.
        self._transfer_tasks = set()
        QGuiApplication.clipboard().dataChanged.connect(self._on_host_clipboard_changed)

        
        self.use_root = False
        self._loading = False
        self._refresh_pending = False
        self._refresh_task = None
        self._closing = False

        
        self._icon_provider = QFileIconProvider()
        self._folder_icon = self._icon_provider.icon(QFileIconProvider.IconType.Folder)
        self._file_icon = self._icon_provider.icon(QFileIconProvider.IconType.File)
        self._icon_cache = {}
        self._generic_icon_key = None

        
        self.root_path = "/"
        self.current_path = "/storage/emulated/0"
        self.path_history = [self.current_path]
        self.all_files = []

        self._thumbs = thumbnails.ThumbnailCache(self)
        self._thumbs.ready.connect(self._apply_thumbnail)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self._load_visible_thumbnails)

        self._setup_ui()
        self._setup_context_menu()
        self._restore_view_mode()
        self.sort_key = self._settings().value("sort_key", "name", type=str)
        self.sort_desc = self._settings().value("sort_desc", False, type=bool)
        self._update_sort_indicator()
        self._update_sort_button()
        self.update_path_display()

    @property
    def device_serial(self):
        return self.adb_handler.device_serial or ""

    @property
    def device_name(self):
        serial = self.device_serial
        model = self.device_info.get("model") or self.adb_handler.devices.get(serial, "")
        return model or serial

    

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        
        header_container = QWidget()
        header_container.setObjectName("panel_header")
        header_layout = QHBoxLayout(header_container)
        header_layout.setContentsMargins(6, 2, 4, 2)
        self.device_header = QLabel(self.device_name)
        self.device_header.setObjectName("device_header")
        header_layout.addWidget(self.device_header)
        header_layout.addStretch()
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("panel_close_btn")
        self.close_btn.setToolTip("Close this device panel")
        self.close_btn.setAccessibleName("Close device panel")
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self.close_btn)
        layout.addWidget(header_container)

        
        self.conn_label = QLabel("🟢 Connected" if self.adb_handler.device_connected else "🔴 Disconnected")
        self.conn_label.setStyleSheet("color: green; font-size: 10px; padding: 0 8px 2px 8px;")
        layout.addWidget(self.conn_label)

        
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(4)

        self.back_btn = QPushButton("←")
        self.back_btn.setMaximumWidth(36)
        self.back_btn.setObjectName("back_btn")
        self.back_btn.clicked.connect(self.go_back)
        nav_layout.addWidget(self.back_btn)

        self.up_btn = QPushButton("↑")
        self.up_btn.setMaximumWidth(36)
        self.up_btn.setObjectName("up_btn")
        self.up_btn.clicked.connect(self.go_up)
        nav_layout.addWidget(self.up_btn)

        self.path_display = PathInput(self)
        self.path_display.setObjectName("path_display")
        nav_layout.addWidget(self.path_display)

        refresh_btn = QPushButton("↻")
        refresh_btn.setMaximumWidth(32)
        refresh_btn.clicked.connect(self.refresh_files)
        nav_layout.addWidget(refresh_btn)

        self.to_pc_btn = QPushButton("\u2193 To PC")
        self.to_pc_btn.setObjectName("to_pc_btn")
        self.to_pc_btn.setToolTip("Download the selected files and folders to this PC (Ctrl+E)")
        # clicked() hands a "checked" bool to any slot that can take an
        # argument, which would land in dest_dir, so swallow it here.
        self.to_pc_btn.clicked.connect(lambda: self.download_selected_items())
        nav_layout.addWidget(self.to_pc_btn)

        self.from_pc_btn = QPushButton("\u2191 From PC")
        self.from_pc_btn.setObjectName("from_pc_btn")
        self.from_pc_btn.setToolTip("Upload files from this PC to the current folder (Ctrl+U)")
        self.from_pc_btn.clicked.connect(lambda: self.upload_file_to_device())
        nav_layout.addWidget(self.from_pc_btn)

        self.sort_btn = QToolButton()
        self.sort_btn.setObjectName("sort_btn")
        self.sort_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sort_btn.setMenu(self._build_sort_menu())
        nav_layout.addWidget(self.sort_btn)

        self.view_btn = QToolButton()
        self.view_btn.setText("▦")
        self.view_btn.setToolTip("Change the view (Ctrl+1 to Ctrl+8, or Ctrl+wheel)")
        self.view_btn.setObjectName("view_btn")
        self.view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_btn.setMenu(self._build_view_menu())
        nav_layout.addWidget(self.view_btn)

        nav_container = QWidget()
        nav_container.setObjectName("panel_nav")
        nav_container.setLayout(nav_layout)
        layout.addWidget(nav_container)

        
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search files...")
        self.search_bar.setObjectName("search_bar")
        self.search_bar.textChanged.connect(self.apply_search_filter)
        layout.addWidget(self.search_bar)

        
        self.tree_model = QStandardItemModel()
        self.tree_model.setHorizontalHeaderLabels(['Name', 'Type', 'Size', 'Permissions', 'Modified'])

        self._tree = DropTreeView(self, self)
        self._tree.setModel(self.tree_model)
        self._tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._tree.setColumnHidden(4, True)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, header.ResizeMode.ResizeToContents)

        self._list = FileListView(self, self)
        self._list.setModel(self.tree_model)
        # One selection model, so switching views keeps the selection.
        self._list.setSelectionModel(self._tree.selectionModel())

        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(self._tree)
        self._view_stack.addWidget(self._list)

        self._tree.selectionModel().selectionChanged.connect(self._selection_changed)

        for view in (self._tree, self._list):
            view.doubleClicked.connect(self.handle_double_click)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(self.show_context_menu)
            view.viewport().installEventFilter(self)
            view.verticalScrollBar().valueChanged.connect(self._schedule_thumbnails)

        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._header_clicked)

        self._setup_shortcuts()
        layout.addWidget(self._view_stack)

        
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("font-size: 10px; color: #888; padding: 0 4px;")
        layout.addWidget(self.status_label)

        if self.adb_handler.device_connected:
            self.refresh_files()
        else:
            self.status_label.setText("No ADB device connected")

    # ----------------------------------------------------------- views -----

    @property
    def tree_view(self):
        """The view currently on screen.

        Everything else in the panel works through this, so a new view mode
        needs no changes anywhere but here.
        """
        return self._view_stack.currentWidget()

    SORT_ARROWS = {False: "\u2191", True: "\u2193"}

    def _build_sort_menu(self):
        """Explorer's Sort menu: the field, then the direction."""
        menu = QMenu(self)
        self._sort_actions = {}
        for key, label in self.SORT_LABELS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, k=key: self.set_sort(k))
            self._sort_actions[key] = action

        menu.addSeparator()
        self._order_actions = {}
        for descending, label in ((False, "Ascending"), (True, "Descending")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, d=descending: self.set_sort(self.sort_key, d))
            self._order_actions[descending] = action

        menu.addSeparator()
        note = menu.addAction("Folders are always listed first")
        note.setEnabled(False)
        return menu

    def _update_sort_button(self):
        label = dict(self.SORT_LABELS).get(self.sort_key, "Name")
        arrow = self.SORT_ARROWS[bool(self.sort_desc)]
        self.sort_btn.setText(f"{arrow} {label}")
        self.sort_btn.setToolTip(
            f"Sorted by {label.lower()}, "
            f"{'Z to A' if self.sort_desc else 'A to Z'} - click to change")
        for key, action in getattr(self, '_sort_actions', {}).items():
            action.setChecked(key == self.sort_key)
        for descending, action in getattr(self, '_order_actions', {}).items():
            action.setChecked(descending == bool(self.sort_desc))

    def _build_view_menu(self):
        menu = QMenu(self)
        self._view_actions = {}
        for number, (mode, label, _) in enumerate(views.VIEW_MODES, start=1):
            action = menu.addAction(f"{label}\tCtrl+{number}")
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, m=mode: self.set_view_mode(m))
            self._view_actions[mode] = action

        menu.addSeparator()
        self._video_thumb_action = menu.addAction("Video thumbnails")
        self._video_thumb_action.setCheckable(True)
        self._video_thumb_action.setChecked(self._video_thumbnails_wanted())
        backend = thumbnails.video_backend()
        if backend is None:
            self._video_thumb_action.setEnabled(False)
            self._video_thumb_action.setText("Video thumbnails (no decoder)")
            self._video_thumb_action.setToolTip(
                "This build has no multimedia support; put ffmpeg on PATH to "
                "get a frame from each video")
        else:
            self._video_thumb_action.setToolTip(
                "A frame from each video, decoded by "
                + ("ffmpeg" if backend == 'ffmpeg' else "Qt, which ships with the app"))
        self._video_thumb_action.toggled.connect(self._set_video_thumbnails)

        self._small_thumb_action = menu.addAction("Thumbnails in List and Details")
        self._small_thumb_action.setCheckable(True)
        self._small_thumb_action.setChecked(self._small_thumbnails_wanted())
        self._small_thumb_action.setToolTip(
            "Also fetch pictures for the small views, which costs a read per file")
        self._small_thumb_action.toggled.connect(self._set_small_thumbnails)

        menu.addSeparator()
        self._verify_action = menu.addAction("Check contents before a move deletes")
        self._verify_action.setCheckable(True)
        self._verify_action.setChecked(self._checksum_wanted())
        self._verify_action.setToolTip(
            "A move always checks the copy's size first. This compares the "
            "contents as well, which means reading every byte on both sides.")
        self._verify_action.toggled.connect(
            lambda on: self._settings().setValue("verify_checksum", bool(on)))

        return menu

    def _small_thumbnails_wanted(self):
        return self._settings().value("small_thumbnails", False, type=bool)

    def _set_small_thumbnails(self, enabled):
        self._settings().setValue("small_thumbnails", bool(enabled))
        self._schedule_thumbnails(immediate=True)

    @staticmethod
    def _settings():
        return QSettings("ADB Explorer", "ADB Explorer")

    def _video_thumbnails_wanted(self):
        if thumbnails.video_backend() is None:
            return False
        return self._settings().value("video_thumbnails", True, type=bool)

    def _set_video_thumbnails(self, enabled):
        self._settings().setValue("video_thumbnails", bool(enabled))
        self._thumbs.clear_memory()
        self._schedule_thumbnails(immediate=True)

    def _restore_view_mode(self):
        saved = self._settings().value("view_mode", views.DETAILS, type=str)
        known = {mode for mode, _, _ in views.VIEW_MODES}
        self.set_view_mode(saved if saved in known else views.DETAILS, remember=False)

    def set_view_mode(self, mode, remember=True):
        self.view_mode = mode
        if mode == views.DETAILS:
            self._view_stack.setCurrentWidget(self._tree)
            self._tree.setIconSize(QSize(16, 16))
            # Three rows to a wheel notch, as in a details folder window.
            self._tree.setVerticalScrollMode(
                QAbstractItemView.ScrollMode.ScrollPerPixel)
            self._tree.verticalScrollBar().setSingleStep(
                max(1, self._tree.sizeHintForRow(0) or 20))
        else:
            self._list.apply_mode(mode)
            self._view_stack.setCurrentWidget(self._list)

        for key, action in getattr(self, '_view_actions', {}).items():
            action.setChecked(key == mode)
        if remember:
            self._settings().setValue("view_mode", mode)

        self._schedule_thumbnails(immediate=True)

    def zoom_view(self, step):
        """Walk the view ladder, as Ctrl+wheel does in a file manager."""
        order = views.ZOOM_ORDER
        try:
            index = order.index(self.view_mode)
        except ValueError:
            index = order.index(views.DETAILS)
        self.set_view_mode(order[max(0, min(len(order) - 1, index + step))])

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.Wheel and (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.zoom_view(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(source, event)

    # ------------------------------------------------------ thumbnails -----

    def _schedule_thumbnails(self, *args, immediate=False):
        """Ask for thumbnails shortly after the view settles."""
        if self._closing:
            return
        if not hasattr(self, '_thumb_timer'):
            return
        self._thumb_timer.start(0 if immediate else 140)

    def _load_visible_thumbnails(self):
        """Fetch thumbnails only for what is actually on screen."""
        if self._closing or not self._items_by_name:
            return
        px = views.thumbnail_px(self.view_mode)
        if not px and self._small_thumbnails_wanted():
            px = max(16, views.icon_px(self.view_mode))
        if not px:
            return

        view = self.tree_view
        viewport = view.viewport().rect().adjusted(0, -px * 2, 0, px * 2)
        videos = self._video_thumbnails_wanted()
        serial = self.device_serial

        for row in range(self.tree_model.rowCount()):
            index = self.tree_model.index(row, 0)
            if not view.visualRect(index).intersects(viewport):
                continue
            name = self.tree_model.data(index)
            item = self._items_by_name.get(name)
            if not item or not thumbnails.can_thumbnail(name, False, videos):
                continue

            key = self._thumbs.key_for(serial, item, px)
            self._thumb_keys[key] = name
            pixmap = self._thumbs.request(serial, item, px)
            if pixmap is not None:
                self._apply_thumbnail(key, pixmap)

    def _apply_thumbnail(self, key, pixmap):
        """Put a finished thumbnail on its row, if that row is still shown."""
        if self._closing:
            return
        name = self._thumb_keys.get(key)
        if not name:
            return
        standard_item = self._items_by_name.get(name, {}).get('item')
        if standard_item is None:
            return
        try:
            marked = thumbnails.decorate(pixmap, badge=self._badge_for(name),
                                         video=thumbnails.is_video(name))
            standard_item.setIcon(QIcon(marked))
        except RuntimeError:
            pass

    def _setup_context_menu(self):
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self.show_context_menu)

    def _setup_shortcuts(self):
        self._shortcuts = []
        bindings = (
            (QKeySequence.StandardKey.Copy, self.copy_selected),
            (QKeySequence.StandardKey.Cut, self.cut_selected),
            (QKeySequence.StandardKey.Paste, self.paste_items),
            (QKeySequence("Ctrl+Shift+C"), self.copy_to_pc_clipboard),
            (QKeySequence("Ctrl+Shift+V"), self.paste_from_pc),
            (QKeySequence("Ctrl+Shift+P"), self.copy_paths_to_clipboard),
            (QKeySequence("Ctrl+D"), self.duplicate_selected),
            (QKeySequence("Ctrl+U"), self.upload_file_to_device),
            (QKeySequence("Ctrl+E"), self.download_selected_items),
            (QKeySequence(Qt.Key.Key_Delete), self._shortcut_delete),
            (QKeySequence(Qt.Key.Key_F2), self._shortcut_rename),
            (QKeySequence(Qt.Key.Key_F5), self.refresh_files),
            (QKeySequence(Qt.Key.Key_Backspace), self.go_up),
            (QKeySequence.StandardKey.SelectAll, self.select_all_items),
            (QKeySequence("Ctrl+I"), self.invert_selection),
        ) + tuple(
            (QKeySequence(f"Ctrl+{number}"), lambda m=mode: self.set_view_mode(m))
            for number, (mode, _, _) in enumerate(views.VIEW_MODES, start=1)
        )
        for key, handler in bindings:
            shortcut = QShortcut(key, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    def update_connection_status(self):
        if self.adb_handler.device_connected:
            self.conn_label.setText("🟢 Connected")
            self.conn_label.setStyleSheet("color: green; font-size: 10px; padding: 0 8px 2px 8px;")
        else:
            self.conn_label.setText("🔴 Disconnected")
            self.conn_label.setStyleSheet("color: red; font-size: 10px; padding: 0 8px 2px 8px;")

    

    def update_path_display(self):
        self.path_display.setText(self.current_path)
        self.back_btn.setEnabled(len(self.path_history) > 1)

    def go_up(self):
        parent = "/".join(self.current_path.rstrip("/").split("/")[:-1]) or self.root_path
        if parent != self.current_path:
            self.current_path = parent
            self.clear_search_on_navigation()
            self.update_path_display()
            self.refresh_files()

    def go_back(self):
        if len(self.path_history) > 1:
            self.path_history.pop()
            self.current_path = self.path_history[-1]
            self.clear_search_on_navigation()
            self.update_path_display()
            self.refresh_files()

    def clear_search_on_navigation(self):
        if self.search_bar.text():
            self.search_bar.blockSignals(True)
            self.search_bar.setText("")
            self.search_bar.blockSignals(False)
        self.apply_search_filter()

    

    def _selection_changed(self, *args):
        """Report the selection the way a file manager's status bar does."""
        if self._closing:
            return
        names = self._selected_names()
        self.to_pc_btn.setEnabled(bool(names))
        self.to_pc_btn.setToolTip(
            "Download the selected files and folders to this PC (Ctrl+E)" if names
            else "Select files or folders first, then download them here (Ctrl+E)")

        if not names:
            total = len(self.all_files or [])
            self._note(f"{total} items")
            return

        chosen = [self._items_by_name.get(n) for n in names]
        files = [c for c in chosen if c and not c['is_dir']]
        folders = [c for c in chosen if c and c['is_dir']]
        size = sum(c['size'] for c in files)

        parts = []
        if folders:
            parts.append(f"{len(folders)} folder{'s' if len(folders) > 1 else ''}")
        if files:
            parts.append(f"{len(files)} file{'s' if len(files) > 1 else ''}")
        summary = " and ".join(parts) if parts else f"{len(names)} items"
        if size:
            summary += f"  -  {self.format_size(size)}"
        self._note(f"{summary} selected  -  \u2193 To PC, Ctrl+C to copy, "
                   f"right-click for more")

    def _on_host_clipboard_changed(self):
        """Remember when the PC clipboard last changed, so Ctrl+V can choose
        between the device clipboard and files copied in Explorer."""
        if self._closing:
            return
        self._host_clipboard_time = time.monotonic()

    def _report_error(self, message):
        """Show why an operation failed, not just that it failed."""
        detail = (self.adb_handler.last_error or "").strip()
        first_line = detail.splitlines()[0] if detail else ""
        if len(first_line) > 160:
            first_line = first_line[:157] + "..."
        self.status_label.setText(f"{message}: {first_line}" if first_line else message)
        self.status_label.setToolTip(detail)

    def refresh_files(self):
        if self._closing:
            return
        if not self.adb_handler.device_connected:
            self.status_label.setText("No ADB device")
            return
        if self._loading:
            self._refresh_pending = True
            return

        # Coming back to the same folder should not throw away the place
        # the user had scrolled to, which matters in a folder of thousands.
        self._pending_restore = (self._capture_place()
                                 if self.current_path == self._loaded_path else None)

        self._loading = True
        self.status_label.setText(f"Loading {self.current_path}...")
        self.tree_model.clear()
        self.tree_model.setHorizontalHeaderLabels(['Name', 'Type', 'Size', 'Permissions', 'Modified'])
        loading_item = QStandardItem("Loading...")
        loading_item.setEnabled(False)
        self.tree_model.appendRow([loading_item])

        def on_files(files):
            self._loading = False
            if self._closing:
                return
            if files is not None:
                self.all_files = files
                self.apply_search_filter()
                # Keep the result of the last operation visible next to the count.
                self._loaded_path = self.current_path
                place, self._pending_restore = self._pending_restore, None
                if place:
                    # After the rows are laid out, which an icon view needs.
                    QTimer.singleShot(0, lambda p=place: self._restore_place(p))

                note = self._post_refresh_note
                self._post_refresh_note = None
                count = f"{len(self.all_files)} items"
                self.status_label.setText(f"{note} - {count}" if note else f"Found {count}")
            else:
                self._report_error("Failed to list directory")
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh_files()

        task = WorkerThread(
            f"List {os.path.basename(self.current_path.rstrip('/')) or '/'}",
            self.adb_handler.list_directory, self.current_path, self.use_root,
        )
        self._refresh_task = task

        def clear_refresh_task(_=None):
            if getattr(self, '_refresh_task', None) is task:
                self._refresh_task = None

        task.finished_signal.connect(on_files)
        task.finished_signal.connect(clear_refresh_task)
        task.start()

    def _capture_place(self):
        """Where the user is in this folder: the top row, selection and all."""
        view = self.tree_view
        model = self.tree_model
        top = view.indexAt(view.viewport().rect().topLeft())
        current = view.currentIndex()
        return {
            'top': model.data(top.siblingAtColumn(0)) if top.isValid() else None,
            'scroll': view.verticalScrollBar().value(),
            'selected': set(self._selected_names()),
            'current': model.data(current.siblingAtColumn(0)) if current.isValid() else None,
        }

    def _restore_place(self, place):
        """Put the view back where it was, as far as the folder still allows."""
        if not place or self._closing:
            return
        view = self.tree_view
        model = self.tree_model
        rows = {model.data(model.index(row, 0)): row for row in range(model.rowCount())}

        selection = view.selectionModel()
        selection.clearSelection()
        flags = (QItemSelectionModel.SelectionFlag.Select
                 | QItemSelectionModel.SelectionFlag.Rows)
        for name in place['selected']:
            if name in rows:
                selection.select(model.index(rows[name], 0), flags)
        if place['current'] in rows:
            selection.setCurrentIndex(model.index(rows[place['current']], 0),
                                      QItemSelectionModel.SelectionFlag.NoUpdate)

        top = place.get('top')
        if top in rows:
            view.scrollTo(model.index(rows[top], 0),
                          QAbstractItemView.ScrollHint.PositionAtTop)
        else:
            bar = view.verticalScrollBar()
            bar.setValue(min(place.get('scroll', 0), bar.maximum()))

    def prepare_for_close(self):
        """Keep the panel alive until its workers have stopped.

        A transfer worker still holds this panel's widgets, and a queued
        progress signal arriving after deletion would abort the process, so
        every task is cancelled and waited for before deleteLater().
        """
        if self._closing:
            return

        self._closing = True
        self._refresh_pending = False
        self.hide()

        pending = [t for t in self._transfer_tasks if t is not None and t.isRunning()]
        for task in pending:
            try:
                task.cancel()
            except Exception:
                pass

        if self._refresh_task is not None and self._refresh_task.isRunning():
            pending.append(self._refresh_task)

        if not pending:
            self.deleteLater()
            return

        remaining = set(pending)

        def one_finished(task=None):
            for candidate in list(remaining):
                if not candidate.isRunning():
                    remaining.discard(candidate)
            if not remaining:
                self.deleteLater()

        for task in pending:
            task.finished.connect(one_finished)


    def apply_search_filter(self):
        if not hasattr(self, 'all_files') or self.all_files is None:
            return
        search_text = self.search_bar.text().lower()
        filtered = [f for f in self.all_files if search_text in f.name.lower()]
        self.populate_view(filtered)

    def populate_view(self, files):
        self.tree_model.clear()
        self.tree_model.setHorizontalHeaderLabels(['Name', 'Type', 'Size', 'Permissions', 'Modified'])
        self._items_by_name = {}
        self._thumb_keys = {}
        # Results for the previous folder are no longer wanted.
        self._thumbs.new_generation()
        if files is None:
            return

        current_normalized = "/" if self.current_path == "/" else self.current_path.rstrip("/")
        if current_normalized != self.root_path:
            parent_item = QStandardItem("..")
            parent_item.setIcon(self._folder_icon)
            self.tree_model.appendRow([parent_item, QStandardItem("Directory"),
                                       QStandardItem(""), QStandardItem(""), QStandardItem("")])

        for file_item in self._sorted(files):
            name_item = QStandardItem(file_item.name)
            if file_item.is_dir:
                name_item.setIcon(self._folder_icon)
            else:
                name_item.setIcon(self._icon_for(file_item.name))
            self._items_by_name[file_item.name] = {
                'name': file_item.name,
                'path': file_item.path,
                'size': file_item.size,
                'modified': file_item.date_modified,
                'is_dir': file_item.is_dir,
                'item': name_item,
            }
            self.tree_model.appendRow([
                name_item,
                QStandardItem("Directory" if file_item.is_dir else "File"),
                QStandardItem(str(file_item.size) if not file_item.is_dir else ""),
                QStandardItem(file_item.permissions),
                QStandardItem(file_item.date_modified),
            ])
        for i in range(5):
            self._tree.resizeColumnToContents(i)
        self.path_display.update_completions(files)
        self._schedule_thumbnails(immediate=True)

    # ------------------------------------------------------------ sorting --

    SORT_COLUMNS = {0: 'name', 1: 'type', 2: 'size', 4: 'date'}
    SORT_LABELS = (('name', 'Name'), ('size', 'Size'), ('type', 'Type'),
                   ('date', 'Date modified'))

    def _sorted(self, files):
        """Folders first, then whichever order the user asked for."""
        def key(item):
            if self.sort_key == 'size':
                return (0 if item.is_dir else 1, item.size, item.name.lower())
            if self.sort_key == 'date':
                return (0 if item.is_dir else 1, self._sortable_date(item.date_modified),
                        item.name.lower())
            if self.sort_key == 'type':
                extension = os.path.splitext(item.name)[1].lower()
                return (0 if item.is_dir else 1, extension, item.name.lower())
            return (0 if item.is_dir else 1, item.name.lower())

        folders = sorted([f for f in files if f.is_dir], key=key,
                         reverse=self.sort_desc)
        rest = sorted([f for f in files if not f.is_dir], key=key,
                      reverse=self.sort_desc)
        return folders + rest

    MONTHS = {m: f"{i + 1:02d}" for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"))}

    @classmethod
    def _sortable_date(cls, text):
        """Make both date styles a device can print sort chronologically.

        Most report "2026-01-02 10:00", which already sorts, but a busybox
        shell prints "Jan 2 10:00" or "Jan 2 2026", which would sort
        alphabetically by month name.
        """
        if not text:
            return ""
        parts = text.split()
        month = cls.MONTHS.get(parts[0][:3].lower()) if parts else None
        if month is None:
            return text
        day = parts[1].zfill(2) if len(parts) > 1 else "00"
        rest = parts[2] if len(parts) > 2 else ""
        if ":" in rest:                       # this year, time given
            return f"9999-{month}-{day} {rest}"
        return f"{rest or '0000'}-{month}-{day}"

    def set_sort(self, key, descending=None):
        if descending is None:
            descending = (not self.sort_desc) if key == self.sort_key else False
        self.sort_key, self.sort_desc = key, descending
        self._settings().setValue("sort_key", key)
        self._settings().setValue("sort_desc", descending)
        self._update_sort_indicator()
        if self.all_files:
            self.apply_search_filter()

    def _update_sort_indicator(self):
        self._update_sort_button()
        header = self._tree.header()
        column = next((c for c, k in self.SORT_COLUMNS.items() if k == self.sort_key), 0)
        header.setSortIndicator(
            column,
            Qt.SortOrder.DescendingOrder if self.sort_desc else Qt.SortOrder.AscendingOrder)
        header.setSortIndicatorShown(True)

    def _header_clicked(self, column):
        key = self.SORT_COLUMNS.get(column)
        if key:
            self.set_sort(key)

    # ---------------------------------------------------------- selection --

    def select_all_items(self):
        view = self.tree_view
        view.selectAll()
        self._drop_parent_row_from_selection()

    def invert_selection(self):
        model = self.tree_view.selectionModel()
        chosen = {index.row() for index in self.selected_rows()}
        model.clearSelection()
        flags = (QItemSelectionModel.SelectionFlag.Select
                 | QItemSelectionModel.SelectionFlag.Rows)
        for row in range(self.tree_model.rowCount()):
            if row not in chosen:
                model.select(self.tree_model.index(row, 0), flags)
        self._drop_parent_row_from_selection()

    def _drop_parent_row_from_selection(self):
        """".." is navigation, so never leave it selected."""
        model = self.tree_view.selectionModel()
        for index in list(self.selected_rows()):
            if self.tree_model.data(index.siblingAtColumn(0)) == "..":
                model.select(index,
                             QItemSelectionModel.SelectionFlag.Deselect
                             | QItemSelectionModel.SelectionFlag.Rows)

    def _icon_for(self, name):
        """The icon the system gives this kind of file, cached per extension."""
        ext = os.path.splitext(name)[1].lower()
        if ext not in self._icon_cache:
            icon = self._icon_provider.icon(QFileInfo(f"x{ext}"))
            self._icon_cache[ext] = icon if not icon.isNull() else self._file_icon
        return self._icon_cache[ext]

    def _badge_for(self, name):
        """The program icon to stamp on a thumbnail, if it says anything.

        An unregistered extension comes back as the plain page icon, which
        would only clutter a picture, so that one is left off.
        """
        icon = self._icon_for(name)
        if icon is self._file_icon:
            return None
        if self._generic_icon_key is None:
            self._generic_icon_key = self._icon_key(self._file_icon)
        return None if self._icon_key(icon) == self._generic_icon_key else icon

    @staticmethod
    def _icon_key(icon):
        pixmap = icon.pixmap(32, 32)
        if pixmap.isNull():
            return None
        image = pixmap.toImage()
        return bytes(image.constBits().asarray(image.sizeInBytes()))

    def handle_double_click(self, index):
        name_index = self.tree_model.index(index.row(), 0)
        type_index = self.tree_model.index(index.row(), 1)
        item_name = self.tree_model.data(name_index)
        item_type = self.tree_model.data(type_index)
        if not item_name:
            return
        if item_type == "Directory":
            new_path = ""
            if item_name == "..":
                parent_path = "/".join(self.current_path.rstrip("/").split("/")[:-1])
                new_path = parent_path if parent_path else self.root_path
            else:
                new_path = f"{self.current_path.rstrip('/')}/{item_name}"
            if new_path and new_path != self.current_path:
                self.current_path = new_path
                self.path_history.append(self.current_path)
                self.clear_search_on_navigation()
                self.update_path_display()
                self.refresh_files()
        elif item_type == "File":
            self.open_file_on_host(item_name)

    

    def get_selected_items(self):
        selected_items = []
        for index in self.selected_rows():
            name = self.tree_model.itemFromIndex(index.siblingAtColumn(0)).text()
            item_type = self.tree_model.itemFromIndex(index.siblingAtColumn(1)).text()
            if not name or name == "..":
                # The parent row is navigation, not an item to act on.
                continue
            selected_items.append({
                'name': name,
                'path': f"{self.current_path.rstrip('/')}/{name}",
                'is_dir': item_type.lower() == 'directory'
            })
        return selected_items




    def show_context_menu(self, position):
        """An Explorer-style menu: what it offers follows what is selected."""
        index = self.tree_view.indexAt(position)

        if index.isValid():
            # Right-clicking outside the selection moves the selection there,
            # so the menu always acts on what is under the cursor.
            if index.row() not in {i.row() for i in self.selected_rows()}:
                self.tree_view.selectionModel().select(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows)
        else:
            self.tree_view.clearSelection()

        names = self._selected_names()
        menu = QMenu(self)

        if not names:
            self._build_background_menu(menu)
        else:
            self._build_selection_menu(menu, names)

        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _add_paste_entries(self, menu):
        """Paste from another panel, or from the PC - whichever is waiting."""
        clip, _, clip_panel = self._panel_clipboard()
        host_files = transfer.host_clipboard_files()
        added = False

        if clip and clip.get('items'):
            label = "Paste" if clip.get('operation') == 'copy' else "Paste (move)"
            if clip_panel is not self:
                label = f"{label} from {clip_panel.device_name}"
            menu.addAction(f"{label}	Ctrl+V", self.paste_items)
            added = True
        if host_files:
            count = len(host_files)
            menu.addAction(f"Paste {count} item(s) from PC	Ctrl+Shift+V", self.paste_from_pc)
            added = True
        return added

    def _build_background_menu(self, menu):
        """The menu for empty space: about this folder and this view."""
        view_menu = menu.addMenu("View")
        for number, (mode, label, _) in enumerate(views.VIEW_MODES, start=1):
            action = view_menu.addAction(f"{label}	Ctrl+{number}")
            action.setCheckable(True)
            action.setChecked(mode == self.view_mode)
            action.triggered.connect(lambda checked=False, m=mode: self.set_view_mode(m))

        sort_menu = menu.addMenu("Sort by")
        for key, label in self.SORT_LABELS:
            action = sort_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self.sort_key)
            action.triggered.connect(lambda checked=False, k=key: self.set_sort(k))
        sort_menu.addSeparator()
        for descending, label in ((False, "Ascending"), (True, "Descending")):
            action = sort_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(descending == self.sort_desc)
            action.triggered.connect(
                lambda checked=False, d=descending: self.set_sort(self.sort_key, d))

        menu.addAction(f"Refresh	F5", self.refresh_files)
        menu.addSeparator()

        if self._add_paste_entries(menu):
            menu.addSeparator()

        new_menu = menu.addMenu("New")
        new_menu.addAction("Folder", self.create_new_folder)
        new_menu.addAction("File", self.create_new_file)

        upload_menu = menu.addMenu("Upload from PC")
        upload_menu.addAction(f"Files...	Ctrl+U", self.upload_file_to_device)
        upload_menu.addAction("Folder...", self.upload_folder_to_device)
        upload_menu.addAction("Sync folder (skip unchanged)...", self.sync_folder_to_device)

        menu.addSeparator()
        menu.addAction(f"Select all	Ctrl+A", self.select_all_items)
        menu.addAction(f"Copy folder path	Ctrl+Shift+P", self.copy_paths_to_clipboard)
        menu.addSeparator()
        menu.addAction("Properties", self.show_properties)

    def _build_selection_menu(self, menu, names):
        """The menu for one or more selected items."""
        single = len(names) == 1
        name = names[0] if single else None
        item = self._items_by_name.get(name) if single else None
        is_dir = bool(item and item['is_dir'])

        if single:
            if is_dir:
                menu.addAction("Open", lambda n=name: self.navigate_to_child(n))
            else:
                menu.addAction("Open on PC", lambda n=name: self.open_file_on_host(n))
            menu.addSeparator()

        menu.addAction(f"Download to PC...	Ctrl+E", self.download_selected_items)
        menu.addAction(f"Copy to PC clipboard	Ctrl+Shift+C", self.copy_to_pc_clipboard)
        menu.addAction("Prepare for dragging to the PC", self.prepare_for_dragging)
        send_menu = menu.addMenu("Send to device")
        self._populate_send_to_menu(send_menu, self.selected_rows(), None)
        menu.addSeparator()

        menu.addAction(f"Copy	Ctrl+C", self.copy_selected)
        menu.addAction(f"Cut	Ctrl+X", self.cut_selected)
        if self._add_paste_entries(menu):
            pass
        menu.addAction(f"Duplicate	Ctrl+D", self.duplicate_selected)
        menu.addSeparator()

        menu.addAction("Copy to folder...", self.copy_selected_to)
        menu.addAction("Move to folder...", self.move_selected_to)
        menu.addSeparator()

        if single:
            menu.addAction(f"Rename	F2", lambda n=name: self.rename_item(n))
        else:
            menu.addAction("Batch rename...", self.batch_rename_selected)
        menu.addAction(f"Delete	Del", self.delete_selected_items)
        menu.addSeparator()

        menu.addAction(f"Copy path	Ctrl+Shift+P", self.copy_paths_to_clipboard)
        menu.addAction("Properties", self.show_properties)

    def navigate_to_child(self, name):
        self.current_path = f"{self.current_path.rstrip('/')}/{name}"
        self.path_history.append(self.current_path)
        self.clear_search_on_navigation()
        self.update_path_display()
        self.refresh_files()

    def show_properties(self):
        """Counts, total size and the path - what Explorer shows."""
        names = self._selected_names()
        if names:
            chosen = [self._items_by_name.get(n) for n in names]
            chosen = [c for c in chosen if c]
            folders = [c for c in chosen if c['is_dir']]
            files = [c for c in chosen if not c['is_dir']]
            title = chosen[0]['name'] if len(chosen) == 1 else f"{len(chosen)} items"
            lines = [f"Location: {self._dest_dir()}"]
            if len(chosen) == 1:
                lines.append(f"Type: {'Folder' if chosen[0]['is_dir'] else 'File'}")
                lines.append(f"Modified: {chosen[0]['modified']}")
            if files:
                lines.append(f"Files: {len(files)}  ({self.format_size(sum(c['size'] for c in files))})")
            if folders:
                lines.append(f"Folders: {len(folders)}")
                measured = self.adb_handler.disk_usage_many([c['path'] for c in folders])
                lines.append("Folder contents: "
                             + (self.format_size(measured) if measured is not None
                                else "could not be measured"))
        else:
            listing = self.all_files or []
            title = self._dest_dir()
            files = [f for f in listing if not f.is_dir]
            folders = [f for f in listing if f.is_dir]
            lines = [f"Path: {self._dest_dir()}",
                     f"Files: {len(files)}  ({self.format_size(sum(f.size for f in files))})",
                     f"Folders: {len(folders)}"]

        QMessageBox.information(self, title, "\n".join(lines))


    def get_file_size_from_row(self, row):
        size_index = self.tree_model.index(row, 2)
        size_text = self.tree_model.data(size_index)
        return int(size_text) if size_text and size_text.isdigit() else 0

    def is_dir_from_row(self, row):
        type_index = self.tree_model.index(row, 1)
        return self.tree_model.data(type_index) == "Directory"

    def format_size(self, bytes_size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024.0:
                return f"{bytes_size:.1f} {unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.1f} TB"

    def open_file_on_host(self, filename):
        if not self.adb_handler.device_connected:
            self.status_label.setText("No connection")
            return
        remote_path = f"{self.current_path.rstrip('/')}/{filename}"
        temp_dir = tempfile.gettempdir()
        local_path = os.path.join(temp_dir, filename)

        def run_pull():
            return self.adb_handler.pull_file(remote_path, local_path)

        def on_pulled(ok):
            if not ok:
                self._report_error(f"Failed to open {filename}")
                return
            try:
                if sys.platform.startswith('win'):
                    os.startfile(local_path)
                elif sys.platform.startswith('darwin'):
                    subprocess.run(['open', local_path])
                else:
                    subprocess.run(['xdg-open', local_path])
                self.status_label.setText(f"Opened {filename}")
            except Exception:
                self.status_label.setText(f"Failed to open file")

        self._run_modal("Downloading file", run_pull, on_done=on_pulled)

    def copy_file_to(self, filename):
        if not self.adb_handler.device_connected:
            return
        remote_path = f"{self.current_path.rstrip('/')}/{filename}"
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", filename)
        if not save_path:
            return
        self._run_transfer("Copying file", remote_path, save_path,
                           f"File copied: {save_path}", f"Failed to copy {filename}")

    def copy_folder_to(self, foldername):
        if not self.adb_handler.device_connected:
            return
        remote_path = f"{self.current_path.rstrip('/')}/{foldername}"
        save_dir = QFileDialog.getExistingDirectory(self, "Select Destination")
        if not save_dir:
            return
        local_path = os.path.join(save_dir, foldername)
        self._run_transfer("Copying folder", remote_path, local_path,
                           f"Folder copied: {local_path}", f"Failed to copy {foldername}")

    def _run_transfer(self, title, src, dst, success_msg, error_msg):
        fn = self.adb_handler.pull_file
        self._run_modal(
            title, fn, src, dst,
            on_done=lambda _: self.status_label.setText(success_msg),
            on_error=lambda: self._report_error(error_msg),
        )

    def rename_item(self, old_name):
        if not self.adb_handler.device_connected:
            return
        new_name, ok = QInputDialog.getText(self, "Rename", f"New name for '{old_name}':")
        if ok and new_name and new_name != old_name:
            old_path = f"{self.current_path.rstrip('/')}/{old_name}"
            new_path = f"{self.current_path.rstrip('/')}/{new_name}"
            if self.adb_handler.rename_item(old_path, new_path):
                self.status_label.setText(f"Renamed to '{new_name}'")
                self.refresh_files()
            else:
                self._report_error("Rename failed")

    def delete_item(self, name, is_dir=False):
        if not self.adb_handler.device_connected:
            return
        reply = QMessageBox.question(
            self, "Delete",
            f"Delete '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            path = f"{self.current_path.rstrip('/')}/{name}"
            if self.adb_handler.delete_item(path, is_dir):
                self.status_label.setText(f"Deleted '{name}'")
                self.refresh_files()
            else:
                self._report_error("Delete failed")

    def _shortcut_delete(self):
        indexes = self.selected_rows()
        if indexes:
            self.delete_selected_items()

    def _shortcut_rename(self):
        indexes = self.selected_rows()
        if len(indexes) != 1:
            return
        name = self.tree_model.data(self.tree_model.index(indexes[0].row(), 0))
        if not name or name == "..":
            return
        self.rename_item(name)

    def delete_selected_items(self):
        if not self.adb_handler.device_connected:
            return
        indexes = self.selected_rows()
        if not indexes:
            return
        names_types = []
        for index in indexes:
            name = self.tree_model.data(self.tree_model.index(index.row(), 0))
            typ = self.tree_model.data(self.tree_model.index(index.row(), 1))
            if name and name != "..":
                names_types.append((name, typ == "Directory"))
        if not names_types:
            return
        reply = QMessageBox.question(
            self, "Delete",
            f"Delete {len(names_types)} selected items?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            def run_delete():
                count = 0
                for name, is_dir in names_types:
                    path = f"{self.current_path.rstrip('/')}/{name}"
                    if self.adb_handler.delete_item(path, is_dir):
                        count += 1
                return count

            def on_done(count):
                self.status_label.setText(f"Deleted {count} items")
                self.refresh_files()

            self._run_modal(f"Delete {len(names_types)} items", run_delete, on_done=on_done)

    def create_new_file(self):
        if not self.adb_handler.device_connected:
            return
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        if ok and name:
            path = f"{self.current_path.rstrip('/')}/{name}"
            if self.adb_handler.create_file(path):
                self.status_label.setText(f"Created '{name}'")
                self.refresh_files()
            else:
                self._report_error("Create failed")

    def create_new_folder(self):
        if not self.adb_handler.device_connected:
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name:
            path = f"{self.current_path.rstrip('/')}/{name}"
            if self.adb_handler.create_folder(path):
                self.status_label.setText(f"Created '{name}'")
                self.refresh_files()
            else:
                self._report_error("Create failed")





    def batch_rename_selected(self):
        if not self.adb_handler.device_connected:
            return
        indexes = self.selected_rows()
        if len(indexes) < 2:
            QMessageBox.information(self, "Info", "Select 2+ items to batch rename.")
            return
        base, ok = QInputDialog.getText(self, "Batch Rename", "Base name:")
        if not ok or not base:
            return
        padding = len(str(len(indexes)))

        def run():
            count = 0
            for i, index in enumerate(indexes):
                old = self.tree_model.data(self.tree_model.index(index.row(), 0))
                new = f"{base}{str(i+1).zfill(padding)}"
                old_path = f"{self.current_path.rstrip('/')}/{old}"
                new_path = f"{self.current_path.rstrip('/')}/{new}"
                if self.adb_handler.rename_item(old_path, new_path):
                    count += 1
            return count

        def on_done(count):
            self.status_label.setText(f"Renamed {count} items")
            self.refresh_files()

        self._run_modal(f"Rename {len(indexes)} items", run, on_done=on_done)

    


    def _run_modal(self, title, fn, *args, on_done=None, on_error=None, refresh=False,
                   ctx=None):
        """Run work in a thread behind a cancellable progress dialog.

        `ctx` is a TransferContext: its progress lines drive the label and the
        Cancel button both flips its flag and kills the adb child, so Cancel
        actually stops the transfer instead of only hiding the dialog.
        """
        task = WorkerThread(title, fn, *args)
        if ctx is not None:
            task.cancel_targets = list(getattr(ctx, 'handlers', []))
        self._transfer_tasks.add(task)

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setFixedSize(420, 140)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 16, 16, 16)

        label = QLabel(f"{title}...")
        label.setWordWrap(True)
        layout.addWidget(label)

        bar = QProgressBar()
        bar.setRange(0, 0)
        layout.addWidget(bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        finished = [False]

        if ctx is not None:
            def show_progress(text):
                # Queued from the worker thread, so the label may already be
                # gone by the time this runs.
                if finished[0]:
                    return
                try:
                    label.setText(text[-160:])
                except RuntimeError:
                    pass

            ctx.message.connect(show_progress)

        def cancel_task():
            cancel_btn.setEnabled(False)
            label.setText("Canceling...")
            if ctx is not None:
                ctx.cancel()
            task.cancel()

        cancel_btn.clicked.connect(cancel_task)

        def cleanup():
            finished[0] = True
            if ctx is not None:
                try:
                    ctx.message.disconnect(show_progress)
                except (TypeError, RuntimeError):
                    pass
            self._transfer_tasks.discard(task)
            try:
                dlg.close()
                dlg.deleteLater()
            except RuntimeError:
                pass

        def handle_finished(val):
            cleanup()
            if self._closing:
                return
            cancelled = bool(ctx is not None and ctx.cancelled) or task.is_cancelled()
            if cancelled and not getattr(val, 'done', 0):
                self._note("Canceled")
                return
            if bool(val) if val is not None else False:
                if on_done:
                    on_done(val)
                if refresh:
                    self.refresh_files()
            elif on_error:
                on_error()

        def handle_error(msg):
            cleanup()
            if self._closing:
                return
            if on_error:
                on_error()

        task.finished_signal.connect(handle_finished)
        task.error_signal.connect(handle_error)
        dlg.show()
        task.start()
        return task



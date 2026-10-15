
# ADB Explorer
A graphical file manager for Android devices connected via ADB (Android Debug Bridge). Provides an intuitive interface for managing files on Android devices without needing to use command-line ADB commands or navigate through the device's UI.
## Features
- **Multi-device support** - Select multiple connected Android devices
- **Device-to-device transfers** - Copy files from one android device to another efficiently
- **Multi-Panel viewer** - Side-by-side panel view
- **Eight view modes** - Icons in four sizes, List, Details, Tiles and Content, with Ctrl+wheel zoom
- **Thumbnails** - Photos and video frames, fetched cheaply, cached, and needing nothing installed
- **Full file operations** - Push, pull, delete, rename, duplicate and copy/paste files
- **Clipboard that spans both worlds** - Copy on the device and paste in Explorer, or the reverse
- **Conflict handling** - Keep both, merge, replace or skip, with apply-to-all
- **Folder sync** - Re-upload a folder and only the changed files move
- **Cancellable transfers** - Cancel stops the running adb transfer, not just the dialog
- **Two-way file transfer** - Transfer files between computer and device easily
- **Familiar file explorer navigation** - Browse device storage like a desktop file manager
- **File search** - Quickly find files on your device
- **Batch operations** - Perform actions on multiple files at once
- **Drag-and-drop support** - Intuitive file transfers
- **App Installer** - Install APK or XAPK files
- **Wireless ADB** - Connect to devices over network (no USB required)

## Dragging
Drag-and-drop follows the rules Windows Explorer uses, so the same habits
work here:

| Gesture | What happens |
| --- | --- |
| Drag within one device | **Move** - the same storage, so nothing is duplicated |
| Drag to another device, or to the PC | **Copy** - a different storage |
| `Ctrl` + drag | Copy, wherever it is dropped |
| `Shift` + drag | Move, wherever it is dropped |
| Drag with the **right button**, from something already selected | A menu on release: Copy here, Move here, Cancel - the action that would have happened is in bold |

The cursor shows which it will be while you drag, because the panel accepts
the drag with that action and the system draws the matching cursor.

A drag only begins on an item that is **already selected**, and it then
carries the whole selection. Pressing anywhere else - an item that is not
selected, or empty space - sweeps out a selection instead, which is how
several items get picked in the first place. This is the one place the app
parts company with Explorer, where pressing an unselected file selects it and
drags it in one motion; here that gesture is kept for selecting.

Dragging out to Explorer, the desktop, or anything else that takes files
copies them there. The files have to be on disk before a drag can begin, so
a selection under 24 MB is fetched as it starts. A larger one still drags
between panels and into folders, and if such a drag is let go outside the
app, the panel offers to download the items instead - that route has no size
limit, and shows progress. If the place you drop into asks for a
move rather than a copy, the files are copied first and the panel then asks
before deleting the originals from the phone.

Files dragged **in** from the PC are always copied, never moved: a move would
have Windows delete your own files once the upload finished, which is not
something a transfer tool should do quietly.

## Selecting and the right-click menu
Selection behaves as it does in a file manager, in every view: click,
`Ctrl`+click to add, `Shift`+click for a range, a rubber band dragged over
empty space, `Ctrl+A` for everything and `Ctrl+I` to invert. The status bar
reports what is selected and how large it is, and `..` is never part of it.

The right-click menu follows what is under the cursor:

- **empty space** - View, Sort by, Refresh, New, Upload from PC, Select all,
  Copy folder path, Properties
- **one item** - Open (a folder here, a file on the PC), Download to PC, Copy
  to PC clipboard, Send to device, Copy/Cut/Duplicate, Copy or Move to
  folder, Rename, Delete, Copy path, Properties
- **several items** - the same, with Batch rename in place of Rename

Right-clicking an item outside the current selection moves the selection to
it first, so the menu always acts on what was clicked.

## Sorting
The toolbar's sort button shows what the list is sorted by and in which
direction, and its menu offers Name, Size, Type and Date modified, each way
round. The same choices are in the right-click menu, and a column header in
Details sorts by that column. The choice is remembered between runs.

Folders are always listed before files, and the sort runs inside each of the
two groups separately, as it does in Explorer.

## Looking at files
The app is dark by default; View > Dark Mode switches it and the choice is
kept in `config.ini` beside the program.

The view button in each panel's toolbar offers the same eight modes as
Explorer - Extra large / Large / Medium / Small icons, List, Details, Tiles
and Content - on `Ctrl+1` to `Ctrl+8`, or `Ctrl+wheel` to zoom through them.
The choice is remembered between runs, and each panel can show a different
one.

### Thumbnails
Pictures are read from the device, so they are fetched as cheaply as possible
and only for the rows actually on screen:

- **JPEG** - only the first 128 KB are read, which holds the thumbnail the
  camera embedded in the EXIF block. A 2.4 MB photo costs about 29 KB and a
  tenth of a second, and the EXIF rotation is applied so portrait photos are
  upright.
- **PNG, WebP, GIF, BMP** - fetched whole, but only below 2 MB.
- **Video** - decoded by Qt's multimedia backend, which ships inside PyQt6
  and therefore inside the packaged app: nothing has to be installed and
  nothing is downloaded. Instead of pulling a whole clip, the first
  megabyte and a half is read, which is all a phone's "faststart" recording
  needs. When the index sits at the end instead, as it does in a long
  recording, the app walks the file's boxes on the device to find exactly
  where it is and reads just that. Both pieces go into a sparse local file
  at their real offsets, so a 968 MB video costs a few megabytes and about
  four seconds. If an `ffmpeg` happens to be on `PATH` (or in `FFMPEG_PATH`)
  it is used instead, purely because it can run on a worker thread, but it
  is never required.

Every thumbnail is stamped with the icon of the program that opens that kind
of file, the way a desktop file manager does, and videos also carry a play
symbol - so a video frame is never mistaken for a photo.

Thumbnails are cached on disk under `%LOCALAPPDATA%\adb_explorer\thumbnails`
(`~/.cache/adb_explorer` elsewhere), keyed by path, size and timestamp, so a
folder costs nothing to revisit. The view menu can also turn video thumbnails
off, or turn thumbnails on for the List and Details views, where they are off
by default because those rows are small and every one costs a read.

## Moving files around
Every transfer works the way it does in a desktop file manager, and all of
them resolve name clashes through the same dialog:

| Choice | What it does |
| --- | --- |
| **Keep both** | Transfers under a free name such as `report (2).pdf`, checked against the whole destination folder |
| **Merge** (folders) | Adds the new contents to the existing folder and replaces only same-named files |
| **Replace** | Puts the new item in place first, then removes the old one; for a folder this discards its current contents, which the dialog says explicitly |
| **Skip** | Leaves the existing item alone |

"Apply this to the remaining items" answers the rest the same way. Nothing at
the destination is deleted until its replacement is in place, so an
interrupted replace leaves the original intact. If the destination cannot be
read, the transfer stops instead of assuming the folder is empty.

**From the PC to the device**
- **↑ From PC** in the panel's toolbar picks files to upload here
- Drag files or folders from Explorer/Finder onto a panel
- `Ctrl+V` in a panel after copying files in Explorer
- Right-click > Upload Files / Upload Folder
- Right-click > Sync Folder - uploads only what changed, so re-running is cheap

**From the device to the PC**
- Select what you want and press **↓ To PC** in the panel's toolbar, which
  asks where to put it. This is the same as `Ctrl+E` and as right-click >
  Download to PC, and it handles folders as well as files.
- Drag items out onto the desktop, Explorer or any app that takes files. The
  files have to be on disk before a drag can start, so a selection under
  24 MB is fetched as the drag begins; a larger one still drags between
  panels, and the status bar points at "To PC" instead.
- `Ctrl+Shift+C` puts the selection on the PC clipboard, then paste in Explorer
- `Ctrl+E` or right-click > Download to PC for a chosen folder (any size)
- Double-click a file to open it on the PC

**Inside one device**
- `Ctrl+C` / `Ctrl+X` / `Ctrl+V` between folders and between panels
- Drag inside a panel to move, hold `Ctrl` while dropping to copy
- `Ctrl+D` duplicates in place
- Right-click > Copy / Move to Device Folder for a folder picker

**Between two devices**
- Drag from one panel to another to copy, hold `Shift` while dropping to move.
  Dropping onto a folder row transfers into that folder.
- Copy in one panel and paste in the other
- Right-click > Send to Device > Copy/Move to <device>; a move asks first,
  since it deletes the originals

Files stream directly between devices without being staged on the PC.

Long transfers show the current file and can be cancelled, which stops the
adb transfer itself. If only some items make it, the status line says how many
failed and why, rather than reporting success.

### What a move checks before it deletes
Within one device a move is a rename, so nothing is copied and there is
nothing to check. Across storages - between two devices, or to the PC - a
move is a copy followed by a delete, and the link can drop in between. The
copy's size is therefore always compared against the original before the
original is removed, which costs one call and catches a truncated transfer;
if they differ, the original is kept and the status line says so.

The View menu can also turn on "Check contents before a move deletes", which
compares an MD5 of both sides. It is off by default because the device has
to read every byte, at roughly 45 MB/s - about twenty seconds each way for a
one-gigabyte video.

Refreshing a folder - after a transfer, or with F5 - keeps the place you had
scrolled to and what you had selected. It follows the row that was at the
top by name rather than by pixels, so it still lands correctly when the
folder's contents have changed.

### Keyboard shortcuts
| Shortcut | Action |
| --- | --- |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | Copy / Cut / Paste (device items, or files copied on the PC) |
| `Ctrl+A` / `Ctrl+I` | Select all / Invert the selection |
| `Ctrl+Shift+C` | Copy the selection to the PC clipboard |
| `Ctrl+Shift+V` | Paste files from the PC clipboard |
| `Ctrl+Shift+P` | Copy the full device path as text |
| `Ctrl+D` | Duplicate in place |
| `Ctrl+U` / `Ctrl+E` | Upload files / Download selection |
| `F2` / `Del` | Rename / Delete |
| `F5` / `Backspace` | Refresh / Go up |

Raise the 24 MB drag-out limit with `ADB_EXPLORER_DRAG_LIMIT_MB` if your
connection is fast.

### Tests
Both suites run against a real connected device and clean up after
themselves:

```bash
python tests/test_transfers.py   # copy, move, conflicts, cancel, failures
python tests/test_views.py       # view modes, thumbnails, cache, rotation
python tests/test_theme.py       # readable text in both themes (no device)
python tests/test_buttons.py     # buttons, selection, menus, sorting (no device)
python tests/test_dragdrop.py    # the Explorer drag rules (no device)
```

`test_transfers.py` covers the destructive paths deliberately - replace,
rollback, cancellation and partial failure - inside `/sdcard/adbx_cr_tests`.

## Installation
### Option 1: Download Executable (Windows)
Download the latest release from the [Releases](../../releases) page and run the `.exe` file.
### Option 2: Just run it (no build)
Double-click **`run.bat`**. It starts the app straight from the source: it
uses the local `venv` if there is one, falls back to a system Python that
already has PyQt6, and only if neither exists does it create an environment
and install PyQt6 once. Nothing is compiled or packaged, and later runs start
immediately.

### Option 3: Run from Source
**Requirements:**
- Python 3.10+
- PyQt6
- ADB installed and in system PATH
**Install dependencies:**
```bash
pip install PyQt6
```
**Run:**
```bash
python main.py
```
## Usage
1. **Connect your Android device** via USB with USB debugging enabled
2. **Launch the application**
3. **Select your device** from the device chooser (if multiple devices connected)
4. **Navigate and manage files** using the GUI:
   - **Push files**: Transfer files from computer to device
   - **Pull files**: Download files from device to computer
   - **Delete**: Remove files from device
   - **Rename**: Rename files on device
   - **Copy/Paste**: Copy files within the device
   - **Search**: Find files quickly using the search function
   - **Drag-and-drop**: Drop files directly into the application to transfer
   - **Batch operations**: Select multiple files for simultaneous operations
## Prerequisites
- ADB (Android Debug Bridge) must be installed and accessible from command line
- For USB connections: Android device with USB debugging enabled
- For wireless connections: Device and computer on the same network
ADB Explorer looks for `adb` on your `PATH` first, then in the usual SDK
locations (`%LOCALAPPDATA%\Android\Sdk\platform-tools` on Windows,
`~/Library/Android/sdk/platform-tools` on macOS, `~/Android/Sdk/platform-tools`
and `/usr/bin` on Linux). If it lives somewhere else, point `ADB_PATH` at the
executable:

```bash
# Windows (PowerShell)
$env:ADB_PATH = "C:\adb\adb.exe"
# macOS / Linux
export ADB_PATH=/opt/platform-tools/adb
```

If adb cannot be found at all, the app says so on startup instead of failing
silently. Non-standard ADB server endpoints are honoured through the usual
`ANDROID_ADB_SERVER_ADDRESS` / `ANDROID_ADB_SERVER_PORT` variables.

### Installing ADB
**Windows:**
- Download [Android SDK Platform Tools](https://developer.android.com/studio/releases/platform-tools)
- Extract and add to system PATH

**macOS:**
```bash
brew install android-platform-tools
```
**Linux:**
```bash
sudo apt-get install adb  # Ubuntu/Debian
```
### Enabling USB Debugging on Android
1. Go to **Settings** > **About Phone**
2. Tap **Build Number** 7 times to enable Developer Options
3. Go to **Settings** > **Developer Options**
4. Enable **USB Debugging**
## Technical Details
Built with:
- **Python** - Core application logic
- **PyQt6** - GUI framework, including the Qt multimedia backend used for
  video thumbnails
- **ADB** - Android Debug Bridge for device communication

### Building the executable
`ADB Explorer.spec` is the single description of the build; `build.bat` and
the release workflow both run it, so they cannot drift apart.

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean "ADB Explorer.spec"
```
The application uses subprocess calls to ADB commands and presents them through a familiar file explorer interface, making Android file management accessible without command-line knowledge.
## Known Limitations
- Some system directories may require root access
- File permissions depend on device settings
## License
MIT License
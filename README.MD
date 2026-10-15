
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
  nothing is downloaded. Instead of pulling a whole clip, the head is read,
  and the tail too if the index lives at the end; they are written into a
  sparse local file at their real offsets so a decoder can find one frame. A
  9.6 MB clip usually costs 1.5 MB and about a third of a second. If an
  `ffmpeg` happens to be on `PATH` (or in `FFMPEG_PATH`) it is used instead,
  purely because it can run on a worker thread, but it is never required.

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
- Hold `Alt` and drag items out onto the desktop, Explorer or any app that
  takes files. The files must be fetched before the drag can start, so this is
  limited to selections under 24 MB and only happens when `Alt` is held - a
  drag between panels stays instant and costs no adb calls.
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

### Keyboard shortcuts
| Shortcut | Action |
| --- | --- |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | Copy / Cut / Paste (device items, or files copied on the PC) |
| `Alt`+drag | Drag the selection out to the PC |
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
python tests/test_buttons.py     # the toolbar buttons do what they say (no device)
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
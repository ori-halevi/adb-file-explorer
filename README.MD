
# ADB Explorer
A graphical file manager for Android devices connected via ADB (Android Debug Bridge). Provides an intuitive interface for managing files on Android devices without needing to use command-line ADB commands or navigate through the device's UI.
## Features
- **Multi-device support** - Select multiple connected Android devices
- **Device-to-device transfers** - Copy files from one android device to another efficiently
- **Multi-Panel viewer** - Side-by-side panel view
- **Full file operations** - Push, pull, delete, rename, duplicate and copy/paste files
- **Clipboard that spans both worlds** - Copy on the device and paste in Explorer, or the reverse
- **Conflict handling** - Keep both, overwrite or skip, with apply-to-all
- **Folder sync** - Re-upload a folder and only the changed files move
- **Two-way file transfer** - Transfer files between computer and device easily
- **Familiar file explorer navigation** - Browse device storage like a desktop file manager
- **File search** - Quickly find files on your device
- **Batch operations** - Perform actions on multiple files at once
- **Drag-and-drop support** - Intuitive file transfers
- **App Installer** - Install APK or XAPK files
- **Wireless ADB** - Connect to devices over network (no USB required)

## Moving files around
Every transfer works the way it does in a desktop file manager, and all of them
resolve conflicts the same way: **Keep both** (adds "(2)" to the name),
**Overwrite** or **Skip**, with an "apply to the rest" option.

**From the PC to the device**
- Drag files or folders from Explorer/Finder onto a panel
- `Ctrl+V` in a panel after copying files in Explorer
- Right-click > Upload Files / Upload Folder
- Right-click > Sync Folder - uploads only what changed, so re-running is cheap

**From the device to the PC**
- Drag items out of a panel onto the desktop, Explorer or any app that takes files
- `Ctrl+Shift+C` puts the selection on the PC clipboard, then paste in Explorer
- `Ctrl+E` or right-click > Download to PC for a chosen folder (any size)
- Double-click a file to open it on the PC

**Inside one device**
- `Ctrl+C` / `Ctrl+X` / `Ctrl+V` between folders and between panels
- Drag inside a panel to move, hold `Ctrl` while dropping to copy
- `Ctrl+D` duplicates in place
- Right-click > Copy / Move to Device Folder for a folder picker

**Between two devices**
- Drag from one panel to another to copy, hold `Shift` while dropping to move
- Copy in one panel and paste in the other
- Right-click > Send to Device > Copy/Move to <device>

Files stream directly between devices without being staged on the PC.

### Keyboard shortcuts
| Shortcut | Action |
| --- | --- |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | Copy / Cut / Paste (device items, or files copied on the PC) |
| `Ctrl+Shift+C` | Copy the selection to the PC clipboard |
| `Ctrl+Shift+V` | Paste files from the PC clipboard |
| `Ctrl+Shift+P` | Copy the full device path as text |
| `Ctrl+D` | Duplicate in place |
| `Ctrl+U` / `Ctrl+E` | Upload files / Download selection |
| `F2` / `Del` | Rename / Delete |
| `F5` / `Backspace` | Refresh / Go up |

Dragging to the PC has to fetch the files before the drag can begin, so it is
limited to selections under 24 MB; larger ones go through Download to PC. Raise
the limit with `ADB_EXPLORER_DRAG_LIMIT_MB` if your connection is fast.

## Installation
### Option 1: Download Executable (Windows)
Download the latest release from the [Releases](../../releases) page and run the `.exe` file.
### Option 2: Run from Source
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
- **PyQt6** - GUI framework
- **ADB** - Android Debug Bridge for device communication
The application uses subprocess calls to ADB commands and presents them through a familiar file explorer interface, making Android file management accessible without command-line knowledge.
## Known Limitations
- Some system directories may require root access
- File permissions depend on device settings
## License
MIT License
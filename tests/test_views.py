"""Live view-mode and thumbnail tests - they need one device connected.

    python tests/test_views.py

Covers the eight view modes, the cheap thumbnail routes (EXIF for JPEG, head
plus tail for video), EXIF rotation, lazy loading, and the on-disk cache.
"""
import atexit
import os
import sys
import time
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QImage

from handler import ADBHandler
from ui.theme import LIGHT
from ui.multi_device_window import MultiDeviceWindow
from ui import thumbnails, views

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(LIGHT)

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  PASS  ' if condition else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))


def pump(seconds=1.0, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False


# The app reads these too, so put back whatever the user had afterwards.
SETTINGS = QSettings("ADB Explorer", "ADB Explorer")
SAVED = {key: SETTINGS.value(key) for key in SETTINGS.allKeys()}


def restore_settings():
    SETTINGS.clear()
    for key, value in SAVED.items():
        SETTINGS.setValue(key, value)
    SETTINGS.sync()


atexit.register(restore_settings)
SETTINGS.clear()

window = MultiDeviceWindow()
window.resize(1100, 700)
window.show()
pump(4)
panel = window.device_panels[0]
h = panel.adb_handler

BASE = "/sdcard/adbx_view_tests"
print("\n=== 8. queued videos keep their own frames ===")
# A shared player handed the next clip the previous clip's frame, so videos
# showed each other's thumbnails.
import hashlib

clips = [f for f in (h.list_directory("/sdcard/DCIM/Camera") or [])
         if f.name.lower().endswith('.mp4') and f.size > 1_000_000][:3]
if len(clips) >= 2 and thumbnails.QT_VIDEO:
    def fingerprint(image):
        if image is None:
            return None
        small = image.scaled(8, 8)
        return hashlib.md5(bytes(small.constBits().asarray(
            small.sizeInBytes()))).hexdigest()[:10]

    local = []
    for clip in clips:
        folder = tempfile.mkdtemp(prefix='adbx_clip_test_')
        local.append((clip.name,
                      thumbnails.fetch_video_locally(h.device_serial, clip.path,
                                                     clip.size, folder)))

    alone = {}
    for name, path in local:
        grabber = thumbnails.VideoFrameGrabber()
        result = {}
        grabber.finished.connect(lambda key, image: result.update({'image': image}))
        grabber.grab(name, path, 96)
        pump(20, until=lambda: 'image' in result)
        alone[name] = fingerprint(result.get('image'))

    queued = {}
    grabber = thumbnails.VideoFrameGrabber()
    grabber.finished.connect(
        lambda key, image: queued.update({key: fingerprint(image)}))
    for name, path in local:
        grabber.grab(name, path, 96)
    pump(60, until=lambda: len(queued) == len(local))

    check("every queued clip was decoded", len(queued) == len(local),
          f"{len(queued)} of {len(local)}")
    mismatched = [n for n, _ in local if queued.get(n) != alone.get(n)]
    check("each clip kept its own frame", not mismatched, str(mismatched))
    check("different clips gave different frames",
          len({v for v in alone.values() if v}) == len([v for v in alone.values() if v]),
          str(list(alone.values())))
else:
    print("  (not enough video on the device to check)")

h.delete_item(BASE, is_dir=True, missing_ok=True)
h.create_folder(BASE)

# a real photo to work with, copied into our own folder
photos = [f for f in (h.list_directory("/sdcard/DCIM/Camera") or [])
          if f.name.lower().endswith('.jpg')][:3]
if not photos:
    print("No JPEG files on the device to test with; skipping.")
    sys.exit(0)
for photo in photos:
    h.copy_on_device(photo.path, f"{BASE}/{photo.name}")
h._shell_action(f"echo hello > '{BASE}/plain.txt'", 'seed')

panel.current_path = BASE
panel.refresh_files()
pump(6, until=lambda: not panel._loading)
pump(0.5)

print("\n=== 1. every view mode is reachable ===")
for mode, label, px in views.VIEW_MODES:
    panel.set_view_mode(mode)
    pump(0.3)
    view = panel.tree_view
    expected = 'DropTreeView' if mode == views.DETAILS else 'FileListView'
    check(f"{label} switches the view",
          type(view).__name__ == expected and view.iconSize().width() == px,
          f"{type(view).__name__} icon={view.iconSize().width()}")

check("the model is shared by both views",
      panel._tree.model() is panel._list.model())
check("the selection is shared by both views",
      panel._tree.selectionModel() is panel._list.selectionModel())

print("\n=== 2. the view choice is remembered ===")
panel.set_view_mode(views.TILES)
check("the mode is stored",
      QSettings("ADB Explorer", "ADB Explorer").value("view_mode") == views.TILES)
panel.zoom_view(1)
check("Ctrl+wheel zooms up the ladder", panel.view_mode == views.ICONS_M, panel.view_mode)
panel.zoom_view(-1)
check("and back down", panel.view_mode == views.TILES, panel.view_mode)

print("\n=== 3. thumbnails appear, and only where they are visible ===")
panel.set_view_mode(views.ICONS_L)
pump(1)


def thumbed_rows():
    found = 0
    for row in range(panel.tree_model.rowCount()):
        icon = panel.tree_model.item(row, 0).icon()
        sizes = icon.availableSizes()
        if sizes and sizes[0].width() > 40:
            found += 1
    return found


pump(25, until=lambda: thumbed_rows() >= len(photos))
check("photos got real thumbnails", thumbed_rows() >= len(photos),
      f"{thumbed_rows()} of {len(photos)}")
check("a text file did not", thumbnails.can_thumbnail("plain.txt", False) is False)

print("\n=== 4. the cheap route is the one being used ===")
photo = photos[0]
started = time.time()
data, orientation = thumbnails._image_bytes(h.device_serial, f"{BASE}/{photo.name}",
                                            photo.size, '.jpg')
elapsed = time.time() - started
check("a thumbnail is far smaller than the photo",
      data is not None and len(data) < photo.size / 10,
      f"{len(data) if data else 0} vs {photo.size} bytes")
check("and arrives quickly", elapsed < 3, f"{elapsed:.2f}s")

image = QImage()
image.loadFromData(data)
rotated = thumbnails.apply_orientation(image, orientation)
if orientation in (6, 8):
    check("a sideways photo is turned upright",
          rotated.width() == image.height() and rotated.height() == image.width(),
          f"orientation={orientation}")
else:
    check("an upright photo is left alone", rotated.size() == image.size(),
          f"orientation={orientation}")

print("\n=== 5. the cache answers the second time ===")
cache = thumbnails.ThumbnailCache()
item = {'name': photo.name, 'path': f"{BASE}/{photo.name}",
        'size': photo.size, 'modified': photo.date_modified}
key = cache.key_for(h.device_serial, item, 96)
got = {}
cache.ready.connect(lambda k, p: got.update({k: p}))
# request() answers straight away when the picture is already cached, and
# through the signal when it has to be fetched.
immediate = cache.request(h.device_serial, item, 96)
pump(20, until=lambda: bool(got) or immediate is not None)
check("the thumbnail was produced", bool(got) or immediate is not None)
check("it is on disk", os.path.isfile(os.path.join(thumbnails.cache_dir(), key + '.png')))

fresh = thumbnails.ThumbnailCache()
started = time.time()
hit = fresh.cached(key)
check("a new session reads it from disk", hit is not None and time.time() - started < 0.5)

print("\n=== 6. video thumbnails need nothing installed ===")
backend = thumbnails.video_backend()
check("a video decoder is available out of the box", backend is not None, str(backend))
check("Qt's multimedia backend is present", thumbnails.QT_VIDEO is True)
check("videos can be thumbnailed", thumbnails.can_thumbnail("clip.mp4", False) is True)
check("the menu entry is enabled", panel._video_thumb_action.isEnabled() is True)

device_videos = [f for f in (h.list_directory("/sdcard/DCIM/Camera") or [])
                 if f.name.lower().endswith('.mp4') and f.size > 1_000_000][:1]
if device_videos:
    clip = device_videos[0]
    cache2 = thumbnails.ThumbnailCache()
    item2 = {'name': clip.name, 'path': clip.path, 'size': clip.size,
             'modified': clip.date_modified}
    frames = {}
    cache2.ready.connect(lambda k, p: frames.update({k: p}))
    started = time.time()
    immediate = cache2.request(h.device_serial, item2, 96)
    pump(40, until=lambda: bool(frames) or immediate is not None)
    pixmap = immediate or (list(frames.values())[0] if frames else None)
    check("a frame was decoded from a real video",
          pixmap is not None and not pixmap.isNull(),
          f"{time.time() - started:.1f}s, backend={backend}")
    if pixmap:
        check("the frame is a picture, not a placeholder",
              pixmap.width() > 8 and pixmap.height() > 8,
              f"{pixmap.width()}x{pixmap.height()}")
else:
    print("  (no video on the device to decode)")

print("\n=== 7. the MP4 index check that keeps video cheap ===")
# an ftyp box of exactly 32 bytes, followed by a complete 16-byte moov
FTYP = b'\x00\x00\x00\x20ftyp' + b'isom' + b'\x00' * 20
check("an index at the front is detected",
      thumbnails.has_moov(FTYP + b'\x00\x00\x00\x10moov' + b'\x00' * 8) is True)
check("an index that runs past the fetched bytes is not",
      thumbnails.has_moov(FTYP + b'\x00\x40\x00\x00moov' + b'\x00' * 8) is False)
check("no index at all is not",
      thumbnails.has_moov(FTYP + b'\x00\x00\x00\x10mdat' + b'\x00' * 8) is False)

sparse = os.path.join(tempfile.gettempdir(), 'adbx_sparse_test.bin')
thumbnails._write_sparse(sparse, 10_000_000, (0, b'HEAD' * 4),
                         (10_000_000 - 16, b'TAIL' * 4))
with open(sparse, 'rb') as fh:
    head_read = fh.read(16)
    fh.seek(10_000_000 - 16)
    tail_read = fh.read(16)
check("head and tail land at their real offsets",
      os.path.getsize(sparse) == 10_000_000 and head_read == b'HEAD' * 4
      and tail_read == b'TAIL' * 4)
os.remove(sparse)

h.delete_item(BASE, is_dir=True, missing_ok=True)
print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)

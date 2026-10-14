"""Thumbnails for files that live on the device.

Reading a whole photo just to draw a 96 px tile would be wasteful over adb, so
each kind of file is handled by the cheapest route that works:

* JPEG - only the first 128 KB are read, which is enough to lift out the
  thumbnail that cameras embed in the EXIF block (typically 512 px for a 2.5 MB
  photo, fetched in about a tenth of a second).
* other images - fetched in full, but only below a size limit.
* video - decoded by Qt's own multimedia backend, which ships with PyQt6, so
  nothing has to be installed. Rather than fetching a whole clip, the head is
  read, and the tail as well when the index lives at the end, and both are
  written into a sparse local file at their real offsets. An external ffmpeg
  is used instead when one is on the PATH, which keeps the work off the GUI
  thread, but it is never required.

Results are cached in memory and on disk, keyed by path, size and timestamp,
so a folder costs nothing to revisit.
"""

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile

from PyQt6.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QTransform

try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    QT_VIDEO = True
except ImportError:          # a PyQt6 build without the multimedia module
    QT_VIDEO = False

from handler import ADBHandler, find_adb, shell_quote

JPEG_EXTS = {'.jpg', '.jpeg', '.jpe', '.jfif'}
IMAGE_EXTS = JPEG_EXTS | {'.png', '.gif', '.bmp', '.webp', '.ico', '.tif', '.tiff',
                          '.heic', '.heif', '.avif'}
VIDEO_EXTS = {'.mp4', '.m4v', '.mkv', '.webm', '.mov', '.3gp', '.3gpp', '.avi', '.ts'}

# How much of a non-JPEG image is worth fetching in full.
MAX_FULL_IMAGE_BYTES = 2 * 1024 * 1024
# A video this small is simply fetched whole; larger ones use head + tail.
MAX_FULL_VIDEO_BYTES = 6 * 1024 * 1024
# Most phone video is written "faststart", with the index at the front, so a
# small head is usually enough for both the index and the first frame.
VIDEO_FIRST_TRY_BYTES = 1536 * 1024
VIDEO_HEAD_BYTES = 4 * 1024 * 1024
VIDEO_TAIL_BYTES = 1 * 1024 * 1024
EXIF_SCAN_BYTES = 128 * 1024

_ffmpeg = None
_ffmpeg_checked = False


def find_ffmpeg(refresh=False):
    """Locate ffmpeg, which is optional and only used for video frames."""
    global _ffmpeg, _ffmpeg_checked
    if _ffmpeg_checked and not refresh:
        return _ffmpeg

    _ffmpeg_checked = True
    override = os.environ.get('FFMPEG_PATH')
    if override and os.path.isfile(override):
        _ffmpeg = override
        return _ffmpeg

    found = shutil.which('ffmpeg')
    if not found:
        candidates = []
        if os.name == 'nt':
            for base in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                         os.environ.get('LOCALAPPDATA', ''), r'C:\\'):
                if base:
                    candidates.append(os.path.join(base, 'ffmpeg', 'bin', 'ffmpeg.exe'))
        else:
            candidates += ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']
        for candidate in candidates:
            if os.path.isfile(candidate):
                found = candidate
                break

    _ffmpeg = found
    return _ffmpeg


def video_backend():
    """How video frames can be decoded on this machine.

    Qt's multimedia backend comes with PyQt6, so video thumbnails work out of
    the box; an external ffmpeg is preferred when present only because it can
    run entirely on a worker thread.
    """
    if find_ffmpeg():
        return 'ffmpeg'
    if QT_VIDEO:
        return 'qt'
    return None


def can_thumbnail(name, is_dir, videos_enabled=True):
    if is_dir:
        return False
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS:
        return True
    return ext in VIDEO_EXTS and videos_enabled and video_backend() is not None


def cache_dir():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
            os.path.expanduser('~'), '.cache')
    path = os.path.join(base, 'adb_explorer', 'thumbnails')
    os.makedirs(path, exist_ok=True)
    return path


# Bumped when the pictures themselves change meaning, so stale entries from
# an older build are simply never found.
CACHE_VERSION = 2


def cache_key(serial, path, size, modified, px):
    raw = f"{CACHE_VERSION}|{serial}|{path}|{size}|{modified}|{px}".encode(
        'utf-8', errors='replace')
    return hashlib.sha1(raw).hexdigest()


# --------------------------------------------------------------------- reading

def _adb_exec(serial, command, timeout=30):
    """Run one exec-out command and return its raw stdout, or None."""
    try:
        cmd = [find_adb()]
        if serial:
            cmd += ['-s', serial]
        cmd += ['exec-out', command]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                **ADBHandler._hidden_kwargs())
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _read_range(serial, path, offset, length, timeout=30):
    """Read `length` bytes starting at `offset`, in whole 64 KB blocks."""
    block = 65536
    skip = offset // block
    count = max(1, (length + block - 1) // block)
    command = (f'dd if={shell_quote(path)} bs={block} skip={skip} count={count} '
               f'2>/dev/null')
    return _adb_exec(serial, command, timeout=timeout)


def _find_exif(data):
    """Offset of the TIFF header inside a JPEG's EXIF block, if present."""
    if not data or data[:2] != b'\xff\xd8':
        return None
    i = 2
    try:
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0xD9):
                return None
            seglen = struct.unpack('>H', data[i + 2:i + 4])[0]
            if marker == 0xE1 and data[i + 4:i + 10] == b'Exif\x00\x00':
                return i + 10
            if marker == 0xDA:        # start of scan: no EXIF after this
                return None
            i += 2 + seglen
    except (struct.error, IndexError):
        return None
    return None


def exif_thumbnail(data):
    """Return the JPEG thumbnail embedded in an EXIF block, if there is one."""
    tiff = _find_exif(data)
    if tiff is None:
        return None
    try:
        return _exif_ifd1_thumbnail(data, tiff)
    except (struct.error, IndexError):
        return None


def exif_orientation(data):
    """The EXIF orientation tag, which says how the camera was held.

    Embedded thumbnails are stored unrotated, so without this every portrait
    photo would be shown on its side.
    """
    tiff = _find_exif(data)
    if tiff is None:
        return 1
    try:
        fmt = '>' if data[tiff:tiff + 2] == b'MM' else '<'
        first_ifd = struct.unpack(fmt + 'I', data[tiff + 4:tiff + 8])[0]
        pos = tiff + first_ifd
        count = struct.unpack(fmt + 'H', data[pos:pos + 2])[0]
        for index in range(count):
            entry = pos + 2 + index * 12
            tag = struct.unpack(fmt + 'H', data[entry:entry + 2])[0]
            if tag == 0x0112:
                value = struct.unpack(fmt + 'H', data[entry + 8:entry + 10])[0]
                return value if 1 <= value <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


def apply_orientation(image, orientation):
    """Rotate or mirror a decoded image the way the EXIF tag asks."""
    if not orientation or orientation == 1 or image.isNull():
        return image

    transform = QTransform()
    if orientation == 2:
        transform.scale(-1, 1)
    elif orientation == 3:
        transform.rotate(180)
    elif orientation == 4:
        transform.scale(1, -1)
    elif orientation == 5:
        transform.rotate(90)
        transform.scale(-1, 1)
    elif orientation == 6:
        transform.rotate(90)
    elif orientation == 7:
        transform.rotate(270)
        transform.scale(-1, 1)
    elif orientation == 8:
        transform.rotate(270)
    else:
        return image
    return image.transformed(transform, Qt.TransformationMode.SmoothTransformation)


def _exif_ifd1_thumbnail(data, tiff):
    """Walk to the second IFD, which is where the thumbnail is described."""
    endian = data[tiff:tiff + 2]
    fmt = '>' if endian == b'MM' else '<'

    first_ifd = struct.unpack(fmt + 'I', data[tiff + 4:tiff + 8])[0]
    pos = tiff + first_ifd
    count = struct.unpack(fmt + 'H', data[pos:pos + 2])[0]
    next_ifd = struct.unpack(fmt + 'I', data[pos + 2 + count * 12:pos + 6 + count * 12])[0]
    if not next_ifd:
        return None

    pos = tiff + next_ifd
    count = struct.unpack(fmt + 'H', data[pos:pos + 2])[0]
    offset = length = None
    for index in range(count):
        entry = pos + 2 + index * 12
        tag = struct.unpack(fmt + 'H', data[entry:entry + 2])[0]
        value = struct.unpack(fmt + 'I', data[entry + 8:entry + 12])[0]
        if tag == 0x0201:
            offset = value
        elif tag == 0x0202:
            length = value

    if not offset or not length:
        return None
    thumb = data[tiff + offset:tiff + offset + length]
    return thumb if thumb[:2] == b'\xff\xd8' else None


def _image_bytes(serial, path, size, ext):
    """Fetch as few bytes as will produce a picture.

    Returns (data, orientation); the orientation applies to the data returned.
    """
    if ext in JPEG_EXTS:
        head = _read_range(serial, path, 0, EXIF_SCAN_BYTES)
        orientation = exif_orientation(head)
        thumb = exif_thumbnail(head)
        if thumb:
            return thumb, orientation
        # No embedded thumbnail: fall back to the whole file if it is small.
        if head is not None and size <= EXIF_SCAN_BYTES:
            return head, orientation

    if size and size <= MAX_FULL_IMAGE_BYTES:
        data = _adb_exec(serial, f'cat {shell_quote(path)}', timeout=60)
        # A full JPEG carries its own orientation, which Qt does not apply.
        return data, (exif_orientation(data) if ext in JPEG_EXTS else 1)
    return None, 1


def _write_sparse(local_path, total_size, head, tail):
    """Write the head and tail of a file at their real offsets.

    The gap is left as a hole, so only the fetched bytes occupy space, and a
    reader that seeks (like ffmpeg looking for an MP4 index at the end) sees a
    file of the right length.
    """
    with open(local_path, 'wb') as handle:
        handle.truncate(total_size)
        if head:
            handle.seek(0)
            handle.write(head)
        if tail:
            handle.seek(max(0, total_size - len(tail)))
            handle.write(tail)


def has_moov(data):
    """True if an MP4 index atom is complete within these leading bytes.

    Phone cameras usually write the index at the front, which means the first
    megabyte or so is all that has to be fetched to decode a frame.
    """
    if not data:
        return False
    pos = 0
    limit = len(data)
    while pos + 8 <= limit:
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        name = data[pos + 4:pos + 8]
        if size == 1:
            if pos + 16 > limit:
                return False
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
        if size < 8:
            return False
        if name == b'moov':
            return pos + size <= limit
        pos += size
    return False


def _run_ffmpeg(ffmpeg, clip, out, px):
    scale = f'scale={px}:{px}:force_original_aspect_ratio=decrease'
    for seek in ('00:00:01', '00:00:00'):
        command = [ffmpeg, '-nostdin', '-loglevel', 'error', '-ss', seek,
                   '-i', clip, '-frames:v', '1', '-vf', scale, '-y', out]
        try:
            subprocess.run(command, capture_output=True, timeout=60,
                           **ADBHandler._hidden_kwargs())
        except Exception:
            return None
        if os.path.isfile(out) and os.path.getsize(out):
            with open(out, 'rb') as handle:
                data = handle.read()
            os.remove(out)
            if data:
                return data
    return None


def fetch_video_locally(serial, path, size, target_dir):
    """Put enough of a remote clip on disk for a decoder to find a frame.

    Returns the local path, or None. Only the head is fetched when the index
    is already in it, which is the usual case for phone video.
    """
    local = os.path.join(target_dir, 'clip' + (os.path.splitext(path)[1].lower() or '.mp4'))

    if size and size <= MAX_FULL_VIDEO_BYTES:
        data = _adb_exec(serial, f'cat {shell_quote(path)}', timeout=120)
        if not data:
            return None
        with open(local, 'wb') as handle:
            handle.write(data)
        return local

    head = _read_range(serial, path, 0, VIDEO_FIRST_TRY_BYTES, timeout=60)
    if not head:
        return None
    if has_moov(head):
        _write_sparse(local, size or len(head), head, None)
        return local

    head = _read_range(serial, path, 0, VIDEO_HEAD_BYTES, timeout=60) or head
    tail_offset = max(0, (size or 0) - VIDEO_TAIL_BYTES)
    tail = _read_range(serial, path, tail_offset, VIDEO_TAIL_BYTES, timeout=60)
    _write_sparse(local, size or len(head), head, tail)
    return local


class VideoFrameGrabber(QObject):
    """Decodes one frame per clip with Qt, on the GUI thread, one at a time.

    QMediaPlayer has to live on a thread with an event loop, so requests are
    queued here and answered asynchronously; a watchdog moves on if a file
    turns out to be undecodable.
    """

    finished = pyqtSignal(str, object)      # key, QImage or None

    TIMEOUT_MS = 8000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self._current = None
        self._player = None
        self._sink = None
        self._audio = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._give_up)

    def grab(self, key, local_path, px):
        self._queue.append((key, local_path, px))
        if self._current is None:
            self._start_next()

    def _ensure_player(self):
        if self._player is not None:
            return
        self._sink = QVideoSink(self)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(lambda *_: self._give_up())

    def _start_next(self):
        self._current = None
        while self._queue:
            key, local_path, px = self._queue.pop(0)
            if not os.path.isfile(local_path):
                self.finished.emit(key, None)
                continue
            self._current = (key, local_path, px)
            break
        if self._current is None:
            return

        self._ensure_player()
        self._timer.start(self.TIMEOUT_MS)
        self._player.setSource(QUrl.fromLocalFile(self._current[1]))

    def _on_status(self, status):
        if self._current and status == QMediaPlayer.MediaStatus.LoadedMedia:
            # A second in avoids the black frame many clips open with.
            self._player.setPosition(1000)
            self._player.play()

    def _on_frame(self, frame):
        if not self._current or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull() or image.width() <= 1:
            return
        self._deliver(image.copy())

    def _give_up(self):
        self._deliver(None)

    def _deliver(self, image):
        if not self._current:
            return
        key = self._current[0]
        self._timer.stop()
        self._player.stop()
        self._player.setSource(QUrl())
        self._current = None
        self.finished.emit(key, image)
        self._start_next()


def _video_frame_bytes(serial, path, size, px):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    with tempfile.TemporaryDirectory(prefix='adbx_thumb_') as tmp:
        clip = os.path.join(tmp, 'clip' + (os.path.splitext(path)[1].lower() or '.mp4'))
        out = os.path.join(tmp, 'frame.png')

        if size and size <= MAX_FULL_VIDEO_BYTES:
            data = _adb_exec(serial, f'cat {shell_quote(path)}', timeout=120)
            if not data:
                return None
            with open(clip, 'wb') as handle:
                handle.write(data)
            return _run_ffmpeg(ffmpeg, clip, out, px)

        # First try: just the head, which is enough for a faststart file.
        head = _read_range(serial, path, 0, VIDEO_FIRST_TRY_BYTES, timeout=60)
        if not head:
            return None
        if has_moov(head):
            _write_sparse(clip, size or len(head), head, None)
            frame = _run_ffmpeg(ffmpeg, clip, out, px)
            if frame:
                return frame

        # Otherwise the index is at the end, so fetch a larger head and the
        # tail and place both at their real offsets.
        head = _read_range(serial, path, 0, VIDEO_HEAD_BYTES, timeout=60) or head
        tail_offset = max(0, (size or 0) - VIDEO_TAIL_BYTES)
        tail = _read_range(serial, path, tail_offset, VIDEO_TAIL_BYTES, timeout=60)
        _write_sparse(clip, size or len(head), head, tail)
        return _run_ffmpeg(ffmpeg, clip, out, px)


# ------------------------------------------------------------------- the cache

class _Signals(QObject):
    done = pyqtSignal(str, object, int)            # key, QImage or None, generation
    video_ready = pyqtSignal(str, str, str, int)   # key, clip path, temp dir, generation


# Marker meaning "this job's result will arrive from somewhere else".
HANDED_OVER = object()


class _ThumbnailJob(QRunnable):

    def __init__(self, signals, serial, item, px, key, generation):
        super().__init__()
        self.signals = signals
        self.serial = serial
        self.item = item
        self.px = px
        self.key = key
        self.generation = generation

    def run(self):
        image = None
        try:
            ext = os.path.splitext(self.item['name'])[1].lower()
            orientation = 1
            if ext in IMAGE_EXTS:
                data, orientation = _image_bytes(self.serial, self.item['path'],
                                                 self.item['size'], ext)
            elif ext in VIDEO_EXTS:
                data = self._video_data()
                if data is HANDED_OVER:
                    return
            else:
                data = None

            if data:
                loaded = QImage()
                if loaded.loadFromData(data) and not loaded.isNull():
                    loaded = apply_orientation(loaded, orientation)
                    image = loaded.scaled(
                        self.px, self.px,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        except Exception:
            image = None
        self.signals.done.emit(self.key, image, self.generation)

    def _video_data(self):
        """Fetch the clip, and decode here only when ffmpeg can do it.

        Qt's decoder needs the GUI thread, so in that case the clip is handed
        over and this job is done.
        """
        backend = video_backend()
        if backend == 'ffmpeg':
            return _video_frame_bytes(self.serial, self.item['path'],
                                      self.item['size'], self.px)
        if backend != 'qt':
            return None

        target = tempfile.mkdtemp(prefix='adbx_clip_')
        clip = None
        try:
            clip = fetch_video_locally(self.serial, self.item['path'],
                                       self.item['size'], target)
        except Exception:
            clip = None
        if not clip:
            shutil.rmtree(target, ignore_errors=True)
            return None

        self.signals.video_ready.emit(self.key, clip, target, self.generation)
        return HANDED_OVER


class ThumbnailCache(QObject):
    """Produces thumbnails off the GUI thread and remembers them."""

    ready = pyqtSignal(str, QPixmap)      # key, pixmap

    def __init__(self, parent=None, max_workers=3, memory_limit=600):
        super().__init__(parent)
        self._memory = {}
        self._order = []
        self._memory_limit = memory_limit
        self._pending = set()
        self._failed = set()
        self._px_for = {}
        self._generation = 0
        self._signals = _Signals()
        self._signals.done.connect(self._job_finished)
        self._signals.video_ready.connect(self._video_fetched)
        self._grabber = None
        self._video_temp = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_workers)

    # -- generations keep results from a folder we have left out of the view
    def new_generation(self):
        self._generation += 1
        self._pending.clear()
        return self._generation

    @property
    def generation(self):
        return self._generation

    def key_for(self, serial, item, px):
        return cache_key(serial, item['path'], item['size'], item['modified'], px)

    def cached(self, key):
        """A thumbnail already in memory, or on disk from an earlier run."""
        pixmap = self._memory.get(key)
        if pixmap is not None:
            return pixmap

        path = os.path.join(cache_dir(), key + '.png')
        if os.path.isfile(path):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._remember(key, pixmap)
                return pixmap
        return None

    def request(self, serial, item, px):
        """Ask for a thumbnail; `ready` is emitted when one arrives."""
        key = self.key_for(serial, item, px)
        pixmap = self.cached(key)
        if pixmap is not None:
            return pixmap
        if key in self._pending or key in self._failed:
            return None

        self._pending.add(key)
        self._px_for[key] = px
        self._pool.start(_ThumbnailJob(self._signals, serial, item, px, key,
                                       self._generation))
        return None

    def _video_fetched(self, key, clip, temp_dir, generation):
        """A clip is on disk; let Qt decode one frame from it."""
        if generation != self._generation or not QT_VIDEO:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._pending.discard(key)
            return

        if self._grabber is None:
            self._grabber = VideoFrameGrabber(self)
            self._grabber.finished.connect(self._video_frame_ready)

        self._video_temp[key] = (temp_dir, generation)
        self._grabber.grab(key, clip, self._px_for.get(key, 96))

    def _video_frame_ready(self, key, image):
        temp_dir, generation = self._video_temp.pop(key, (None, self._generation))
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if image is not None and not image.isNull():
            px = self._px_for.get(key, 96)
            image = image.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self._job_finished(key, image, generation)

    def _job_finished(self, key, image, generation):
        self._pending.discard(key)
        if image is None:
            # Remember the failure so a broken or unreadable file is not
            # fetched again every time the folder is shown.
            self._failed.add(key)
            return

        pixmap = QPixmap.fromImage(image)
        self._remember(key, pixmap)
        try:
            pixmap.save(os.path.join(cache_dir(), key + '.png'), 'PNG')
        except Exception:
            pass
        if generation == self._generation:
            self.ready.emit(key, pixmap)

    def _remember(self, key, pixmap):
        if key not in self._memory:
            self._order.append(key)
        self._memory[key] = pixmap
        while len(self._order) > self._memory_limit:
            self._memory.pop(self._order.pop(0), None)

    def clear_memory(self):
        self._memory.clear()
        self._order.clear()
        self._failed.clear()

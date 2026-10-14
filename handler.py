import subprocess
import socket
import struct
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import re
import time
import zipfile
import os
import tempfile
import json


class ADBNotFoundError(RuntimeError):
    """Raised when the adb executable cannot be located on this machine."""


def _candidate_adb_paths():
    """Common SDK install locations, so the app works without adb on PATH."""
    home = os.path.expanduser('~')
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA', os.path.join(home, 'AppData', 'Local'))
        for base in (
            os.path.join(local, 'Android', 'Sdk'),
            os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'), 'Android', 'Sdk'),
            r'C:\Android\Sdk',
            r'C:\Android',
        ):
            yield os.path.join(base, 'platform-tools', 'adb.exe')
            yield os.path.join(base, 'adb.exe')
        return

    for path in (
        os.path.join(home, 'Library', 'Android', 'sdk', 'platform-tools', 'adb'),
        os.path.join(home, 'Android', 'Sdk', 'platform-tools', 'adb'),
        '/opt/homebrew/bin/adb',
        '/usr/local/bin/adb',
        '/usr/bin/adb',
        '/usr/lib/android-sdk/platform-tools/adb',
    ):
        yield path


_adb_executable: Optional[str] = None


def find_adb(refresh: bool = False) -> str:
    """Locate the adb executable, raising ADBNotFoundError with a usable message."""
    global _adb_executable
    if _adb_executable and not refresh:
        return _adb_executable

    override = os.environ.get('ADB_PATH')
    if override:
        candidates = [override, os.path.join(override, 'adb.exe' if os.name == 'nt' else 'adb')]
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                _adb_executable = candidate
                return _adb_executable

    found = shutil.which('adb')
    if not found:
        for candidate in _candidate_adb_paths():
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break

    if not found:
        raise ADBNotFoundError(
            "ADB (Android Debug Bridge) was not found.\n\n"
            "Install the Android SDK Platform Tools and make sure 'adb' is on your "
            "PATH, or set the ADB_PATH environment variable to the adb executable."
        )

    _adb_executable = found
    return _adb_executable


def adb_available() -> bool:
    try:
        find_adb()
        return True
    except ADBNotFoundError:
        return False


def shell_quote(value: str) -> str:
    """Quote a value for the device's POSIX shell.

    Single quotes are the only form that is safe for every byte a filename may
    contain ($, `, \\, ", spaces, newlines), so quote with them and escape any
    embedded single quote by closing, inserting a literal one, and reopening.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"


@dataclass
class FileItem:
    name: str
    path: str
    is_dir: bool
    size: int
    permissions: str
    date_modified: str
    link_target: Optional[str] = None

    @property
    def is_link(self) -> bool:
        return self.permissions.startswith('l')


@dataclass
class DeviceEndpoint:
    """One ADB endpoint as reported by `adb devices -l`."""
    serial: str
    state: str
    model: str

    @property
    def is_usable(self) -> bool:
        return self.state == 'device'

    @property
    def status_text(self) -> str:
        if self.state == 'unauthorized':
            return (f"{self.serial} is unauthorized - accept the 'Allow USB debugging' "
                    f"prompt on the device")
        if self.state == 'offline':
            return f"{self.serial} is offline - reconnect the cable or re-pair the device"
        return f"{self.serial}: {self.state}"


class ADBHandler:
    _active_streams = {}

    _WIN_FLAGS: int = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    # Marker used to split the directory and file halves of a single `find` run.
    _FIND_SEPARATOR = '---adb-explorer-find-split---'

    def __init__(self, device_serial=None):
        import logging
        self.logger = logging.getLogger('ADBHandler')

        self.device_serial = device_serial
        self.root_mode = None
        self.last_error = None
        self._active_process = None
        self.endpoints: List[DeviceEndpoint] = []
        self.adb_missing = not adb_available()

        if self.adb_missing:
            self.logger.error("ADB executable not found; see find_adb() for search locations")
            self.last_error = "ADB executable not found"
            self.devices = {}
            self.device_connected = False
            return

        self._ensure_server_started()

        self.devices = self.get_connected_devices()
        self.device_connected = bool(self.devices)

        if not self.device_connected:
            self.logger.warning("No ADB device connected")
        elif not device_serial and len(self.devices) == 1:
            self.device_serial = next(iter(self.devices.keys()))

    def _build_adb_cmd(self, command: list, device_serial: str = None) -> list:
        cmd = [find_adb()]
        serial = device_serial or self.device_serial
        if serial:
            cmd.extend(['-s', serial])
        cmd.extend(command)
        return cmd

    @staticmethod
    def _make_startupinfo():
        startupinfo = None
        if hasattr(subprocess, 'STARTUPINFO'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo

    @classmethod
    def _hidden_kwargs(cls) -> dict:
        """Keep console windows from flashing up on Windows."""
        return {'startupinfo': cls._make_startupinfo(), 'creationflags': cls._WIN_FLAGS}

    @staticmethod
    def _failed_result(cmd, message: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, '', message)

    def _exec_mkdir(self, serial, remote_dirs):
        """Create one or many remote directories in a single adb invocation."""
        if isinstance(remote_dirs, str):
            remote_dirs = [remote_dirs]
        wanted = [d for d in dict.fromkeys(remote_dirs) if d]
        if not wanted:
            return True

        command = 'mkdir -p ' + ' '.join(shell_quote(d) for d in wanted)
        try:
            result = subprocess.run(
                self._build_adb_cmd(['exec-out', command], device_serial=serial),
                capture_output=True, timeout=max(15, 2 * len(wanted)),
                **self._hidden_kwargs(),
            )
        except Exception as e:
            self.logger.warning(f"mkdir -p failed for {wanted}: {e}")
            return False

        if result.returncode != 0:
            error = (result.stderr or b'').decode('utf-8', errors='replace').strip()
            self.logger.warning(f"mkdir -p failed for {wanted}: {error}")
            return False
        return True

    def _ensure_server_started(self):
        try:
            subprocess.run(
                [find_adb(), 'start-server'],
                capture_output=True,
                timeout=15,
                **self._hidden_kwargs(),
            )
        except Exception as e:
            self.logger.warning(f"Could not start the ADB server: {e}")

    def get_device_endpoints(self) -> List[DeviceEndpoint]:
        """Parse `adb devices -l` into endpoints, keeping unusable ones visible."""
        endpoints: List[DeviceEndpoint] = []
        try:
            result = self._run_adb_command(['devices', '-l'], device_serial=False)
            if result.returncode != 0:
                self.logger.error(f"Error getting devices: {result.stderr}")
                self.endpoints = []
                return []

            for line in (result.stdout or '').splitlines():
                line = line.strip()
                # Skip the header and the blank/daemon-startup noise around it.
                if not line or line.startswith('List of devices'):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                serial, state = parts[0], parts[1]
                model = next(
                    (p.split(':', 1)[1] for p in parts[2:] if p.startswith('model:')),
                    'Unknown',
                )
                endpoints.append(DeviceEndpoint(serial=serial, state=state, model=model))
        except Exception as e:
            self.logger.error(f"Error listing devices: {e}")
            self.endpoints = []
            return []

        self.endpoints = endpoints
        return endpoints

    def get_connected_devices(self) -> Dict[str, str]:
        return {e.serial: e.model for e in self.get_device_endpoints() if e.is_usable}

    def get_unusable_endpoints(self) -> List[DeviceEndpoint]:
        """Endpoints that are present but not ready (unauthorized, offline, ...)."""
        return [e for e in (self.endpoints or self.get_device_endpoints()) if not e.is_usable]

    def get_device_identity(self, serial: str) -> str:
        """Return the physical device serial reported by a specific ADB endpoint."""
        try:
            result = subprocess.run(
                self._build_adb_cmd(['get-serialno'], device_serial=serial),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
                **self._hidden_kwargs(),
            )
            identity = (result.stdout or '').strip()
            if result.returncode == 0 and identity and identity != 'unknown':
                return identity
        except Exception as e:
            self.logger.debug(f"get-serialno failed for {serial}: {e}")
        return serial

    def get_unique_devices(self) -> Dict[str, str]:
        """Return one preferred ADB endpoint per physical device, preferring USB."""
        devices = self.get_connected_devices()
        if len(devices) <= 1:
            # Nothing to de-duplicate, so skip the extra adb round-trip entirely.
            return devices

        serials = list(devices)
        # One `get-serialno` per endpoint, run concurrently instead of serially.
        with ThreadPoolExecutor(max_workers=min(8, len(serials))) as pool:
            identities = list(pool.map(self.get_device_identity, serials))

        unique: Dict[str, Tuple[str, str]] = {}
        for serial, identity in zip(serials, identities):
            current = unique.get(identity)
            if current is None or self._endpoint_priority(serial) < self._endpoint_priority(current[0]):
                unique[identity] = (serial, devices[serial])
        return {serial: model for serial, model in unique.values()}

    @staticmethod
    def _endpoint_priority(serial: str):
        return (':' in serial, serial)

    def check_adb_connection(self) -> bool:
        try:
            return any(e.is_usable for e in self.get_device_endpoints())
        except Exception as e:
            self.logger.error(f"ADB Connection error: {e}")
            return False

    def _escape_path(self, path: str) -> str:
        return shell_quote(path)

    def _su_command(self, command: str) -> list:
        """Wrap a shell snippet so `su -c` receives it as a single argument."""
        return ['shell', f'su -c {shell_quote(command)}']

    def _shell(self, command: str, use_root: Optional[bool] = None, timeout: int = 30):
        """Run a shell snippet on the device.

        `use_root=None` elevates automatically whenever the user has enabled su
        root mode, so file operations are not silently denied after the device
        has been rooted. Pass True/False to decide explicitly.
        """
        if use_root is None:
            use_root = self.root_mode == 'su'
        if use_root and self.root_mode == 'su':
            return self._run_adb_command(self._su_command(command), timeout=timeout)
        return self._run_adb_command(['shell', command], timeout=timeout)

    def _shell_action(self, command: str, description: str, timeout: int = 30) -> bool:
        """Run a shell command that is silent on success and report real errors."""
        if not self.device_connected:
            self.last_error = "No ADB device connected"
            self.logger.error("No ADB device connected")
            return False

        try:
            result = self._shell(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.last_error = f"Timed out while {description}"
            self.logger.error(self.last_error)
            return False
        except Exception as e:
            self.last_error = str(e)
            self.logger.error(f"Error while {description}: {e}")
            return False

        stderr = (result.stderr or "").strip()
        # Older adb always exits 0, so treat any stderr output as a failure too.
        if result.returncode != 0 or stderr:
            self.last_error = stderr or (result.stdout or "").strip() or f"Failed while {description}"
            self.logger.error(f"Error while {description}: {self.last_error}")
            return False

        self.last_error = None
        return True

    def _run_adb_command(self, command: list, use_shell=False, timeout=30,
                         device_serial=None) -> subprocess.CompletedProcess:
        """Run an adb command. `device_serial=False` targets the server, not a device."""
        try:
            serial = self.device_serial if device_serial is None else (device_serial or None)
            cmd = self._build_adb_cmd(command, device_serial=serial)
        except ADBNotFoundError as e:
            self.logger.error(str(e))
            return self._failed_result(['adb', *command], str(e))

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                # Device output is UTF-8; without this, Python would decode it
                # with the host locale codec and crash on non-Latin filenames.
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                shell=use_shell,
                **self._hidden_kwargs(),
            )
        except subprocess.TimeoutExpired:
            self.logger.error(f"Command timed out: {' '.join(cmd)}")
            raise
        except OSError as e:
            self.logger.error(f"Could not run adb: {e}")
            return self._failed_result(cmd, f"Could not run adb: {e}")

    @staticmethod
    def _adb_server_address() -> Tuple[str, int]:
        """Honour the standard ADB server env vars instead of assuming 127.0.0.1:5037."""
        host = os.environ.get('ANDROID_ADB_SERVER_ADDRESS') or '127.0.0.1'
        try:
            port = int(os.environ.get('ANDROID_ADB_SERVER_PORT') or 5037)
        except ValueError:
            port = 5037
        return host, port

    def _adb_push_sync(self, dst_serial, dst_path, data_iter, cancel_check=None):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect(self._adb_server_address())

            def sexact(d):
                t = 0
                while t < len(d):
                    n = sock.send(d[t:])
                    if n == 0: raise ConnectionError("ADB disconnected")
                    t += n

            def rexact(n):
                buf = b''
                while len(buf) < n:
                    c = sock.recv(n - len(buf))
                    if not c: raise ConnectionError("ADB disconnected")
                    buf += c
                return buf

            def adb_cmd(c):
                sexact(b'%04x' % len(c) + c)
                r = rexact(4)
                if r != b'OKAY':
                    raise RuntimeError(f"ADB cmd failed: {r!r}")

            adb_cmd(b'host:transport:' + dst_serial.encode())
            adb_cmd(b'sync:')
            sock.settimeout(None)

            path_b = dst_path.rstrip('/').encode()
            mode_str = b'644'
            send_data = path_b + b',' + mode_str
            while len(send_data) % 4 != 0:
                send_data += b'\x00'
            sexact(b'SEND' + struct.pack('<I', len(send_data)) + send_data)

            for chunk in data_iter:
                if cancel_check and cancel_check():
                    return False, "cancelled"
                sexact(b'DATA' + struct.pack('<I', len(chunk)) + chunk)

            sexact(b'DONE' + struct.pack('<I', int(time.time())))

            resp = rexact(4)
            if resp == b'OKAY':
                return True, ""
            elif resp == b'FAIL':
                elen = struct.unpack('<I', rexact(4))[0]
                return False, rexact(elen).decode('utf-8', errors='replace')
            else:
                return False, f"unexpected sync response: {resp!r}"
        except Exception as e:
            return False, str(e)
        finally:
            if sock:
                try: sock.close()
                except Exception: pass

    def stream_file(self, src_serial: str, src_path: str, dst_serial: str, dst_path: str,
                    chunk_callback=None, cancel_check=None, ensure_dir: bool = True) -> bool:
        if ensure_dir:
            si_dir = '/'.join(dst_path.rstrip('/').split('/')[:-1])
            if si_dir:
                self._exec_mkdir(dst_serial, si_dir)

        src_proc = None
        try:
            src_proc = subprocess.Popen(
                self._build_adb_cmd(['exec-out', f'cat {shell_quote(src_path)}'],
                                    device_serial=src_serial),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **self._hidden_kwargs(),
            )
            self._active_process = src_proc
            stream_id = id(src_proc)
            ADBHandler._active_streams[stream_id] = (src_proc, None)

            def data_iter():
                buf = 262144
                while True:
                    if cancel_check and cancel_check():
                        return
                    chunk = src_proc.stdout.read(buf)
                    if not chunk:
                        break
                    if chunk_callback:
                        chunk_callback(len(chunk))
                    yield chunk

            ok, err = self._adb_push_sync(dst_serial, dst_path, data_iter(), cancel_check=cancel_check)
            src_proc.wait()

            src_err = (src_proc.stderr.read() or b'').decode('utf-8', errors='replace').strip()
            if src_err:
                self.logger.error(f"Stream src stderr: {src_err[:300]}")

            ADBHandler._active_streams.pop(stream_id, None)
            self._active_process = None

            if not ok:
                self.logger.error(f"Stream push failed: {err}")
                self.last_error = err
                return False
            if src_proc.returncode != 0:
                self.last_error = f"source cat exited {src_proc.returncode}"
                return False
            return True

        except Exception as e:
            self.logger.error(f"Stream file error: {e}")
            self.last_error = str(e)
            return False
        finally:
            if src_proc:
                ADBHandler._active_streams.pop(id(src_proc), None)
                if src_proc.returncode is None:
                    try: src_proc.kill()
                    except Exception: pass
            self._active_process = None

    def stream_directory(self, src_serial: str, src_path: str, dst_serial: str, dst_path: str,
                         line_callback=None, cancel_check=None) -> bool:
        parent = '/'.join(dst_path.rstrip('/').split('/')[:-1])
        base_name = src_path.rstrip('/').split('/')[-1]
        target = f"{parent}/{base_name}" if parent else base_name
        src_root = src_path.rstrip('/')

        try:
            # One walk of the source tree: directories first, then files.
            quoted_src = shell_quote(src_path)
            r = subprocess.run(
                self._build_adb_cmd(
                    ['exec-out',
                     f'find {quoted_src} -type d; echo "{self._FIND_SEPARATOR}"; '
                     f'find {quoted_src} -type f'],
                    device_serial=src_serial),
                capture_output=True, timeout=180,
                **self._hidden_kwargs(),
            )
            if r.returncode != 0:
                self.last_error = (r.stderr or b'').decode('utf-8', errors='replace').strip()
                self.logger.error(f"Could not walk {src_path}: {self.last_error}")
                return False

            output = r.stdout.decode('utf-8', errors='replace')
            dir_block, _, file_block = output.partition(self._FIND_SEPARATOR)
            dirs = [d.strip() for d in dir_block.splitlines() if d.strip()]
            files = [f.strip() for f in file_block.splitlines() if f.strip()]

            def remote_dest(path: str) -> str:
                rel = path[len(src_root):].lstrip('/')
                return f"{target}/{rel}" if rel else target

            # Create every destination directory up front rather than one adb
            # call per transferred file, which dominated the transfer time.
            needed_dirs = [parent] + [remote_dest(d) for d in dirs]
            needed_dirs += ['/'.join(remote_dest(f).rstrip('/').split('/')[:-1]) for f in files]
            if not self._exec_mkdir(dst_serial, needed_dirs):
                self.last_error = f"Could not create destination directory under {target}"
                return False

            for fpath in files:
                if cancel_check and cancel_check():
                    return False
                if line_callback:
                    line_callback(f"Streaming {fpath}")
                ok = self.stream_file(src_serial, fpath, dst_serial, remote_dest(fpath),
                                      cancel_check=cancel_check, ensure_dir=False)
                if not ok:
                    return False
            return True
        except Exception as e:
            self.logger.error(f"Directory streaming error: {e}")
            self.last_error = str(e)
            return False

    def enable_root(self) -> bool:
        self.last_error = None
        try:
            result = self._run_adb_command(['root'])
            out = (result.stdout or "") + (result.stderr or "")
            if "restarting adbd as root" in out or "adbd is already running as root" in out:
                time.sleep(1)
                self.root_mode = "adb"
                return True
            if "cannot run as root" not in out:
                if result.returncode == 0:
                    time.sleep(1)
                    self.root_mode = "adb"
                    return True
        except Exception:
            pass

        try:
            result = self._run_adb_command(['shell', 'su', '-c', 'id'])
            if result.returncode == 0 and "uid=0" in (result.stdout or ""):
                self.root_mode = "su"
                return True
            self.last_error = (result.stderr or result.stdout or "").strip()
        except Exception as e:
            self.last_error = str(e)

        self.root_mode = None
        return False

    # `ls -la` output differs between toolboxes: toybox prints ISO timestamps,
    # busybox prints "Mon DD HH:MM" for recent files and "Mon DD  YYYY" for old
    # ones, and device nodes carry a "major, minor" pair instead of a size.
    _LS_LINE = re.compile(
        r'^(?P<perms>[-dlcbpsD][rwxsStT-]{9}[+.@]?)\s+'
        r'(?:\d+\s+)?'
        r'(?:\S+\s+){1,2}?'
        r'(?:\d+,\s*)?(?P<size>\d+)\s+'
        r'(?P<date>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?:\s+[+-]\d{4})?'
        r'|[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4}))\s+'
        r'(?P<name>.+)$'
    )

    def list_directory(self, path: str, use_root: bool = False) -> Optional[List[FileItem]]:
        """List a remote directory.

        Returns the entries on success and None on failure, so callers can tell
        an empty directory apart from an error.
        """
        if not self.device_connected:
            self.logger.error("No ADB device connected")
            self.last_error = "No ADB device connected"
            return None

        try:
            self.logger.debug(f"Listing directory: {path}")
            result = self._shell(f'ls -la {shell_quote(path)}', use_root=use_root, timeout=60)

            stdout = result.stdout or ""
            stderr = (result.stderr or "").strip()

            items = []
            links = []
            for line in stdout.splitlines():
                line = line.rstrip()
                if not line or line.startswith('total'):
                    continue

                match = self._LS_LINE.match(line.strip())
                if not match:
                    self.logger.debug(f"Skipping unparsable ls line: {line!r}")
                    continue

                perms = match.group('perms')
                name = match.group('name')
                link_target = None

                if perms.startswith('l') and ' -> ' in name:
                    name, _, link_target = name.partition(' -> ')

                if name in ('.', '..'):
                    continue

                item = FileItem(
                    name=name,
                    path=f"{path.rstrip('/')}/{name}",
                    is_dir=perms.startswith('d'),
                    size=int(match.group('size')),
                    permissions=perms,
                    date_modified=re.sub(r'\s+', ' ', match.group('date')),
                    link_target=link_target,
                )
                items.append(item)
                if item.is_link:
                    links.append(item)

            if not items and (result.returncode != 0 or stderr):
                self.last_error = stderr or stdout.strip() or f"Could not list {path}"
                self.logger.error(f"ADB Error listing {path}: {self.last_error}")
                return None

            if result.returncode != 0 and stderr:
                # Partial listings happen on directories with unreadable entries.
                self.logger.warning(f"ls reported an error for {path}: {stderr}")

            # Symlinks to directories must be navigable, so resolve them all in
            # one shell round-trip instead of per entry.
            if links:
                for item, is_dir in zip(links, self._resolve_dirs([i.path for i in links], use_root)):
                    item.is_dir = is_dir

            self.last_error = None
            self.logger.debug(f"Successfully parsed {len(items)} items")
            return items
        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout listing directory: {path}")
            self.last_error = f"Timed out listing {path}"
            return None
        except Exception as e:
            self.logger.error(f"Error listing directory: {e}")
            self.last_error = str(e)
            return None

    def _resolve_dirs(self, paths: List[str], use_root: bool = False) -> List[bool]:
        """Test many remote paths for directory-ness in a single shell call."""
        if not paths:
            return []
        try:
            quoted = ' '.join(shell_quote(p) for p in paths)
            command = f'for p in {quoted}; do if [ -d "$p" ]; then echo d; else echo f; fi; done'
            result = self._shell(command, use_root=use_root, timeout=30)
            answers = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            if len(answers) != len(paths):
                self.logger.debug("Unexpected directory-test output; treating links as files")
                return [False] * len(paths)
            return [a == 'd' for a in answers]
        except Exception as e:
            self.logger.debug(f"Could not resolve symlink targets: {e}")
            return [False] * len(paths)

    def pull_file(self, remote_path: str, local_path: str) -> bool:
        return self._run_transfer(['pull', remote_path, local_path],
                                  f'pulling {remote_path}')

    def _run_transfer(self, command: list, description: str) -> bool:
        """Run an adb push/pull, keeping the process cancellable and reporting why it failed."""
        if not self.device_connected:
            self.last_error = "No ADB device connected"
            self.logger.error("No ADB device connected")
            return False

        process = None
        try:
            process = subprocess.Popen(
                self._build_adb_cmd(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **self._hidden_kwargs(),
            )
            self._active_process = process
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                output = (stderr or b'').decode('utf-8', errors='replace').strip()
                self.last_error = output or (stdout or b'').decode('utf-8', errors='replace').strip()                     or f"adb exited {process.returncode} while {description}"
                self.logger.error(f"Error {description}: {self.last_error}")
                return False

            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            self.logger.error(f"Error {description}: {e}")
            return False
        finally:
            self._active_process = None
            if process and process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass

    def _run_transfer_streaming(self, command, progress_callback=None, line_callback=None):
        if not self.device_connected:
            self.last_error = "No ADB device connected"
            return False

        try:
            cmd = self._build_adb_cmd(command)
        except ADBNotFoundError as e:
            self.last_error = str(e)
            self.logger.error(self.last_error)
            return False

        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **self._hidden_kwargs(),
            )
            self._active_process = process

            # adb reports progress and errors on the same stream; keep the tail
            # so a failure can be explained instead of just reported as False.
            recent_lines = []
            for raw_line in iter(process.stdout.readline, b''):
                line = raw_line.decode('utf-8', errors='replace').rstrip('\r\n')
                if line:
                    recent_lines.append(line)
                    del recent_lines[:-10]
                if line_callback:
                    line_callback(line)

            process.wait()
            if process.returncode != 0:
                self.last_error = '\n'.join(recent_lines) or f"adb exited {process.returncode}"
                self.logger.error(f"Transfer failed: {self.last_error}")
                return False

            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            self.logger.error(f"Error during streaming transfer: {e}")
            return False
        finally:
            self._active_process = None
            if process and process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass

    def pull_file_streaming(self, remote_path, local_path, line_callback=None):
        return self._run_transfer_streaming(['pull', remote_path, local_path], line_callback=line_callback)

    def push_file_streaming(self, local_path, remote_path, line_callback=None):
        return self._run_transfer_streaming(['push', local_path, remote_path], line_callback=line_callback)

    def rename_item(self, old_path: str, new_path: str) -> bool:
        return self._shell_action(
            f'mv -- {shell_quote(old_path)} {shell_quote(new_path)}',
            f'renaming {old_path}',
        )

    def delete_item(self, path: str, is_dir: bool = False) -> bool:
        flags = '-rf' if is_dir else '-f'
        return self._shell_action(
            f'rm {flags} -- {shell_quote(path)}',
            f'deleting {path}',
            timeout=120 if is_dir else 30,
        )

    def create_file(self, remote_path: str) -> bool:
        return self._shell_action(
            f'touch -- {shell_quote(remote_path)}',
            f'creating {remote_path}',
        )

    def create_folder(self, remote_path: str) -> bool:
        return self._shell_action(
            f'mkdir -p -- {shell_quote(remote_path)}',
            f'creating {remote_path}',
        )

    def push_file(self, local_path: str, remote_path: str) -> bool:
        return self._run_transfer(['push', local_path, remote_path],
                                  f'pushing {os.path.basename(local_path)}')

    def copy_on_device(self, src_path: str, dest_path: str) -> bool:
        return self._transfer_on_device('cp -r', src_path, dest_path, 'copying')

    def move_on_device(self, src_path: str, dest_path: str) -> bool:
        return self._transfer_on_device('mv', src_path, dest_path, 'moving')

    def _transfer_on_device(self, tool: str, src_path: str, dest_path: str, verb: str) -> bool:
        parent_dir = '/'.join(dest_path.split('/')[:-1])
        if parent_dir and not self.create_folder(parent_dir):
            return False

        return self._shell_action(
            f'{tool} -- {shell_quote(src_path)} {shell_quote(dest_path)}',
            f'{verb} {src_path} to {dest_path}',
            timeout=600,
        )

    def path_exists(self, path: str) -> bool:
        if not self.device_connected:
            return False
        try:
            result = self._shell(f'test -e {shell_quote(path)} && echo yes || echo no')
            return 'yes' in (result.stdout or "")
        except Exception as e:
            self.logger.error(f"Error checking path existence: {e}")
            return False

    def paths_exist(self, paths: List[str]) -> Dict[str, bool]:
        """Test many remote paths in one shell call instead of one call each."""
        if not paths:
            return {}
        wanted = list(dict.fromkeys(paths))
        try:
            quoted = ' '.join(shell_quote(p) for p in wanted)
            command = f'for p in {quoted}; do if [ -e "$p" ]; then echo y; else echo n; fi; done'
            result = self._shell(command, timeout=60)
            answers = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
            if len(answers) != len(wanted):
                self.logger.warning("Unexpected existence-test output; falling back to per-path checks")
                return {p: self.path_exists(p) for p in wanted}
            return dict(zip(wanted, [a == 'y' for a in answers]))
        except Exception as e:
            self.logger.error(f"Error checking paths: {e}")
            return {p: False for p in wanted}

    def push_items(self, local_paths: List[str], remote_dir: str, line_callback=None,
                   sync: bool = False) -> bool:
        """Push files and whole folders in a single adb push.

        adb pushes directories recursively and accepts several sources at once,
        which is far faster than one invocation per file. `sync=True` skips
        files that are already identical on the device.
        """
        if not local_paths:
            return True
        if not self.create_folder(remote_dir):
            return False
        command = ['push']
        if sync:
            command.append('--sync')
        command += [*local_paths, remote_dir.rstrip('/') + '/']
        return self._run_transfer_streaming(command, line_callback=line_callback)

    def pull_items(self, remote_paths: List[str], local_dir: str, line_callback=None) -> bool:
        """Pull files and whole folders in a single adb pull."""
        if not remote_paths:
            return True
        os.makedirs(local_dir, exist_ok=True)
        return self._run_transfer_streaming(
            ['pull', *remote_paths, local_dir],
            line_callback=line_callback,
        )

    def disk_usage_many(self, paths: List[str]) -> int:
        """Total size of several remote paths, measured in one shell call."""
        if not paths:
            return 0
        try:
            quoted = ' '.join(shell_quote(p) for p in paths)
            result = self._shell(f'du -sk {quoted}', timeout=120)
            total = 0
            for line in (result.stdout or "").splitlines():
                first = line.split()
                if first and first[0].isdigit():
                    total += int(first[0]) * 1024
            return total
        except Exception as e:
            self.logger.debug(f"du failed for {paths}: {e}")
            return 0

    def disk_usage(self, path: str) -> Optional[int]:
        """Total size of a remote file or folder in bytes, or None if unknown."""
        try:
            result = self._shell(f'du -sk {shell_quote(path)}', timeout=120)
            first = (result.stdout or "").strip().split()
            if first and first[0].isdigit():
                return int(first[0]) * 1024
        except Exception as e:
            self.logger.debug(f"du failed for {path}: {e}")
        return None

    def install_apk(self, apk_path: str) -> bool:
        if not self.device_connected:
            raise ConnectionError("No ADB device connected")
        result = self._run_adb_command(["install", "-r", apk_path], timeout=120)
        output = (result.stdout or "") + (result.stderr or "")
        self.last_error = None if result.returncode == 0 else output.strip()
        return result.returncode == 0 and "Success" in output

    def install_xapk(self, xapk_path: str, callback=None) -> int:
        if not self.device_connected:
            raise ConnectionError("No ADB device connected")

        self.last_error = None
        try:
            with tempfile.TemporaryDirectory(prefix="xapk_") as tmp:
                with zipfile.ZipFile(xapk_path, 'r') as zf:
                    self._extract_zip_safely(zf, tmp)

                apks = self._find_files(tmp, ".apk")
                if not apks:
                    raise ValueError("No APK files found in XAPK archive")

                for apk in apks:
                    if callback:
                        callback(os.path.basename(apk))

                if len(apks) == 1:
                    command = ["install", "-r", apks[0]]
                else:
                    command = ["install-multiple", "-r", *apks]

                result = self._run_adb_command(command, timeout=max(300, 90 * len(apks)))
                output = (result.stdout or "") + (result.stderr or "")
                if result.returncode != 0 or "Success" not in output:
                    self.last_error = output.strip()
                    raise RuntimeError(self.last_error or "XAPK installation failed")

                self._install_xapk_obb_files(tmp, callback=callback)
                self.last_error = None
                return len(apks)
        except Exception as e:
            if not self.last_error:
                self.last_error = str(e)
            raise

    def _extract_zip_safely(self, zf: zipfile.ZipFile, target_dir: str):
        target_dir = os.path.abspath(target_dir)
        for member in zf.infolist():
            target_path = os.path.abspath(os.path.join(target_dir, member.filename))
            if target_path != target_dir and not target_path.startswith(target_dir + os.sep):
                raise ValueError(f"Unsafe path in archive: {member.filename}")
            zf.extract(member, target_dir)

    def _find_files(self, root: str, suffix: str):
        matches = []
        suffix = suffix.lower()
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for filename in files:
                if filename.lower().endswith(suffix):
                    matches.append(os.path.join(current, filename))
        return sorted(matches, key=lambda p: (os.path.basename(p).lower() != "base.apk", p.lower()))

    def _install_xapk_obb_files(self, extracted_dir: str, callback=None):
        obbs = self._find_files(extracted_dir, ".obb")
        if not obbs:
            return

        package_name = self._read_xapk_package_name(extracted_dir)
        if not package_name:
            self.logger.warning("XAPK contains OBB files but no package_name was found in manifest.json")
            return

        remote_dir = f"/sdcard/Android/obb/{package_name}"
        self.create_folder(remote_dir)
        for obb in obbs:
            if callback:
                callback(os.path.basename(obb))
            remote_path = f"{remote_dir}/{os.path.basename(obb)}"
            if not self.push_file(obb, remote_path):
                raise RuntimeError(f"Failed to push OBB file: {os.path.basename(obb)}")

    def _read_xapk_package_name(self, extracted_dir: str):
        manifest_path = os.path.join(extracted_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            return manifest.get("package_name") or manifest.get("package")
        except Exception as e:
            self.logger.warning(f"Could not read XAPK manifest: {e}")
            return None
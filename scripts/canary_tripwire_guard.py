#!/usr/bin/env python3
"""Deploy decoy credentials and trip an Air-Gap command on Linux file access.

The trip marker is durable and remains set until an operator removes it. inotify
reports access after the syscall, so this guard cannot prevent the first read.
"""

import argparse
from collections import deque
import ctypes
import fcntl
import hashlib
import json
import os
import select
import shutil
import stat
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
STATIC_LOCATIONS = (".canary-credentials.json", "data/.canary-credentials.json",
                    "scripts/.canary-credentials.json")
DEFAULT_COMMAND = ("nmcli", "networking", "off")
IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_OPEN = 0x00000020
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
EVENT_MASK = (IN_ACCESS | IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_OPEN |
              IN_DELETE_SELF | IN_MOVE_SELF)
EVENT = struct.Struct("iIII")


class TripwireError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def state_paths(root, state_dir=None):
    root = Path(root).resolve(strict=True)
    state = Path(state_dir) if state_dir is not None else root / "var/canary-tripwire"
    if ".." in state.parts:
        raise TripwireError("state directory may not contain '..'")
    state = state.absolute()
    if not state.is_relative_to(root) or state == root:
        raise TripwireError("state directory must be inside the workspace")
    for parent in (state, *state.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise TripwireError("state directory contains a symlink")
    return root, state


def safe_file(root, relative):
    if not isinstance(relative, str):
        raise TripwireError("invalid canary path")
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.is_relative_to(root):
        raise TripwireError("canary path escapes workspace")
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise TripwireError("canary path contains a symlink")
    return path


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise TripwireError("canary is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def write_exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def replace_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name("." + path.name + "." + token_hex(8))
    try:
        write_exclusive(temporary, data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def decoy(kind):
    return (json.dumps({"service": "workspace-internal", "api_key": "canary_" + token_hex(24),
                        "kind": kind, "notice": "DECOY ONLY - not a usable credential"},
                       sort_keys=True) + "\n").encode("utf-8")


def entry(root, path, kind, data):
    return {"path": path.relative_to(root).as_posix(), "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest()}


def dynamic_file(root, state):
    directory = state / "dynamic"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ("session-" + token_hex(12) + ".json")
    data = decoy("dynamic")
    write_exclusive(path, data)
    return entry(root, path, "dynamic", data)


def validate_manifest(root, state, manifest):
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or len(files) != len(STATIC_LOCATIONS) + 1:
        raise TripwireError("invalid canary manifest")
    static = set()
    dynamic = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "kind", "sha256"}:
            raise TripwireError("invalid canary manifest entry")
        path = safe_file(root, item["path"])
        if item["kind"] == "static" and item["path"] in STATIC_LOCATIONS:
            static.add(item["path"])
        elif item["kind"] == "dynamic" and path.parent == state / "dynamic" and \
                path.name.startswith("session-") and path.suffix == ".json":
            dynamic.append(item["path"])
        else:
            raise TripwireError("unexpected canary path")
        try:
            actual = hashlib.sha256(read_regular(path)).hexdigest()
        except OSError as error:
            raise TripwireError("canary missing or inaccessible: " + item["path"]) from error
        if actual != item["sha256"]:
            raise TripwireError("canary changed: " + item["path"])
    if static != set(STATIC_LOCATIONS) or len(dynamic) != 1:
        raise TripwireError("incomplete canary manifest")
    return files


def load_manifest(root, state):
    try:
        manifest = json.loads(read_regular(state / "manifest.json"))
    except (OSError, ValueError) as error:
        raise TripwireError("canary manifest missing or invalid") from error
    return validate_manifest(root, state, manifest)


@contextmanager
def monitor_lock(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state / "monitor.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TripwireError("another canary monitor is active") from error
        yield
    finally:
        os.close(fd)


def _deploy_unlocked(root, state):
    if (state / "airgap.tripped").exists():
        raise TripwireError("Air-Gap circuit breaker is already tripped")
    manifest_path = state / "manifest.json"
    if manifest_path.exists():
        return load_manifest(root, state)
    created = []
    try:
        files = []
        for relative in STATIC_LOCATIONS:
            path = safe_file(root, relative)
            if not path.parent.is_dir():
                raise TripwireError("missing canary directory: " + str(path.parent))
            data = decoy("static")
            write_exclusive(path, data)
            created.append(path)
            files.append(entry(root, path, "static", data))
        item = dynamic_file(root, state)
        created.append(safe_file(root, item["path"]))
        files.append(item)
        write_exclusive(manifest_path, (json.dumps({"files": files}, indent=2) + "\n").encode())
        return files
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def deploy(root=DEFAULT_ROOT, state_dir=None):
    root, state = state_paths(root, state_dir)
    with monitor_lock(state):
        return _deploy_unlocked(root, state)


class AirGapBreaker:
    def __init__(self, state, command=DEFAULT_COMMAND, runner=None):
        self.state = Path(state)
        self.command = tuple(command)
        self.runner = runner if runner is not None else subprocess.run
        if not self.command:
            raise TripwireError("Air-Gap command is required")

    def trip(self, reason, path=None):
        marker = self.state / "airgap.tripped"
        if not marker.exists():
            payload = {"at": utc_now(), "reason": reason, "path": path}
            try:
                write_exclusive(marker, (json.dumps(payload, sort_keys=True) + "\n").encode())
            except FileExistsError:
                pass
        self.enforce_pending()

    def enforce_pending(self):
        if (self.state / "airgap.applied").exists():
            return
        if not (self.state / "airgap.tripped").exists():
            return
        try:
            self.runner(self.command, check=True, timeout=10,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise TripwireError("Air-Gap command failed; trip marker remains set") from error
        write_exclusive(self.state / "airgap.applied", (utc_now() + "\n").encode())


class InotifyWatcher:
    def __init__(self):
        if sys.platform != "linux":
            raise TripwireError("Linux inotify is required")
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.paths = {}
        self._queue = deque()

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def add(self, path):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), EVENT_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch failed: " + str(path))
        self.paths[wd] = Path(path)
        return wd

    def remove(self, path):
        for wd, watched in list(self.paths.items()):
            if watched == Path(path):
                del self.paths[wd]
                self.libc.inotify_rm_watch(self.fd, wd)

    def poll(self, timeout):
        if self._queue:
            return self._queue.popleft()
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        data = os.read(self.fd, 65536)
        offset = 0
        while offset + EVENT.size <= len(data):
            wd, mask, _cookie, length = EVENT.unpack_from(data, offset)
            offset += EVENT.size + length
            if mask & IN_Q_OVERFLOW:
                self._queue.append({"reason": "inotify event queue overflow", "path": None})
            elif wd in self.paths and mask & EVENT_MASK:
                self._queue.append({"reason": "canary file accessed or changed",
                                    "path": str(self.paths[wd])})
            elif wd in self.paths and mask & IN_IGNORED:
                self._queue.append({"reason": "canary watch lost", "path": str(self.paths[wd])})
        return self._queue.popleft() if self._queue else None


def rotate_dynamic(root, state, files, watcher):
    old = next(item for item in files if item["kind"] == "dynamic")
    old_path = safe_file(root, old["path"])
    new = dynamic_file(root, state)
    new_path = safe_file(root, new["path"])
    try:
        watcher.add(new_path)
        updated = [item for item in files if item["kind"] == "static"] + [new]
        replace_json(state / "manifest.json", {"files": updated})
    except Exception:
        watcher.remove(new_path)
        new_path.unlink(missing_ok=True)
        raise
    watcher.remove(old_path)
    old_path.unlink(missing_ok=True)
    return updated


def monitor(root=DEFAULT_ROOT, state_dir=None, command=DEFAULT_COMMAND, rotate_seconds=3600):
    root, state = state_paths(root, state_dir)
    breaker = AirGapBreaker(state, command)
    with monitor_lock(state):
        if (state / "airgap.tripped").exists():
            breaker.enforce_pending()
            return 2
        try:
            files = _deploy_unlocked(root, state)
        except TripwireError:
            breaker.trip("canary integrity failure")
            raise
        try:
            with InotifyWatcher() as watcher:
                for item in files:
                    watcher.add(safe_file(root, item["path"]))
                deadline = time.monotonic() + rotate_seconds
                while True:
                    event = watcher.poll(max(0, deadline - time.monotonic()))
                    if event:
                        path = event["path"]
                        relative = str(Path(path).relative_to(root)) if path else None
                        breaker.trip(event["reason"], relative)
                        return 2
                    if time.monotonic() >= deadline:
                        files = rotate_dynamic(root, state, files, watcher)
                        deadline = time.monotonic() + rotate_seconds
        except OSError as error:
            breaker.trip("canary monitoring failed")
            raise TripwireError("canary monitoring failed") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "monitor", "status"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--rotate-seconds", type=float, default=3600)
    parser.add_argument("--breaker-command", nargs="+", default=list(DEFAULT_COMMAND),
                        help="command and arguments used to disable network access")
    args = parser.parse_args(argv)
    try:
        root, state = state_paths(args.root, args.state_dir)
        if args.action == "status":
            print("tripped" if (state / "airgap.tripped").exists() else "armed" if
                  (state / "manifest.json").exists() else "not deployed")
            return 0
        if args.action == "deploy":
            files = deploy(root, state)
            print("Deployed %d canaries" % len(files))
            return 0
        if args.rotate_seconds <= 0:
            raise TripwireError("rotation interval must be positive")
        if shutil.which(args.breaker_command[0]) is None:
            raise TripwireError("Air-Gap command is unavailable")
        return monitor(root, state, args.breaker_command, args.rotate_seconds)
    except (OSError, TripwireError, ValueError) as error:
        print("canary tripwire: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

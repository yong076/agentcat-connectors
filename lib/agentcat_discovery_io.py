"""Local mount policy and disposable, deadline-bound discovery IO.

Only the worker touches candidate paths. A blocked filesystem call cannot hold
the snapshot thread or survive into the next scan. No CLI or network is called.
"""
from __future__ import annotations

import ctypes
import errno
import multiprocessing
import os
from pathlib import Path
import re
import sys
import time


class DiscoveryTimeout(TimeoutError):
    pass


class LocalMounts:
    """Use cached mount metadata, never statfs a possibly hung mount target."""
    LINUX_LOCAL = frozenset(("ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs",
                             "overlay", "aufs", "ramfs", "ufs", "vfat", "exfat", "ntfs",
                             "ntfs3", "f2fs", "jfs", "reiserfs", "bcachefs", "squashfs"))

    def __init__(self, mounts=None):
        self.mounts = self._load() if mounts is None else mounts

    @staticmethod
    def _darwin():
        class StatFS(ctypes.Structure):
            _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
                        ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
                        ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
                        ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
                        ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
                        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
                        ("fstype", ctypes.c_char * 16), ("mount", ctypes.c_char * 1024),
                        ("source", ctypes.c_char * 1024), ("flags_ext", ctypes.c_uint32),
                        ("reserved", ctypes.c_uint32 * 7)]
        libc = ctypes.CDLL(None, use_errno=True)
        getfsstat = libc.getfsstat64
        getfsstat.argtypes = [ctypes.POINTER(StatFS), ctypes.c_int, ctypes.c_int]
        getfsstat.restype = ctypes.c_int
        count = getfsstat(None, 0, 2)  # MNT_NOWAIT: cached flags, no mount IO.
        if count < 0:
            raise OSError(ctypes.get_errno(), "mount metadata unavailable")
        buffer = (StatFS * (count + 16))()
        count = getfsstat(buffer, ctypes.sizeof(buffer), 2)
        if count < 0:
            raise OSError(ctypes.get_errno(), "mount metadata unavailable")
        return [(Path(os.fsdecode(row.mount)), bool(row.flags & 0x1000))  # MNT_LOCAL
                for row in buffer[:count]]

    def _load(self):
        try:
            if sys.platform == "darwin":
                return self._darwin()
            if sys.platform.startswith("linux"):
                mounts = []
                with open("/proc/self/mountinfo", encoding="utf-8") as handle:
                    for line in handle:
                        before, after = line.split(" - ", 1)
                        fields = before.split()
                        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
                        mounts.append((Path(mount), after.split()[0] in self.LINUX_LOCAL))
                return mounts
        except (OSError, ValueError, AttributeError):
            pass
        return []  # Unknown mounts fail closed for automatic discovery.

    def is_local(self, path):
        path = Path(os.path.abspath(path))  # Lexical only; do not resolve links.
        if os.name == "nt":
            # UNC and mapped network drives are rejected without touching them.
            if str(path).startswith("\\\\"):
                return False
            drive_type = ctypes.windll.kernel32.GetDriveTypeW
            drive_type.argtypes = [ctypes.c_wchar_p]
            drive_type.restype = ctypes.c_uint
            return drive_type(str(path.anchor)) in (2, 3, 5, 6)
        matches = [(len(str(root)), local) for root, local in self.mounts
                   if path == root or root in path.parents]
        return max(matches, default=(0, False))[1]


def _local(fs, path):
    return fs.is_local(path) and fs.is_local(fs.realpath(path))


def _io_worker(connection, fs_type, deadline):
    fs = fs_type()
    iterators = {}
    next_id = 0
    try:
        while True:
            operation, args, local_only = connection.recv()
            if operation == "stop":
                break
            try:
                if time.monotonic() >= deadline:
                    connection.send(("timeout", None))
                    continue
                if operation == "next":
                    iterator = iterators[args[0]]
                    batch = []
                    for _ in range(32):
                        if time.monotonic() >= deadline:
                            raise DiscoveryTimeout()
                        child = next(iterator, None)
                        if child is None:
                            iterators.pop(args[0], None)
                            break
                        batch.append(child)
                    result = (batch, args[0] in iterators)
                elif operation == "close":
                    iterator = iterators.pop(args[0], None)
                    if iterator is not None and hasattr(iterator, "close"):
                        iterator.close()
                    result = None
                elif local_only and not _local(fs, args[0]):
                    if operation in ("stat", "realpath", "read"):
                        raise OSError(errno.EXDEV, "non-local discovery path")
                    result = {"children": None, "glob": [], "sqlite_ids": (set(), True)}.get(operation, False)
                elif operation == "children":
                    next_id += 1
                    iterators[next_id] = iter(fs.children(*args))
                    result = next_id
                else:
                    result = getattr(fs, operation)(*args)
                connection.send(("ok", result))
            except DiscoveryTimeout:
                connection.send(("timeout", None))
            except OSError as exc:
                connection.send(("error", exc.errno or errno.EIO))
            except Exception:
                connection.send(("error", errno.EIO))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        for iterator in iterators.values():
            if hasattr(iterator, "close"):
                iterator.close()
        connection.close()


class BoundedFS:
    """One worker per scan; every request shares the caller's global deadline."""
    now = staticmethod(time.monotonic)

    def __init__(self, deadline, fs_type=None):
        if fs_type is None:
            from agentcat_home_signatures import LocalFS
            fs_type = LocalFS
        self.deadline = deadline
        self.fs_type = fs_type
        self._process = None
        self._connection = None
        self._cache = {}

    @property
    def alive(self):
        return self._process is not None and self._process.is_alive()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self.alive:
            self._process.terminate()  # Only the process this scan started.
            self._process.join(0.05)
            if self.alive:
                self._process.kill()
                self._process.join(0.05)
        elif self._process is not None:
            self._process.join(0)

    def _request(self, operation, *args, local_only=False):
        if self.now() >= self.deadline:
            raise DiscoveryTimeout()
        key = (operation, args, local_only)
        cacheable = operation in ("stat", "is_dir", "is_file", "is_symlink", "realpath", "is_local")
        if cacheable and key in self._cache:
            return self._cache[key]
        if self._process is None:
            context = multiprocessing.get_context("spawn")
            self._connection, child = context.Pipe()
            self._process = context.Process(target=_io_worker, args=(child, self.fs_type, self.deadline), daemon=True)
            self._process.start()
            child.close()
        try:
            self._connection.send((operation, args, local_only))
            if not self._connection.poll(max(0, self.deadline - self.now())):
                raise DiscoveryTimeout()
            status, result = self._connection.recv()
        except (EOFError, BrokenPipeError, OSError) as exc:
            raise DiscoveryTimeout() from exc
        if status == "timeout":
            raise DiscoveryTimeout()
        if status == "error":
            raise OSError(result, "discovery IO failed")
        if cacheable:
            self._cache[key] = result
        return result

    def children(self, path, *, local_only=False):
        identifier = self._request("children", path, local_only=local_only)
        if identifier is None:
            return
        more = True
        try:
            while more:
                batch, more = self._request("next", identifier)
                yield from batch
        finally:
            if more and self.now() < self.deadline:
                self._request("close", identifier)

    def __getattr__(self, operation):
        if operation not in ("stat", "is_dir", "is_file", "is_symlink", "realpath", "read", "is_local", "glob", "sqlite_ids"):
            raise AttributeError(operation)
        return lambda *args: self._request(operation, *args)


class LocalOnlyFS:
    """Also guard nested mounts and launcher paths, before stat or scandir."""
    def __init__(self, fs):
        self.fs = fs
        self.now = fs.now

    def __getattr__(self, operation):
        if operation == "is_local":
            return self.fs.is_local
        if operation == "children" and isinstance(self.fs, BoundedFS):
            return lambda path: self.fs.children(path, local_only=True)
        if isinstance(self.fs, BoundedFS):
            return lambda *args: self.fs._request(operation, *args, local_only=True)
        def call(path, *args):
            if not _local(self.fs, path):
                if operation in ("stat", "realpath", "read"):
                    raise OSError(errno.EXDEV, "non-local discovery path")
                return {"children": (), "glob": [], "sqlite_ids": (set(), True)}.get(operation, False)
            return getattr(self.fs, operation)(path, *args)
        return call

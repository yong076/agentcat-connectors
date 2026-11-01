"""Bounded append cursors retaining only a caller's usage metadata projection."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import threading


class ProjectedJSONLCursor:
    def __init__(self, maximum_events=20000, maximum_files=128, maximum_metadata_bytes=8 * 1024 * 1024):
        self.entries = OrderedDict()
        self.maximum_events = maximum_events
        self.maximum_files = maximum_files
        self.maximum_metadata_bytes = maximum_metadata_bytes
        self.lock = threading.Lock()

    def read(self, path: Path, project, maximum_bytes):
        with self.lock:
            return self._read(path, project, maximum_bytes)

    def _read(self, path, project, maximum_bytes):
        try:
            stat = path.stat()
        except OSError:
            self.entries.pop(str(path), None)
            return []
        key = str(path)
        identity = (stat.st_dev, stat.st_ino)
        stamp = (stat.st_size, stat.st_mtime_ns)
        cached = self.entries.pop(key, None)
        if cached is not None and cached["identity"] == identity and cached["stamp"] == stamp:
            self.entries[key] = cached
            return [row[2] for row in cached["rows"]] + cached["tail"]
        start = max(0, stat.st_size - maximum_bytes)
        append = (cached is not None and cached["identity"] == identity
                  and stat.st_size > cached["stamp"][0] and cached["offset"] >= start)
        offset = cached["offset"] if append else start
        rows = [row for row in cached["rows"] if row[0] >= start] if append else []
        tail = []
        try:
            with path.open("rb") as handle:
                if append:
                    # An in-place rewrite can grow too. Hash-only boundary
                    # anchors catch replaced headers/tails without retaining
                    # private text or rereading the historical body.
                    handle.seek(0)
                    prefix = hashlib.sha256(handle.read(cached["prefix_length"])).hexdigest()
                    handle.seek(max(0, cached["stamp"][0] - 256))
                    suffix = hashlib.sha256(handle.read(min(256, cached["stamp"][0]))).hexdigest()
                    if prefix != cached["prefix"] or suffix != cached["suffix"]:
                        append = False
                        offset = start
                        rows = []
                handle.seek(offset)
                if not append and start:
                    handle.readline(maximum_bytes + 1)  # partial first line
                    offset = handle.tell()
                while handle.tell() < stat.st_size:
                    beginning = handle.tell()
                    raw = handle.readline(min(maximum_bytes + 1, stat.st_size - beginning))
                    if not raw:
                        break
                    complete = raw.endswith(b"\n")
                    try:
                        value = json.loads(raw.decode("utf-8", errors="replace"))
                        projected = project(value) if isinstance(value, dict) else None
                    except (ValueError, TypeError):
                        projected = None
                    if not complete:
                        # Render a valid trailing record but do not commit its
                        # byte cursor until newline. A later append replaces it.
                        tail = [projected] if projected is not None else []
                        break
                    offset = handle.tell()
                    if projected is not None:
                        rows.append((beginning, offset, projected))
                prefix_length = min(256, stat.st_size)
                handle.seek(0)
                prefix = hashlib.sha256(handle.read(prefix_length)).hexdigest()
                handle.seek(max(0, stat.st_size - 256))
                suffix = hashlib.sha256(handle.read(min(256, stat.st_size))).hexdigest()
                after = path.stat()
        except OSError:
            return []
        if identity != (after.st_dev, after.st_ino) or stamp != (after.st_size, after.st_mtime_ns):
            # The returned view is from this read; never cache a raced cursor.
            return [row[2] for row in rows] + tail
        result = [row[2] for row in rows] + tail
        metadata_bytes = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
        if len(result) <= self.maximum_events and metadata_bytes <= self.maximum_metadata_bytes:
            self.entries[key] = {"identity": identity, "stamp": stamp,
                                 "offset": offset, "rows": rows, "tail": tail, "bytes": metadata_bytes,
                                 "prefix_length": prefix_length, "prefix": prefix, "suffix": suffix}
        while (len(self.entries) > self.maximum_files or
               sum(len(entry["rows"]) + len(entry["tail"]) for entry in self.entries.values()) > self.maximum_events or
               sum(entry["bytes"] for entry in self.entries.values()) > self.maximum_metadata_bytes):
            self.entries.popitem(last=False)
        return result

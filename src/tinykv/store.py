"""A small key-value store backed by a write-ahead log."""
import os
import shutil
import struct
import zlib
from typing import Dict, Iterator, Union

try:
    import fcntl
except ImportError:  # not POSIX
    fcntl = None

Value = Union[bytes, str]

OP_SET_BYTES = 1
OP_SET_STR = 2
OP_DELETE = 3
_OPS = (OP_SET_BYTES, OP_SET_STR, OP_DELETE)

# crc32 (4) | op (1) | key length (4) | value length (4), then key, then value.
# The crc covers everything after the crc field.
_HEADER = struct.Struct(">IBII")
_CRC_SIZE = 4

# Keys and values over this size are refused on write and treated as garbage on replay.
MAX_FIELD = 1 << 30


def encode_record(op: int, key: bytes, value: bytes = b"") -> bytes:
    body = struct.pack(">BII", op, len(key), len(value)) + key + value
    return struct.pack(">I", zlib.crc32(body)) + body


def _read_records(data: bytes):
    """Yield (end_offset, op, key, value) for each good record, stopping at the first bad one.

    value is a str for OP_SET_STR and bytes otherwise. A record whose crc is fine but whose
    key or str value is not valid utf-8 counts as a bad record.
    """
    pos = 0
    n = len(data)
    while pos + _HEADER.size <= n:
        crc, op, klen, vlen = _HEADER.unpack_from(data, pos)
        if op not in _OPS or klen > MAX_FIELD or vlen > MAX_FIELD:
            return
        end = pos + _HEADER.size + klen + vlen
        if end > n:
            return
        if zlib.crc32(data[pos + _CRC_SIZE:end]) != crc:
            return
        kstart = pos + _HEADER.size
        try:
            key = data[kstart:kstart + klen].decode("utf-8")
            value = data[kstart + klen:end]
            if op == OP_SET_STR:
                value = value.decode("utf-8")
        except UnicodeDecodeError:
            return
        yield end, op, key, value
        pos = end


def _fsync_dir(path: str) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    """Key-value store with a write-ahead log.

    Not thread-safe. Use one Store per file and one process per file. On POSIX a second
    open of the same file fails fast (BlockingIOError) instead of cutting into a live writer.
    Iterating takes a snapshot of the keys, so mutating while iterating is fine.
    """

    def __init__(self, path: str, compact_threshold: int = 1 << 20):
        self.path = os.fspath(path)
        self.compact_threshold = compact_threshold
        self._tmp_path = self.path + ".compact"
        self._lock_path = self.path + ".lock"
        self._data: Dict[str, Value] = {}
        self._live_size = 0
        self._sizes: Dict[str, int] = {}
        self._log_size = 0
        self._f = None
        self._lock = None
        self._broken = False
        self._open()

    # recovery

    def _open(self) -> None:
        self._acquire_lock()
        try:
            self._replay()
        except BaseException:
            self._release_lock()
            raise

    def _acquire_lock(self) -> None:
        if fcntl is None:
            return
        f = open(self._lock_path, "ab")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise
        self._lock = f

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _replay(self) -> None:
        # A leftover temp file means a crash during compaction before the rename.
        # The real log is still complete, so the temp file is just thrown away.
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

        existed = os.path.exists(self.path)
        data = b""
        if existed:
            with open(self.path, "rb") as f:
                data = f.read()

        good = 0
        for end, op, key, value in _read_records(data):
            self._apply(op, key, value, end - good)
            good = end

        self._f = open(self.path, "ab")
        if good < len(data):
            # Torn or corrupt tail: cut it off so new records start on a clean boundary.
            self._f.truncate(good)
            self._f.flush()
            os.fsync(self._f.fileno())
        self._log_size = good
        if not existed:
            _fsync_dir(os.path.dirname(os.path.abspath(self.path)))

    def _apply(self, op: int, key: str, value: Value, size: int) -> None:
        self._live_size -= self._sizes.pop(key, 0)
        if op == OP_DELETE:
            self._data.pop(key, None)
        else:
            self._data[key] = value
            self._sizes[key] = size
            self._live_size += size

    # api

    def set(self, key: str, value: Value) -> None:
        if not isinstance(key, str):
            raise TypeError("key must be a str")
        if isinstance(value, str):
            op, raw = OP_SET_STR, value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            op, raw = OP_SET_BYTES, bytes(value)
        else:
            raise TypeError("value must be bytes or str")
        rawkey = key.encode("utf-8")
        if len(rawkey) > MAX_FIELD or len(raw) > MAX_FIELD:
            raise ValueError("key or value is too large")
        rec = encode_record(op, rawkey, raw)
        self._append(rec)
        self._apply(op, key, value if op == OP_SET_STR else raw, len(rec))
        self._maybe_compact()

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def delete(self, key: str) -> bool:
        """Delete a key. Returns False (and writes nothing) if it wasn't there."""
        if key not in self._data:
            return False
        rec = encode_record(OP_DELETE, key.encode("utf-8"))
        self._append(rec)
        self._apply(OP_DELETE, key, b"", len(rec))
        self._maybe_compact()
        return True

    def keys(self) -> Iterator[str]:
        return iter(list(self._data))

    __iter__ = keys

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def _append(self, rec: bytes) -> None:
        if self._broken or self._f is None:
            raise ValueError("store is closed or broken")
        try:
            self._f.write(rec)
            self._f.flush()
            os.fsync(self._f.fileno())
        except BaseException:
            self._undo_append()
            raise
        self._log_size += len(rec)

    def _undo_append(self) -> None:
        """A failed append may have left part of a record in the file. Cut it off, because
        otherwise later (acknowledged) records would sit behind garbage and be lost on replay."""
        try:
            try:
                self._f.close()
            except OSError:
                pass
            os.truncate(self.path, self._log_size)
            self._f = open(self.path, "ab")
            os.fsync(self._f.fileno())
        except OSError:
            self._broken = True

    # compaction

    def _maybe_compact(self) -> None:
        if self._log_size > self.compact_threshold and self._log_size > 2 * self._live_size:
            try:
                self.compact()
            except OSError:
                pass  # the write already succeeded; compaction is retried on a later write

    def compact(self) -> None:
        """Rewrite the log so it holds only live keys, atomically."""
        if self._broken or self._f is None:
            raise ValueError("store is closed or broken")
        sizes = {}
        try:
            with open(self._tmp_path, "wb") as tmp:
                for key, value in self._data.items():
                    if isinstance(value, str):
                        rec = encode_record(OP_SET_STR, key.encode("utf-8"), value.encode("utf-8"))
                    else:
                        rec = encode_record(OP_SET_BYTES, key.encode("utf-8"), value)
                    tmp.write(rec)
                    sizes[key] = len(rec)
                tmp.flush()
                os.fsync(tmp.fileno())
            try:
                shutil.copymode(self.path, self._tmp_path)
            except OSError:
                pass
        except BaseException:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass
            raise
        self._f.close()
        try:
            os.replace(self._tmp_path, self.path)
            # The new log is in place, so the bookkeeping must match it even if the
            # directory fsync below fails.
            self._sizes = sizes
            self._live_size = sum(sizes.values())
            self._log_size = self._live_size
            _fsync_dir(os.path.dirname(os.path.abspath(self.path)))
        finally:
            # Whether or not the rename happened, reopen whatever is at self.path.
            self._f = open(self.path, "ab")

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        self._release_lock()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

import os

import pytest

from tinykv import Store
from tinykv.store import OP_DELETE, OP_SET_STR, encode_record

OPS = [
    ("set", "a", "one"),
    ("set", "b", b"two\x00"),
    ("set", "a", "three"),
    ("delete", "b", None),
    ("set", "c", "four"),
]


def build(path, ops):
    """Write ops through a real Store; return the log size after each op."""
    sizes = []
    with Store(path, compact_threshold=1 << 30) as s:
        for op, k, v in ops:
            if op == "set":
                s.set(k, v)
            else:
                s.delete(k)
            sizes.append(os.path.getsize(path))
    return sizes


def expected(ops):
    d = {}
    for op, k, v in ops:
        if op == "set":
            d[k] = v
        else:
            d.pop(k, None)
    return d


def snapshot(s):
    return {k: s.get(k) for k in s}


def test_truncate_at_every_byte_offset(tmp_path):
    path = str(tmp_path / "db.log")
    sizes = build(path, OPS)
    full = open(path, "rb").read()
    for cut in range(len(full) + 1):
        with open(path, "wb") as f:
            f.write(full[:cut])
        complete = sum(1 for sz in sizes if sz <= cut)
        with Store(path) as s:
            assert snapshot(s) == expected(OPS[:complete]), f"cut at {cut}"
        # The torn tail must be gone from disk, leaving a clean record boundary.
        want = sizes[complete - 1] if complete else 0
        assert os.path.getsize(path) == want, f"cut at {cut}"


def test_store_is_writable_after_recovery(tmp_path):
    path = str(tmp_path / "db.log")
    sizes = build(path, OPS)
    full = open(path, "rb").read()
    cut = sizes[2] + 3  # three bytes into the 4th record
    with open(path, "wb") as f:
        f.write(full[:cut])
    with Store(path) as s:
        s.set("new", "x")
    with Store(path) as s:
        assert snapshot(s) == {**expected(OPS[:3]), "new": "x"}


def test_corrupt_trailing_record_dropped(tmp_path):
    path = str(tmp_path / "db.log")
    sizes = build(path, OPS)
    data = bytearray(open(path, "rb").read())
    data[-1] ^= 0xFF  # flip a bit in the last record's value
    open(path, "wb").write(bytes(data))
    with Store(path) as s:
        assert snapshot(s) == expected(OPS[:-1])
    assert os.path.getsize(path) == sizes[-2]


def test_corruption_in_middle_drops_everything_after(tmp_path):
    path = str(tmp_path / "db.log")
    sizes = build(path, OPS)
    data = bytearray(open(path, "rb").read())
    data[sizes[1] + 6] ^= 0xFF  # inside the 3rd record
    open(path, "wb").write(bytes(data))
    with Store(path) as s:
        assert snapshot(s) == expected(OPS[:2])


def test_garbage_length_field_does_not_blow_up(tmp_path):
    path = str(tmp_path / "db.log")
    good = encode_record(OP_SET_STR, b"a", b"1")
    junk = b"\x00\x00\x00\x00" + bytes([OP_SET_STR]) + b"\xff\xff\xff\xff" + b"\xff\xff\xff\xff"
    open(path, "wb").write(good + junk)
    with Store(path) as s:
        assert snapshot(s) == {"a": "1"}


def test_unknown_op_stops_replay(tmp_path):
    path = str(tmp_path / "db.log")
    good = encode_record(OP_SET_STR, b"a", b"1")
    bad = encode_record(99, b"b", b"2")
    open(path, "wb").write(good + bad)
    with Store(path) as s:
        assert snapshot(s) == {"a": "1"}


def test_torn_delete_record_keeps_key(tmp_path):
    path = str(tmp_path / "db.log")
    set_rec = encode_record(OP_SET_STR, b"a", b"1")
    del_rec = encode_record(OP_DELETE, b"a")
    open(path, "wb").write(set_rec + del_rec[:-2])
    with Store(path) as s:
        assert s.get("a") == "1"


# compaction

def test_compaction_keeps_live_drops_dead(tmp_path):
    path = str(tmp_path / "db.log")
    with Store(path, compact_threshold=1 << 30) as s:
        for i in range(50):
            s.set("k", f"v{i}")
        s.set("dead", "x")
        s.delete("dead")
        s.set("live", b"y")
        before = os.path.getsize(path)
        s.compact()
        assert os.path.getsize(path) < before
        assert snapshot(s) == {"k": "v49", "live": b"y"}
        s.set("after", "z")
    with Store(path) as s:
        assert snapshot(s) == {"k": "v49", "live": b"y", "after": "z"}
    assert not os.path.exists(path + ".compact")


def test_auto_compaction_triggers(tmp_path):
    path = str(tmp_path / "db.log")
    with Store(path, compact_threshold=500) as s:
        for i in range(200):
            s.set("k", "x" * 20)
        assert os.path.getsize(path) < 500 + 100
        assert s.get("k") == "x" * 20
    with Store(path) as s:
        assert snapshot(s) == {"k": "x" * 20}


def test_crash_before_rename_leaves_old_log_intact(tmp_path, monkeypatch):
    path = str(tmp_path / "db.log")
    s = Store(path, compact_threshold=1 << 30)
    for i in range(10):
        s.set("k", f"v{i}")
    s.set("other", "o")
    old = open(path, "rb").read()

    def boom(*a, **kw):
        raise RuntimeError("crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        s.compact()
    monkeypatch.undo()
    s.close()

    assert open(path, "rb").read() == old
    with Store(path) as s2:
        assert snapshot(s2) == {"k": "v9", "other": "o"}
    assert not os.path.exists(path + ".compact")


def test_leftover_partial_compact_file_is_ignored(tmp_path):
    path = str(tmp_path / "db.log")
    build(path, OPS)
    old = open(path, "rb").read()
    # A crash while writing the temp file leaves a partial snapshot behind.
    open(path + ".compact", "wb").write(encode_record(OP_SET_STR, b"zzz", b"bad")[:7])
    with Store(path) as s:
        assert snapshot(s) == expected(OPS)
    assert open(path, "rb").read() == old
    assert not os.path.exists(path + ".compact")


def test_crash_after_rename_new_log_is_complete(tmp_path):
    path = str(tmp_path / "db.log")
    s = Store(path, compact_threshold=1 << 30)
    for i in range(10):
        s.set("k", f"v{i}")
    s.compact()
    s._f.close()  # simulate the process dying right after the rename
    with Store(path) as s2:
        assert snapshot(s2) == {"k": "v9"}

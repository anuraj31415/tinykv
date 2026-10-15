import os

import pytest

from tinykv import Store
from tinykv.store import OP_DELETE, OP_SET_STR, _read_records, encode_record

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


def parse(path):
    """Oracle: (end offset, state after that record) for every record in an intact log."""
    out = []
    state = {}
    for end, op, key, value in _read_records(open(path, "rb").read()):
        if op == OP_DELETE:
            state.pop(key, None)
        else:
            state[key] = value
        out.append((end, dict(state)))
    return out


def build_with_compact(path):
    with Store(path, compact_threshold=1 << 30) as s:
        for i in range(8):
            s.set(f"k{i % 3}", f"v{i}")
        s.delete("k1")
        s.compact()
        s.set("after", b"x")
        s.delete("k0")
        s.set("k2", "again")


OTHER_OPS = [
    ("set", "", "empty key"),
    ("set", "h\u00e9llo", "w\u00f6rld \u2603"),
    ("set", "e", b""),
    ("set", "", b"\xff\x00"),
    ("delete", "h\u00e9llo", None),
    ("delete", "nope", None),
    ("set", "z", ""),
]


@pytest.mark.parametrize("case", ["basic", "unicode_empty", "compacted"])
def test_truncate_at_every_byte_offset(tmp_path, case):
    path = str(tmp_path / "db.log")
    if case == "basic":
        build(path, OPS)
    elif case == "unicode_empty":
        build(path, OTHER_OPS)
    else:
        build_with_compact(path)
    full = open(path, "rb").read()
    records = parse(path)
    assert records and records[-1][0] == len(full)
    for cut in range(len(full) + 1):
        with open(path, "wb") as f:
            f.write(full[:cut])
        done = [r for r in records if r[0] <= cut]
        want_state = done[-1][1] if done else {}
        want_size = done[-1][0] if done else 0
        with Store(path) as s:
            assert snapshot(s) == want_state, f"cut at {cut}"
        # The torn tail must be gone from disk, leaving a clean record boundary.
        assert os.path.getsize(path) == want_size, f"cut at {cut}"


def test_basic_ops_match_expected_state(tmp_path):
    path = str(tmp_path / "db.log")
    build(path, OPS)
    with Store(path) as s:
        assert snapshot(s) == expected(OPS)


@pytest.mark.parametrize("n", [1, 5, 12, 13, 14, 40, 4096])
def test_zero_filled_tail(tmp_path, n):
    path = str(tmp_path / "db.log")
    build(path, OPS)
    full = open(path, "rb").read()
    open(path, "wb").write(full + b"\x00" * n)
    with Store(path) as s:
        assert snapshot(s) == expected(OPS)
    assert open(path, "rb").read() == full


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


def test_reopen_after_compact_without_close(tmp_path):
    path = str(tmp_path / "db.log")
    s = Store(path, compact_threshold=1 << 30)
    for i in range(10):
        s.set("k", f"v{i}")
    s.compact()
    s.close()  # process gone right after the rename
    with Store(path) as s2:
        assert snapshot(s2) == {"k": "v9"}


def test_crash_in_dir_fsync_leaves_store_consistent(tmp_path, monkeypatch):
    import tinykv.store as st

    path = str(tmp_path / "db.log")
    s = Store(path, compact_threshold=1 << 30)
    for i in range(20):
        s.set("k", f"v{i}")
    s.set("other", b"o")

    def boom(_):
        raise OSError("dir fsync failed")

    monkeypatch.setattr(st, "_fsync_dir", boom)
    with pytest.raises(OSError):
        s.compact()
    monkeypatch.undo()
    # The rename happened, so the bookkeeping must describe the new, small log.
    assert s._log_size == os.path.getsize(path) == s._live_size
    s.set("later", "1")
    s.close()
    with Store(path) as s2:
        assert snapshot(s2) == {"k": "v19", "other": b"o", "later": "1"}


def test_failed_temp_write_removes_temp_file(tmp_path, monkeypatch):
    path = str(tmp_path / "db.log")
    with Store(path, compact_threshold=1 << 30) as s:
        s.set("a", "1")
        real = os.fsync
        calls = []

        def flaky(fd):
            calls.append(fd)
            raise OSError("fsync failed")

        monkeypatch.setattr(os, "fsync", flaky)
        with pytest.raises(OSError):
            s.compact()
        monkeypatch.setattr(os, "fsync", real)
        assert not os.path.exists(path + ".compact")
        assert s.get("a") == "1"
        s.set("b", "2")
    with Store(path) as s:
        assert snapshot(s) == {"a": "1", "b": "2"}


def test_invalid_utf8_key_stops_replay(tmp_path):
    path = str(tmp_path / "db.log")
    good = encode_record(OP_SET_STR, b"a", b"1")
    bad = encode_record(OP_SET_STR, b"\xff", b"x")
    after = encode_record(OP_SET_STR, b"b", b"2")
    open(path, "wb").write(good + bad + after)
    with Store(path) as s:
        assert snapshot(s) == {"a": "1"}
    assert open(path, "rb").read() == good


def test_invalid_utf8_str_value_stops_replay(tmp_path):
    path = str(tmp_path / "db.log")
    good = encode_record(OP_SET_STR, b"a", b"1")
    bad = encode_record(OP_SET_STR, b"b", b"\xc3\x28")
    open(path, "wb").write(good + bad)
    with Store(path) as s:
        assert snapshot(s) == {"a": "1"}

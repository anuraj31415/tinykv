import os
import stat

import pytest

from tinykv import Store
import tinykv.store as st


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "db.log")


def snapshot(s):
    return {k: s.get(k) for k in s}


# failed appends

def test_failed_fsync_does_not_poison_later_writes(path, monkeypatch):
    s = Store(path)
    s.set("a", "1")
    real = os.fsync
    state = {"n": 0}

    def flaky(fd):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("fsync failed")
        return real(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    with pytest.raises(OSError):
        s.set("lost", "x")
    monkeypatch.undo()
    assert s.get("lost") is None
    s.set("b", "2")
    s.close()
    with Store(path) as s2:
        assert snapshot(s2) == {"a": "1", "b": "2"}


def test_partial_write_is_cut_off(path):
    s = Store(path)
    s.set("a", "1")

    class HalfWriter:
        def __init__(self, f):
            self.f = f

        def write(self, data):
            self.f.write(data[: len(data) // 2])
            self.f.flush()
            raise OSError("disk full")

        def __getattr__(self, name):
            return getattr(self.f, name)

    s._f = HalfWriter(s._f)
    with pytest.raises(OSError):
        s.set("lost", "x" * 50)
    s.set("b", "2")
    s.close()
    with Store(path) as s2:
        assert snapshot(s2) == {"a": "1", "b": "2"}


def test_auto_compaction_failure_does_not_fail_the_write(path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("rename failed")

    with Store(path, compact_threshold=100) as s:
        monkeypatch.setattr(os, "replace", boom)
        for i in range(30):
            s.set("k", "x" * 20)  # must not raise
        monkeypatch.undo()
        assert s.get("k") == "x" * 20
    with Store(path) as s:
        assert snapshot(s) == {"k": "x" * 20}


def test_explicit_compact_still_raises(path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("rename failed")

    with Store(path) as s:
        s.set("a", "1")
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            s.compact()


# size limits

def test_oversized_key_and_value_refused(path, monkeypatch):
    monkeypatch.setattr(st, "MAX_FIELD", 10)
    with Store(path) as s:
        s.set("k", "x" * 10)
        with pytest.raises(ValueError):
            s.set("k", "x" * 11)
        with pytest.raises(ValueError):
            s.set("k", b"x" * 11)
        with pytest.raises(ValueError):
            s.set("k" * 11, "v")
        assert s.get("k") == "x" * 10
    monkeypatch.undo()
    with Store(path) as s:
        assert snapshot(s) == {"k": "x" * 10}


# durability ordering

def test_set_and_delete_fsync_before_updating_memory(path, monkeypatch):
    s = Store(path)
    seen = []
    real = os.fsync

    def spy(fd):
        seen.append(("k" in s._data, s.get("k")))
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    s.set("k", "1")
    assert seen == [(False, None)]  # fsynced while the key was not yet in memory
    s.set("k", "2")
    assert seen[-1] == (True, "1")  # old value still there at fsync time
    del seen[:]
    s.delete("k")
    assert seen == [(True, "2")]  # still present when the delete record is fsynced
    assert s.get("k") is None
    s.close()


def test_nothing_acknowledged_without_fsync(path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    with Store(path) as s:
        del calls[:]
        s.set("a", "1")
        assert len(calls) >= 1
        n = len(calls)
        s.delete("a")
        assert len(calls) > n


def test_compaction_fsync_order(path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append("dir fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file fsync")
        return real_fsync(fd)

    def spy_replace(a, b):
        events.append("replace")
        return real_replace(a, b)

    with Store(path, compact_threshold=1 << 30) as s:
        s.set("a", "1")
        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        s.compact()
    assert events == ["file fsync", "replace", "dir fsync"]


# misc

def test_compaction_keeps_file_mode(path):
    with Store(path, compact_threshold=1 << 30) as s:
        s.set("a", "1")
        os.chmod(path, 0o600)
        s.compact()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.skipif(st.fcntl is None, reason="needs flock")
def test_second_open_fails_fast(path):
    with Store(path):
        with pytest.raises(OSError):
            Store(path)
    with Store(path):
        pass


def test_use_after_close_raises(path):
    s = Store(path)
    s.close()
    with pytest.raises(ValueError):
        s.set("a", "1")

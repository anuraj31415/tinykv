import pytest

from tinykv import Store


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "db.log")


def test_set_get_delete(path):
    with Store(path) as s:
        s.set("a", "1")
        s.set("b", b"\x00\xff")
        assert s.get("a") == "1"
        assert s.get("b") == b"\x00\xff"
        assert s.delete("a") is True
        assert s.get("a") is None
        assert s.delete("a") is False


def test_iteration_and_len(path):
    with Store(path) as s:
        s.set("x", "1")
        s.set("y", "2")
        s.delete("x")
        assert list(s) == ["y"]
        assert len(s) == 1
        assert "y" in s


def test_bad_types(path):
    with Store(path) as s:
        with pytest.raises(TypeError):
            s.set(1, "a")
        with pytest.raises(TypeError):
            s.set("a", 5)


def test_reopen_keeps_data_and_types(path):
    with Store(path) as s:
        s.set("a", "text")
        s.set("b", b"raw")
        s.set("u", "héllo")
        s.set("a", "text2")
    with Store(path) as s:
        assert s.get("a") == "text2"
        assert s.get("b") == b"raw"
        assert s.get("u") == "héllo"


def test_delete_then_reopen(path):
    with Store(path) as s:
        s.set("a", "1")
        s.set("b", "2")
        s.delete("a")
    with Store(path) as s:
        assert s.get("a") is None
        assert s.get("b") == "2"


def test_empty_log(path):
    open(path, "wb").close()
    with Store(path) as s:
        assert list(s) == []
        s.set("a", "1")
    with Store(path) as s:
        assert s.get("a") == "1"


def test_missing_file_is_created(path):
    with Store(path) as s:
        assert len(s) == 0
    assert open(path, "rb").read() == b""


def test_log_with_only_deletes(path):
    from tinykv.store import OP_DELETE, encode_record

    with open(path, "wb") as f:
        f.write(encode_record(OP_DELETE, b"a"))
        f.write(encode_record(OP_DELETE, b"b"))
    with Store(path) as s:
        assert list(s) == []
        s.set("a", "1")
    with Store(path) as s:
        assert s.get("a") == "1"


def test_empty_value_and_empty_key(path):
    with Store(path) as s:
        s.set("", "")
        s.set("k", b"")
    with Store(path) as s:
        assert s.get("") == ""
        assert s.get("k") == b""

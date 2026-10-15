# tinykv

A small key-value store in Python. Every write goes to a write-ahead log and is fsynced before the call returns.

On open it replays the log, and if the last record was cut off by a crash it drops that record and keeps the rest. The log gets compacted when it grows. Standard library only, Python 3.9+.

## Install

```
pip install .
```

## Usage

```python
from tinykv import Store

with Store("data.log") as db:
    db.set("name", "anu")
    db.set("blob", b"\x00\x01")
    print(db.get("name"))   # anu
    db.delete("name")
    for key in db:
        print(key)
```

Strings come back as strings and bytes as bytes. `Store(path, compact_threshold=...)` sets the log size in bytes after which compaction kicks in (1 MiB by default).

It's single-process and not thread-safe. A second open of the same file fails.

There's also a tiny CLI: `python -m tinykv data.log set key value`.

Tests: `pip install pytest` then `python -m pytest`.

How the log, recovery and compaction work is in [DESIGN.md](DESIGN.md).

# Design

tinykv keeps all keys and values in a dict in memory. The log file is the only thing on disk. It exists so that the dict can be rebuilt after a restart or a crash.

## Log record format

The log is a plain sequence of records, back to back. There is no file header. Each record looks like this, all integers big endian:

```
crc32    4 bytes
op       1 byte    1 = set bytes, 2 = set str, 3 = delete
key len  4 bytes
val len  4 bytes
key      key len bytes (utf-8)
value    val len bytes (empty for delete)
```

The crc32 covers everything after the crc field: op, both lengths, key and value. Having separate ops for bytes and str is how a value keeps its type across a reopen.

## Writing

`set` and `delete` encode one record, write it to the end of the file, flush, and call fsync. Only then does the call return and the in-memory dict change. A delete of a key that doesn't exist writes nothing.

## Recovery

On open the whole log is read and records are applied in order. A record is accepted only if all of these hold:

- the full 13 byte header is there
- the op is one of the three known ones, and the lengths are under a sanity limit (1 GiB), so a garbage length can't make us try to read something huge
- the bytes the header promises are all there
- the crc matches

At the first record that fails any check, replay stops. The file is truncated to the end of the last good record and fsynced, so the next append starts on a clean boundary. A crash while appending can only damage the end of the file, so in the normal case this drops exactly the one record that was being written, and that call never returned to its caller anyway.

If the damage is in the middle of the log (bit rot, not a torn write), the same rule applies: everything from the first bad record on is dropped. There's no resync, because after a bad length field there's no safe way to find the next record start. This is a deliberate choice and it can lose data that was fine, but it never applies a record it can't verify.

A torn tail that happens to leave a valid-looking prefix is caught by the crc. The tests cut the log at every byte offset of a small log and check that the store comes back with exactly the complete records each time.

## Compaction

When the log is bigger than `compact_threshold` and also more than twice the size of the live data, `compact()` runs. It can also be called directly.

1. Write one set record per live key to `<log>.compact`.
2. fsync that file.
3. `os.replace` it over the log. Rename is atomic on POSIX, so the path points at either the old complete log or the new complete one, never a mix.
4. fsync the directory so the rename itself survives a power cut.

A crash before step 3 leaves the old log untouched, plus a stray `.compact` file. On open the stray file is deleted without being read, since the real log is already complete. A crash after step 3 leaves the new log, which is a normal log. The old log is never modified in place, so there is no point at which the data exists only in a half-written file.

The `.compact` file is also not trusted if it looks fine, because we can't tell a finished one from one that stopped right at a record boundary before the fsync.

## Limits

- Everything lives in memory, so the data set has to fit in RAM.
- One process at a time. There is no file locking.
- Directory fsync is skipped on non-POSIX systems.

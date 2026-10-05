# jev-decisions v0.9.0

## The `limit` argument on every store read was decorative

`ledger.read(limit)` did `path.read_text().splitlines()[-limit:]`. The slice
bounded what was **returned**, never what was **read**, decoded and split, so
asking for 10 rows cost the same as asking for 5,000. `closed_loop._read()` was
worse: no limit at all, every call.

The stores are append-only with no size cap, so this was not a constant-factor
cost. Every `metrics()`, every `queue()`, every `digest()`, and every
`jev_ledger action=list` paid O(total history) forever.

Measured on a 3.8 MB, 52,500-row store:

| operation | v0.8.0 | v0.9.0 |
| --- | --- | --- |
| `read(10)` | 11.31 ms | **0.33 ms** |
| `read(200)` | 10.74 ms | **0.92 ms** |
| `read(5000)` | 18.03 ms | 19.19 ms |

`read(10)` used to cost 96% of `read(5000)`. It now costs about 1.7%. A
50,000-row store is a plausible year of history for a busy agent; at that size
the old path spent a third of a second per digest.

`tail_lines()` walks backwards in 64 KB blocks and stops once it has seen
`limit` newlines. A file smaller than one block is read whole. Verified
byte-identical to the old implementation across every window tested, including
a 40,000-row file where the first line is a mid-record fragment and must be
dropped rather than parsed.

## `metrics()` is now cached on (mtime_ns, size)

`metrics()` is a pure function of an append-only file, and `digest()` calls it
along with `queue()`, while the gateway's `snapshot()` calls it directly. Since
the store only grows by appending, `(mtime_ns, size)` identifies its contents
exactly, so a repeat call costs one `stat`.

```
cold 17.46 ms   warm 0.004 ms   4,366x   values identical
```

`append()` drops the cache explicitly, so invalidation does not depend on
filesystem mtime resolution, and a write by another process is still observed
because the key is derived from `stat` rather than from a call.

## Also fixed

- `closed_loop.list_records()` capped its **result** at 500 while reading the
  whole file. It now reads only as far back as it returns.
- The by-`decision_id` lookup still reads whole-file. That is correct: it can
  match any row, so it cannot be bounded the same way.

## Not changed, deliberately

Two structural costs remain, both real and both needing a design decision rather
than a patch:

- **Seven fsync'd appends per tool call** with hooks enabled, four of them
  carrying information derivable from the others. `ledger.append_many()` writing
  N lines under one flock and one fsync is the fix, but it changes the on-disk
  shape and wants its own change with its own tests.
- **The lesson store's read cache never re-reads.** A long-lived process misses
  rules written by the CLI, cron, or another session for its whole lifetime. It
  is also not buying measurable speed, so the fix is to key `_load()` on
  `(mtime_ns, size)`. This one is small and is the recommended next change.

## Verification

```
462 passed, 4 skipped          (422 before, 40 new)
public scan passed
compileall clean
```

Each change was reverted individually to confirm its tests fail: reverting the
metrics cache fails 1, the closed_loop limit fails 1, the tail-only read fails 1,
and un-bounding the block walk fails 2.

A first attempt at these tests used wall-clock thresholds and passed 37/37 with
the fixes reverted, because the whole-file read of a 20,000-row store measures
1.8 ms, which clears any threshold loose enough to be stable on a loaded CI
runner. They now count the bytes actually read off disk, which asserts the
mechanism instead of the speed.

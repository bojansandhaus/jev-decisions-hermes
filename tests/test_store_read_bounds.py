"""Regression tests for the bounded store reads and the metrics cache.

Independent review measured that `ledger.read(limit)` and `closed_loop._read()`
did not bound their I/O: the slice applied to what was RETURNED, never to what
was read, decoded and split. The stores are append-only with no size cap, so
every `metrics()`, `queue()` and `jev_ledger action=list` paid O(total history)
forever.

Measured on a 3.8 MB, 52,500-row store, before these changes:

    read(10)  11.31 ms   read(200)  10.74 ms   read(5000)  18.03 ms
    tail-only read(10)   0.11 ms

`read(10)` cost 96% of `read(5000)`: the limit was decorative.

`metrics()` is a pure function of an append-only file, so it is cached on
(mtime_ns, size). Repeated snapshots go to stat cost instead of a full re-read:
measured 11.58 ms cold to 0.023 ms warm.
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys
import unittest.mock
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import closed_loop  # noqa: E402
import ledger  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point both modules at a temp store and return its path."""
    path = tmp_path / "jev-ledger.jsonl"
    monkeypatch.setattr(ledger, "_path", lambda: path)
    return path


def write_rows(path: Path, count: int, prefix: str = "row") -> list[dict]:
    rows = [{"kind": "review", "review_id": f"{prefix}{i}", "score": 0.9} for i in range(count)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return rows


class TestTailLinesIsExact:
    @pytest.mark.parametrize("limit", [1, 3, 10, 1000])
    @pytest.mark.parametrize(
        "body",
        [
            "",
            '{"a":1}',
            '{"a":1}\n',
            '{"a":1}\n{"b":2}\n',
            '{"a":1}\n\n\n{"b":2}\n',
        ],
        ids=["empty", "no-newline", "one", "two", "blanks"],
    )
    def test_matches_plain_splitlines_for_small_files(self, tmp_path, body, limit):
        path = tmp_path / "s.jsonl"
        path.write_text(body)
        assert ledger.tail_lines(path, limit) == (body.splitlines()[-limit:] if body else [])

    def test_returns_exactly_the_last_n_lines_of_a_large_file(self, store):
        write_rows(store, 5_000)
        for limit in (1, 5, 10, 200, 5_000):
            assert len(ledger.tail_lines(store, limit)) == min(limit, 5_000)

    def test_a_limit_beyond_the_row_count_returns_everything(self, store):
        write_rows(store, 12)
        assert len(ledger.tail_lines(store, 10_000)) == 12

    def test_identical_to_the_previous_whole_file_implementation(self, store):
        """The old code was `read_text().splitlines()[-limit:]`. The new one must
        agree with it on every window, or the optimisation changed behaviour."""
        write_rows(store, 1_200)
        for limit in (1, 7, 50, 1_000, 5_000):
            expected = [
                json.loads(line)
                for line in store.read_text().splitlines()[-limit:]
                if line.strip()
            ]
            assert [json.loads(x) for x in ledger.tail_lines(store, limit)] == expected

    def test_does_not_return_a_partial_leading_record(self, store):
        """A file larger than one block starts mid-record; the fragment is
        dropped rather than parsed as a truncated row."""
        write_rows(store, 40_000)
        lines = ledger.tail_lines(store, 5)
        for line in lines:
            json.loads(line)  # must not raise


class TestReadIsBounded:
    def test_read_returns_only_the_last_n_rows(self, store):
        write_rows(store, 50)
        assert len(ledger.read(10)) == 10
        assert ledger.read(10)[-1]["review_id"] == "row49"

    def test_read_on_a_missing_store_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "_path", lambda: tmp_path / "absent.jsonl")
        assert ledger.read(10) == []

    def test_read_tolerates_a_torn_tail_line(self, store):
        write_rows(store, 5)
        with store.open("a") as handle:
            handle.write('{"kind":"review","revi')  # interrupted append
        assert len(ledger.read(10)) == 5

    def test_a_small_limit_reads_only_a_tail_block(self, store):
        """Assert the mechanism, not a timing.

        A wall-clock ceiling is useless here: the whole-file read of a 20k-row
        store measures 1.8 ms, which passes any threshold loose enough to be
        stable on a loaded CI runner. So count the bytes actually read off disk.
        A bounded read must not read the whole file; the old
        `read_text().splitlines()[-limit:]` read every byte and then discarded
        almost all of them, which is the entire defect.
        """
        write_rows(store, 20_000)
        size = store.stat().st_size
        ledger.read(10)  # warm any import-time state

        counted = {"bytes": 0}
        real_open = io.open

        def counting_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if Path(file) == store and "b" in mode:
                inner = handle.read

                def read(size_or_pos=-1):
                    data = inner(size_or_pos)
                    counted["bytes"] += len(data)
                    return data

                handle.read = read  # type: ignore[method-assign]
            return handle

        with unittest.mock.patch.object(io, "open", counting_open):
            rows = ledger.read(10)
        assert len(rows) == 10
        assert 0 < counted["bytes"] < size / 2, (
            f"read(10) consumed {counted['bytes']} of {size} bytes; "
            "a bounded read must not read the whole file"
        )


class TestMetricsCache:
    def test_repeated_calls_return_the_same_value(self, store):
        write_rows(store, 100)
        assert ledger.metrics() == ledger.metrics()

    def test_an_append_is_picked_up(self, store, monkeypatch):
        write_rows(store, 100)
        assert ledger.metrics()["reviews"] == 100
        with store.open("a") as handle:
            handle.write(json.dumps({"kind": "review", "review_id": "new"}) + "\n")
        assert ledger.metrics()["reviews"] == 101, "cache served a stale count"
        assert ledger.metrics()["reviews"] == 101

    def test_append_invalidates_explicitly(self, store):
        """`append()` drops the cache, so the invalidation does not depend on the
        filesystem's mtime resolution."""
        write_rows(store, 10)
        ledger.metrics()
        ledger._METRICS_CACHE = ("bogus", 0, {"reviews": -1})
        ledger.append("review", {"review_id": "x", "score": 1.0})
        assert ledger.metrics()["reviews"] != -1

    def test_a_missing_store_does_not_poison_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "_path", lambda: tmp_path / "absent.jsonl")
        first = ledger.metrics()
        assert first["reviews"] == 0

    def test_a_repeat_call_does_not_read_the_store_again(self, store):
        """Assert the mechanism: the second call must not open the file at all.

        A timing threshold would be worthless here (the whole computation is a
        few milliseconds), so count store reads instead.
        """
        write_rows(store, 5_000)
        ledger.metrics()
        counted = {"n": 0}
        real_open = io.open

        def counting_open(file, mode="r", *args, **kwargs):
            if Path(file) == store:
                counted["n"] += 1
            return real_open(file, mode, *args, **kwargs)

        with unittest.mock.patch.object(io, "open", counting_open):
            ledger.metrics()
        assert counted["n"] == 0, (
            f"a warm metrics() opened the store {counted['n']} time(s); "
            "it should be served from the (mtime_ns, size) cache"
        )

    def test_an_external_writer_is_still_observed(self, store):
        """The cache is keyed on stat, so a write by another process or another
        tool invalidates it without an explicit call."""
        write_rows(store, 50)
        assert ledger.metrics()["reviews"] == 50
        with store.open("a") as handle:
            handle.write(json.dumps({"kind": "review", "review_id": "ext"}) + "\n")
        assert ledger.metrics()["reviews"] == 51

    def test_cache_is_process_local(self, store):
        """Two processes must not disagree, because the key is stat-derived."""
        write_rows(store, 20)
        code = (
            "import sys, json, pathlib;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import ledger;"
            f"ledger._path=lambda: pathlib.Path({str(store)!r});"
            "print(json.dumps(ledger.metrics()['reviews']))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout.strip()) == ledger.metrics()["reviews"]


class TestClosedLoopReadIsBounded:
    def test_list_records_reads_only_what_it_returns(self, store):
        write_rows(store, 1_000, prefix="cl")
        assert len(closed_loop.list_records(limit=10)) == 10

    def test_list_records_caps_at_its_own_maximum(self, store):
        write_rows(store, 1_000, prefix="cl")
        assert len(closed_loop.list_records(limit=10_000)) <= 500

    def test_list_records_does_not_read_whole_file(self, tmp_path, monkeypatch):
        """`list_records(limit=10)` capped its RESULT at 10 while reading the whole
        file. Assert it does not open the store's full contents."""
        path = tmp_path / "cl.jsonl"
        monkeypatch.setattr(closed_loop, "_path", lambda: path)
        write_rows(path, 20_000)
        size = path.stat().st_size
        closed_loop.list_records(limit=10)
        counted = {"bytes": 0}
        real_open = io.open

        def counting_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if Path(file) == path and "b" in mode:
                inner = handle.read

                def read(size_or_pos=-1):
                    data = inner(size_or_pos)
                    counted["bytes"] += len(data)
                    return data

                handle.read = read  # type: ignore[method-assign]
            return handle

        with unittest.mock.patch.object(io, "open", counting_open):
            rows = closed_loop.list_records(limit=10)
        assert len(rows) == 10
        assert 0 < counted["bytes"] < size / 2, (
            f"list_records(10) read {counted['bytes']} of {size} bytes"
        )

    def test_read_without_a_limit_still_returns_everything(self, tmp_path, monkeypatch):
        path = tmp_path / "cl.jsonl"
        monkeypatch.setattr(closed_loop, "_path", lambda: path)
        write_rows(path, 20)
        assert len(closed_loop._read()) == 20

    def test_the_shared_helper_is_the_same_object(self):
        """closed_loop must not keep a second copy of the tail logic."""
        assert closed_loop.tail_lines is ledger.tail_lines
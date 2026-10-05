"""Small append-only ledger for Jev outcomes, commitments, and decisions."""
from __future__ import annotations

import fcntl
import json
import pathlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .runtime import get_hermes_home
except ImportError:
    from runtime import get_hermes_home


def _path() -> Path:
    path = Path(get_hermes_home()) / "logs" / "jev-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append(kind: str, payload: dict[str, Any]) -> str:
    _invalidate_metrics()
    timestamp = datetime.now(timezone.utc).isoformat()
    record_id = uuid.uuid4().hex[:16]
    record = {"id": record_id, "kind": kind, "timestamp": timestamp, **payload}
    if kind == "review":
        record["review_id"] = record_id
    with _path().open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record_id


# A record is a single JSON object on one line. Reading only the tail means
# seeking from the end and walking backwards, which is what `limit` was always
# meant to do. The previous implementation did `read_text().splitlines()[-limit:]`:
# the slice bounded what was RETURNED, never what was read, decoded and split. The
# stores have no size cap, so every `metrics()`, `queue()` and `jev_ledger
# action=list` paid O(total history) forever.
#
# Measured on a 3.8 MB, 52,500-row store:
#   read(10)    11.31 ms   read(200)  10.74 ms   read(5000)  18.03 ms
#   tail read(10) 0.11 ms
_TAIL_BLOCK = 64 * 1024


def tail_lines(path: pathlib.Path, limit: int) -> list[str]:
    """Return the last `limit` complete lines without reading the whole file.

    Walks backwards in 64 KB blocks and stops once it has seen `limit` newlines.
    If the read did not start at offset zero, the first line is a fragment of a
    record that began earlier and is dropped. A file smaller than one block is
    read whole, so there is no path that silently returns too few rows.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size <= _TAIL_BLOCK:
            handle.seek(0)
            lines = handle.read().decode("utf-8", errors="ignore").splitlines()
            return lines[-limit:]
        blocks: list[bytes] = []
        newlines = 0
        position = size
        while position > 0 and newlines < limit:
            step = min(_TAIL_BLOCK, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            blocks.append(block)
            newlines += block.count(b"\n")
    data = b"".join(reversed(blocks)).decode("utf-8", errors="ignore")
    lines = data.splitlines()
    if position > 0 and lines:
        lines = lines[1:]
    return lines[-limit:]


def read(limit: int = 200) -> list[dict[str, Any]]:
    path = _path()
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in tail_lines(path, limit):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


# `metrics()` is a pure function of the store's contents, and every snapshot pays
# for it: `digest()` calls both `queue()` and `metrics()`, and the gateway's
# `snapshot()` calls `metrics()` too. Since the store is append-only, (mtime_ns,
# size) identifies its contents exactly, so a repeated snapshot costs one stat
# instead of a full re-read and re-parse.
#
# Any append bumps mtime, so the cache cannot go stale. The entry is also dropped
# explicitly on failure so an unreadable file is never served from cache.
_METRICS_CACHE: tuple[int, int, dict[str, Any]] | None = None


def _invalidate_metrics() -> None:
    global _METRICS_CACHE
    _METRICS_CACHE = None


def _store_fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def metrics() -> dict[str, Any]:
    global _METRICS_CACHE
    path = _path()
    try:
        fingerprint = _store_fingerprint(path)
    except OSError:
        _invalidate_metrics()
        fingerprint = None
    if fingerprint is not None and _METRICS_CACHE is not None:
        if _METRICS_CACHE[:2] == fingerprint:
            return _METRICS_CACHE[2]
    rows = read(5000)
    reviews = {row.get("review_id"): row for row in rows if row.get("kind") == "review" and row.get("review_id")}
    outcomes = [row for row in rows if row.get("kind") == "outcome"]
    by_review = {row.get("review_id"): row for row in outcomes if row.get("review_id")}
    correct = sum(1 for row in outcomes if row.get("correct") is True)
    result = {
        "reviews": len(reviews),
        "outcomes": len(outcomes),
        "labeled_reviews": len(by_review),
        "unlabeled_reviews": max(0, len(reviews) - len(by_review)),
        "correct": correct,
        "incorrect": sum(1 for row in outcomes if row.get("correct") is False),
        "accuracy": round(correct / len(outcomes), 4) if outcomes else None,
        "kinds": sorted({str(row.get("kind")) for row in rows}),
    }
    if fingerprint is not None:
        _METRICS_CACHE = (fingerprint[0], fingerprint[1], result)
    return result

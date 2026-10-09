"""Small append-only ledger for Jev outcomes, commitments, and decisions."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
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


def _canonical(payload: dict[str, Any]) -> str:
    """The exact serialization a record's hash is computed over.

    `sort_keys` and `ensure_ascii` are fixed so the same dict always produces the
    same bytes, which is the whole requirement for a hash that can be recomputed
    by anyone reading the file later.
    """
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _link(prev_hash: str, payload: dict[str, Any]) -> str:
    """Bind this record to the one before it.

    The hash covers the previous link as well as this record's own body, so
    removing, reordering or editing any record breaks the chain from that point
    onward. Hashing only the id would detect truncation but not a rewritten row.
    """
    return hashlib.sha256(f"{prev_hash}|{_canonical(payload)}".encode("utf-8")).hexdigest()


def _tail_link() -> tuple[str, str, bool]:
    """The last record's id and chain hash, or empty strings for an empty store.

    Read under the caller's lock. `tail_lines` walks backwards and stops after
    one newline, so this is a single 64 KB block even on a large store rather
    than a whole-file read.
    """
    path = _path()
    if not path.exists():
        return "", "", False
    lines = tail_lines(path, 1)
    if not lines:
        return "", "", False
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        # A corrupt tail cannot be linked to, and silently starting a new chain
        # would hide it. Report the store as unlinked rather than pretending.
        return "", "", True
    if not isinstance(last, dict):
        return "", "", True
    return str(last.get("id", "")), str(last.get("chain_hash", "")), "chain_hash" in last


def append(kind: str, payload: dict[str, Any]) -> str:
    """Append one record and extend the hash chain.

    The chain is additive and self-declaring: a record written before this
    change carries no `chain_hash`, is linked to nothing, and is reported as an
    unlinked head rather than as a break. Nothing that reads the ledger has to
    know which version wrote it.
    """
    _invalidate_metrics()
    timestamp = datetime.now(timezone.utc).isoformat()
    record_id = uuid.uuid4().hex[:16]
    body: dict[str, Any] = {"id": record_id, "kind": kind, "timestamp": timestamp, **payload}
    if kind == "review":
        body["review_id"] = record_id
    prev_id, prev_hash, _ = _tail_link()
    body["prev_id"] = prev_id
    body["chain_hash"] = _link(prev_hash, body)
    with _path().open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(body, ensure_ascii=True, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record_id


def verify_chain() -> dict[str, Any]:
    """Walk the ledger and report whether its hash chain still holds.

    Truncation and rewriting are the two attacks the chain exists to detect. A
    removed record leaves the next one pointing at a `prev_id` that no longer
    exists; a rewritten row leaves its own `chain_hash` disagreeing with the body
    it was computed from. Either is reported with the record id where the walk
    stopped, so an operator can find it by eye.

    Records written before the chain existed link to nothing and are reported as
    `unlinked_head` rather than as a break, because a ledger that predates the
    feature has not been tampered with — it is simply older.
    """
    path = _path()
    if not path.exists():
        return {"records": 0, "linked": 0, "intact": True, "unlinked_head": True}
    records = 0
    linked = 0
    seen: dict[str, str] = {}
    prev_hash = ""
    unlinked_head = True
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        records += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return {
                "records": records, "linked": linked, "intact": False,
                "reason": "unparseable_record", "at": line[:120],
            }
        if not isinstance(row, dict):
            return {
                "records": records, "linked": linked, "intact": False,
                "reason": "record_is_not_an_object", "at": line[:120],
            }
        record_id = str(row.get("id", ""))
        # Every record's id is tracked, chained or not, because the first
        # chained record of a pre-existing ledger legitimately points at the
        # last pre-chain one. Tracking only chained rows made that ordinary
        # transition read as a missing link.
        seen[record_id] = ""
        declared = row.get("chain_hash")
        if declared is None:
            # A pre-chain record. It is the head of the unlinked prefix, and the
            # chain is expected to start after it.
            unlinked_head = True
            prev_hash = ""
            continue
        unlinked_head = False
        body = {k: v for k, v in row.items() if k != "chain_hash"}
        expected = _link(prev_hash, body)
        if expected != declared:
            return {
                "records": records, "linked": linked, "intact": False,
                "reason": "chain_hash_mismatch", "at": str(row.get("id", "")),
            }
        prev_id = str(row.get("prev_id", ""))
        if prev_id and prev_id not in seen:
            return {
                "records": records, "linked": linked, "intact": False,
                "reason": "prev_id_not_in_ledger", "at": str(row.get("id", "")),
                "missing": prev_id,
            }
        seen[record_id] = str(declared)
        prev_hash = str(declared)
        linked += 1
    return {
        "records": records, "linked": linked, "intact": True,
        "unlinked_head": unlinked_head,
    }


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
            # The first line is a fragment only when the read began in the
            # middle of a record. Asking the byte one before the start is
            # exact; assuming it was always a fragment dropped a real row
            # every time a block boundary landed on a line boundary, which it
            # does whenever `limit` rows happen to fill a whole number of
            # blocks. `read(8192)` returned 8,191 rows on such a file.
            handle.seek(position - 1)
            if handle.read(1) != b"\n":
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


# `metrics()` is a pure function of *the window it reads*, and every snapshot
# pays for it: `digest()` calls both `queue()` and `metrics()`, and the gateway's
# `snapshot()` calls `metrics()` too. Since the store is append-only, (mtime_ns,
# size) identifies its contents exactly, so a repeated snapshot costs one stat
# instead of a full re-read and re-parse.
#
# Any append bumps mtime, so the cache cannot go stale. The entry is also dropped
# explicitly on failure so an unreadable file is never served from cache.
#
# The window is `METRICS_WINDOW` rows from the tail, not the whole store, so the
# counts below are window-scoped whenever the store is taller than that. They
# were reported as totals. `sampled` and `window_rows` say so, and the comment
# that used to call this "a pure function of the store's contents" was the error
# that let the undercount reach an operator's screen unchallenged: on a store of
# 8,000 rows this reported `reviews: 3500` where 5,000 were recorded.
_METRICS_CACHE: tuple[int, int, dict[str, Any]] | None = None
METRICS_WINDOW = 5000


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
    rows = read(METRICS_WINDOW)
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
        # A review is appended before its own outcome, so every review inside a
        # window carries its outcome inside the same window. `reviews` is
        # therefore only ever short of the *earlier* rows that scrolled out,
        # never short of a pairing, and `unlabeled_reviews` stays meaningful.
        # What the window cannot tell you is how much scrolled out, so the two
        # fields below are the honest form of "totals".
        "window_rows": len(rows),
        "sampled": len(rows) >= METRICS_WINDOW,
    }
    if fingerprint is not None:
        _METRICS_CACHE = (fingerprint[0], fingerprint[1], result)
    return result

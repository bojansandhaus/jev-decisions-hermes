"""Closed loop decision journal for Jev.

This module records decisions, evidence, observations, and outcomes without
changing execution authority. It produces deterministic calibration signals.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ledger import tail_lines

try:
    from .runtime import get_hermes_home
except ImportError:
    from runtime import get_hermes_home


def _path() -> Path:
    path = Path(get_hermes_home()) / "logs" / "jev-closed-loop.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(kind: str, payload: dict[str, Any], record_id: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    record_id = record_id or uuid.uuid4().hex[:16]
    record = {"id": record_id, "kind": kind, "timestamp": timestamp, **payload}
    if kind == "decision":
        record["decision_id"] = record_id
    with _path().open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record_id


def _read(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the closed-loop store, newest rows last.

    `limit` bounds how much is read, not just what is returned. The previous
    implementation always read the whole file with no limit at all, so every
    consumer paid O(total history). `None` keeps the old whole-file behaviour for
    callers that genuinely need it.
    """
    path = _path()
    if not path.exists():
        return []
    lines = (
        path.read_text(encoding="utf-8").splitlines()
        if limit is None
        else tail_lines(path, limit)
    )
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def record_decision(
    question: str,
    chosen: str,
    options: list[str] | None = None,
    evidence: list[Any] | None = None,
    assumptions: list[str] | None = None,
    owner: str = "user",
    deadline: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "question": question[:4000],
        "chosen": chosen[:1000],
        "options": (options or [])[:20],
        "evidence": (evidence or [])[:20],
        "assumptions": (assumptions or [])[:20],
        "owner": owner,
        "deadline": deadline,
    }
    decision_id = _append("decision", payload, decision_id)
    return {"decision_id": decision_id, "kind": "decision"}


def record_observation(decision_id: str, observation: str, source: str, supports: bool | None = None) -> dict[str, Any]:
    observation_id = _append("observation", {
        "decision_id": decision_id,
        "observation": observation[:4000],
        "source": source[:500],
        "supports": supports,
    })
    return {"observation_id": observation_id, "decision_id": decision_id, "kind": "observation"}


def record_outcome(decision_id: str, status: str, success: bool | None, evidence: Any = None, notes: str = "") -> dict[str, Any]:
    outcome_id = _append("outcome", {
        "decision_id": decision_id,
        "status": status[:500],
        "success": success if isinstance(success, bool) else None,
        "evidence": evidence,
        "notes": notes[:4000],
    })
    return {"outcome_id": outcome_id, "decision_id": decision_id, "kind": "outcome"}


def label_outcome(decision_id: str, success: bool, evidence: Any, labeler: str = "user", notes: str = "") -> dict[str, Any]:
    label_id = _append("outcome_label", {
        "decision_id": decision_id,
        "success": bool(success),
        "evidence": evidence,
        "labeler": labeler,
        "notes": notes[:4000],
    })
    return {"label_id": label_id, "decision_id": decision_id, "kind": "outcome_label"}


def reopen(decision_id: str, reason: str, evidence: Any = None) -> dict[str, Any]:
    update_id = _append("reopen", {
        "decision_id": decision_id,
        "reason": reason[:4000],
        "evidence": evidence,
        "status": "reopened",
    })
    return {"update_id": update_id, "decision_id": decision_id, "status": "reopened"}


def assess(decision_id: str) -> dict[str, Any]:
    rows = [row for row in _read() if row.get("decision_id") == decision_id]
    decision = next((row for row in rows if row.get("kind") == "decision"), None)
    labels = [row for row in rows if row.get("kind") == "outcome_label" and isinstance(row.get("success"), bool)]
    outcomes = labels or [row for row in rows if row.get("kind") == "outcome" and isinstance(row.get("success"), bool)]
    successes = sum(1 for row in outcomes if row.get("success") is True)
    accuracy = round(successes / len(outcomes), 4) if outcomes else None
    reopened = any(row.get("kind") == "reopen" for row in rows)
    if reopened:
        recommendation = "review_new_evidence"
        reason = "later evidence reopened this decision"
        status = "reopened"
    elif len(outcomes) >= 5 and accuracy is not None and (accuracy <= 0.6 or accuracy >= 0.9):
        recommendation = "change_behavior"
        reason = "repeated labeled outcomes provide enough evidence to review the current behavior"
        status = "closed"
    elif outcomes:
        recommendation = "continue_observing"
        reason = "outcome evidence exists but is not yet decisive"
        status = "closed"
    else:
        recommendation = "collect_outcome"
        reason = "the decision has no recorded outcome"
        status = "open"
    return {
        "decision_id": decision_id,
        "found": decision is not None,
        "status": status,
        "question": decision.get("question") if decision else None,
        "chosen": decision.get("chosen") if decision else None,
        "observations": sum(1 for row in rows if row.get("kind") == "observation"),
        "outcomes": len(outcomes),
        "successes": successes,
        "accuracy": accuracy,
        "recommendation": recommendation,
        "reason": reason,
    }


def list_records(limit: int = 100) -> list[dict[str, Any]]:
    wanted = max(1, min(limit, 500))
    # Read only as far back as `wanted`, instead of the whole file and then
    # discarding almost all of it.
    return _read(limit=wanted)[-wanted:]
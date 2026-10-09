"""Regressions for journal integrity and for a failure nobody could see.

Two properties, both about making an invisible event visible:

- a ledger whose chain no longer holds says so, and says where it stopped;
- a lesson guard that cannot reach a decision still abstains, but its failure
  reaches an operator's ledger rather than only the agent it is gating.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def ledger_home(monkeypatch, tmp_path):
    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(ledger, "_invalidate_metrics", lambda: None)
    return ledger, tmp_path


def _store(ledger, home: Path, rows: list[dict]) -> Path:
    path = home / "logs" / "jev-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    return path


# ---------------------------------------------------------------------------
# 1. The ledger is forgeable by the party it judges — truncation and rewriting
# ---------------------------------------------------------------------------

def test_a_fresh_chain_verifies(ledger_home):
    ledger, _ = ledger_home
    for i in range(6):
        ledger.append("review", {"n": i})
    report = ledger.verify_chain()
    assert report == {"records": 6, "linked": 6, "intact": True, "unlinked_head": False}


def test_a_record_binds_to_the_one_before_it(ledger_home):
    """The link covers the previous hash as well as this record's own body.

    Hashing only the id would detect truncation but not a rewritten row: an
    edited record keeps its id, so an id-only chain would still verify.
    """
    ledger, _ = ledger_home
    ledger.append("review", {"n": 1})
    ledger.append("review", {"n": 2})
    rows = [json.loads(l) for l in ledger._path().read_text().splitlines()]
    assert rows[1]["prev_id"] == rows[0]["id"]
    assert rows[1]["chain_hash"] != rows[0]["chain_hash"]


def test_truncating_a_record_is_detected(ledger_home):
    """An append-only store is only append-only if removing a row is visible.

    Before the chain, any process with write access could drop the record that
    contradicted it and the ledger would simply be shorter.
    """
    ledger, home = ledger_home
    for i in range(5):
        ledger.append("review", {"n": i})
    lines = ledger._path().read_text().splitlines()
    del lines[2]
    ledger._path().write_text("\n".join(lines) + "\n")

    report = ledger.verify_chain()
    assert report["intact"] is False, report
    assert report["reason"] in {"chain_hash_mismatch", "prev_id_not_in_ledger"}, report


def test_rewriting_a_record_is_detected(ledger_home):
    """The other half. A rewritten body leaves its own `chain_hash` disagreeing
    with the body it was computed from."""
    ledger, home = ledger_home
    for i in range(4):
        ledger.append("review", {"n": i, "correct": False})
    lines = ledger._path().read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["correct"] = True  # the record now claims the opposite
    ledger._path().write_text(
        json.dumps(tampered, sort_keys=True) + "\n" + "\n".join(lines[2:]) + "\n"
    )

    report = ledger.verify_chain()
    assert report["intact"] is False, report
    assert report["reason"] == "chain_hash_mismatch", report
    assert report["at"] == tampered["id"], report


def test_a_break_reports_where_the_walk_stopped(ledger_home):
    """A report that says only 'broken' is not actionable."""
    ledger, _ = ledger_home
    for i in range(6):
        ledger.append("review", {"n": i})
    lines = ledger._path().read_text().splitlines()
    del lines[3]
    ledger._path().write_text("\n".join(lines) + "\n")
    report = ledger.verify_chain()
    assert report["intact"] is False
    assert report["at"], report


def test_a_pre_chain_ledger_is_not_accused_of_tampering(ledger_home):
    """The chain is additive and self-declaring. A ledger written before the
    feature links to nothing and is reported as an unlinked head — older, not
    tampered with."""
    ledger, home = ledger_home
    _store(ledger, home, [{"id": f"old{i}", "kind": "review"} for i in range(3)])
    report = ledger.verify_chain()
    assert report["intact"] is True
    assert report["unlinked_head"] is True
    assert report["linked"] == 0


def test_appending_after_a_pre_chain_ledger_is_not_a_break(ledger_home):
    """The ordinary transition. The first chained record of an existing ledger
    legitimately points at the last pre-chain one, and tracking only chained ids
    made that read as a missing link — a defect the test caught during the fix.
    """
    ledger, home = ledger_home
    _store(ledger, home, [{"id": f"old{i}", "kind": "review"} for i in range(3)])
    for i in range(3):
        ledger.append("review", {"n": i})
    report = ledger.verify_chain()
    assert report["intact"] is True, report
    assert report["linked"] == 3, report


def test_an_empty_ledger_verifies(ledger_home):
    ledger, _ = ledger_home
    assert ledger.verify_chain() == {
        "records": 0, "linked": 0, "intact": True, "unlinked_head": True,
    }


def test_an_unparseable_record_is_reported_not_swallowed(ledger_home):
    ledger, home = ledger_home
    path = _store(ledger, home, [{"id": "a", "kind": "review"}])
    path.write_text(path.read_text() + "this is not json\n")
    report = ledger.verify_chain()
    assert report["intact"] is False
    assert report["reason"] == "unparseable_record", report


def test_the_chain_action_is_reachable_and_reports(monkeypatch, tmp_path):
    """The operator path. `jev_ledger action=verify_chain` is an operator check,
    not an agent-facing one, and it is declared in the tool schema."""
    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))

    spec = importlib.util.spec_from_file_location("_reg_chain", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.get_hermes_home = lambda: str(tmp_path)

    module.append_ledger("review", {"n": 1})
    module.append_ledger("review", {"n": 2})
    result = json.loads(module.jev_ledger_handler({"action": "verify_chain"}))
    assert result["success"] is True, result
    assert result["chain"]["intact"] is True, result
    assert result["chain"]["linked"] == 2, result
    assert "verify_chain" in module.JEV_LEDGER_SCHEMA["parameters"]["properties"]["action"]["enum"]


# ---------------------------------------------------------------------------
# 2. A lesson-guard failure was invisible to everyone but the agent
# ---------------------------------------------------------------------------

def _load_module():
    """Load the plugin module the way its own fixtures do."""
    spec = importlib.util.spec_from_file_location("_reg_gate", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_gate_failure_reaches_the_ledger(monkeypatch):
    """The counters were process-local and the only way to read them was
    `jev_lessons action=stats` — a tool the gated agent calls, and the same agent
    that can ignore what it says. A store unreadable for days was invisible to
    everyone but the party whose behaviour it constrains.
    """
    module = _load_module()
    appended: list[tuple] = []
    monkeypatch.setattr(module, "append_ledger", lambda kind, payload: appended.append((kind, payload)))
    monkeypatch.setattr(module, "_lesson_gate_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unreadable")))

    assert module._lesson_gate("terminal", {}, False) is None

    assert [kind for kind, _ in appended] == ["lesson_gate_error"], appended
    _, payload = appended[0]
    assert payload["error_type"] == "RuntimeError"
    assert payload["tool_name"] == "terminal"


def test_only_the_first_failure_is_appended(monkeypatch):
    """Appending per failure would let a broken store grow the ledger without
    bound, which is its own denial of service."""
    module = _load_module()
    appended: list[tuple] = []
    monkeypatch.setattr(module, "append_ledger", lambda kind, payload: appended.append((kind, payload)))
    monkeypatch.setattr(module, "_lesson_gate_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store still unreadable")))

    for _ in range(5):
        module._lesson_gate("terminal", {}, False)

    assert [kind for kind, _ in appended] == ["lesson_gate_error"], appended
    assert module._LESSON_GATE_ERRORS["count"] == 5


def test_reporting_a_failure_never_becomes_the_failure(monkeypatch):
    """If the ledger itself is what is broken, the gate must still abstain and
    still return rather than raise into a policy hook, where an exception is
    resolved as a BLOCK on the user's tool call."""
    module = _load_module()
    monkeypatch.setattr(module, "_lesson_gate_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gate")))
    monkeypatch.setattr(module, "append_ledger", lambda *a, **k: (_ for _ in ()).throw(OSError("ledger gone")))

    assert module._lesson_gate("terminal", {}, False) is None
    assert module._LESSON_GATE_ERRORS["count"] == 1


def test_the_chain_action_is_reachable_and_reports(monkeypatch):
    """The operator path. `jev_ledger action=verify_chain` is an operator check,
    not an agent-facing one, and it is declared in the tool schema."""
    module = _load_module()
    monkeypatch.setattr(
        module, "verify_ledger_chain",
        lambda: {"records": 4, "linked": 4, "intact": True, "unlinked_head": False},
    )
    result = json.loads(module.jev_ledger_handler({"action": "verify_chain"}))
    assert result["success"] is True, result
    assert result["chain"] == {
        "records": 4, "linked": 4, "intact": True, "unlinked_head": False,
    }, result
    assert "verify_chain" in module.JEV_LEDGER_SCHEMA["parameters"]["properties"]["action"]["enum"]


def test_verify_chain_reports_a_break_through_the_tool(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module, "verify_ledger_chain",
        lambda: {"records": 3, "linked": 1, "intact": False, "reason": "chain_hash_mismatch", "at": "abc123"},
    )
    result = json.loads(module.jev_ledger_handler({"action": "verify_chain"}))
    assert result["chain"]["intact"] is False
    assert result["chain"]["reason"] == "chain_hash_mismatch"
    assert result["chain"]["at"] == "abc123"

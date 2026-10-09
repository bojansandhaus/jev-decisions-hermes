"""Regressions for the six issues 0.10.0 shipped with and named as known.

Each asserts one property rather than one bug. The common shape across all six
is that a bound, a sample, or a fallback silently chose the permissive reading,
and the caller had no way to know. Nothing here makes a provider call.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 1. The decision journal is forgeable by the party it judges
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_home(monkeypatch, tmp_path):
    """Redirect every local store into tmp_path.

    `ledger` and `closed_loop` each bind `get_hermes_home` from `runtime` at
    import time, so patching `runtime` after import would leave both writing to
    the real home. Each module's own reference is patched instead."""
    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    import closed_loop

    monkeypatch.setattr(closed_loop, "get_hermes_home", lambda: str(tmp_path))
    return tmp_path


def test_an_outcome_row_records_who_claimed_it(ledger_home):
    """An outcome row used to carry no labeler at all, so a self-certified
    `correct: true` was indistinguishable from an operator's confirmation and
    `metrics()` counted both the same way."""
    import gateway

    gateway.record_outcome("r1", True)
    rows = ledger_home.joinpath("logs", "jev-ledger.jsonl").read_text().splitlines()
    outcomes = [json.loads(row) for row in rows if json.loads(row).get("kind") == "outcome"]
    assert outcomes, "no outcome row was written"
    assert outcomes[0].get("labeler") == "user", outcomes[0]


def test_a_tool_supplied_labeler_cannot_claim_to_be_the_operator(ledger_home):
    """`label_outcome` read the labeler out of the tool arguments, so the
    supervised party signalled its own label as the operator's."""
    import closed_loop

    # The default is the operator's signature, and it is reachable only by
    # calling this module directly, which operator code does.
    closed_loop.label_outcome("d1", True, {"k": "v"})
    row = _last_row(ledger_home, "outcome_label")
    assert row["labeler"] == "user", row


def test_the_tool_boundary_signs_every_label_as_the_agent(ledger_home):
    """The real boundary. `jev_closed_loop action=label_outcome` is a registered
    tool the model calls, so its label is signed accordingly however it asks."""
    import importlib.util

    import closed_loop

    spec = importlib.util.spec_from_file_location("_reg_journal", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = json.loads(module.jev_loop_handler({
        "action": "label_outcome",
        "decision_id": "d1",
        "success": True,
        "labeler": "user",          # the forgery the caller would attempt
        "evidence": {},
    }))
    assert result.get("success") is True, result
    row = _last_row(ledger_home, "outcome_label")
    assert row["labeler"] == "agent", row


def test_agent_labels_are_not_counted_as_operator_judgement(ledger_home):
    """A shadow report that graded itself on its own labels could report perfect
    accuracy while being wrong. Agent labels stay in the report, but they are
    reported beside the operator figure rather than folded into it."""
    import shadow_report

    rows = [
        {"kind": "outcome_label", "decision_id": "d1", "success": True, "labeler": "agent"},
        {"kind": "outcome_label", "decision_id": "d2", "success": True, "labeler": "agent"},
        {"kind": "outcome_label", "decision_id": "d3", "success": False, "labeler": "agent"},
        {"kind": "outcome_label", "decision_id": "d4", "success": True, "labeler": "user"},
        {"kind": "outcome_label", "decision_id": "d5", "success": False, "labeler": "user"},
    ]
    summary = shadow_report.closed_loop_calibration(rows)
    assert summary["labeled"] == 2, summary
    assert summary["correct"] == 1, summary
    assert summary["accuracy"] == 0.5, summary


def _last_row(home: Path, kind: str) -> dict:
    """Read the closed_loop store, which is `jev-closed-loop.jsonl`.

    Deliberately not the main ledger: `closed_loop` keeps its own append-only
    file, so a regression that starts writing to the wrong one would otherwise
    pass silently here."""
    rows = home.joinpath("logs", "jev-closed-loop.jsonl").read_text().splitlines()
    for line in reversed(rows):
        row = json.loads(line)
        if row.get("kind") == kind:
            return row
    raise AssertionError(f"no {kind} row found")


# ---------------------------------------------------------------------------
# 2. Turn eviction dropped a live blocking control
# ---------------------------------------------------------------------------


@pytest.fixture
def supervision(monkeypatch, tmp_path):
    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    import supervision as module

    monkeypatch.setattr(module, "append_ledger", ledger.append)
    return module


@pytest.fixture
def sup(supervision):
    return supervision.Supervision(
        supervision.SupervisionConfig(mode="precommit", enabled=True, max_tracked_turns=3)
    )


def _blocked(supervision_instance, turn_id, session_id, command):
    args = {"command": command}
    supervision_instance.begin_turn(turn_id=turn_id, session_id=session_id, user_message="restart the production service")
    for _ in range(3):
        supervision_instance.record_tool_outcome(
            tool_name="terminal", args=args, result={"error": "connection refused"},
            turn_id=turn_id, session_id=session_id,
        )
    return args


def test_unrelated_traffic_cannot_evict_a_live_control(sup):
    """Eviction took `next(iter(self._turns))` whatever it held, and the turn it
    dropped was often the one whose control was blocking an action. The gate then
    reported `allow: True`, because a turn that cannot be resolved looks like a
    turn with nothing to enforce."""
    args = _blocked(sup, "T1", "s1", "systemctl restart api")
    assert sup.check_control(tool_name="terminal", args=args, turn_id="T1", session_id="s1")["allow"] is False

    for turn in ("T2", "T3", "T4", "T5", "T6"):
        sup.begin_turn(turn_id=turn, session_id="other", user_message="unrelated traffic")

    gate = sup.check_control(tool_name="terminal", args=args, turn_id="T1", session_id="s1")
    assert gate["allow"] is False, gate
    assert gate.get("control") == "REPLAN", gate


def test_turns_without_a_control_are_still_evicted(sup):
    """The bound still has to bound something, or the fix is a leak."""
    for turn in ("A", "B", "C", "D", "E", "F"):
        sup.begin_turn(turn_id=turn, session_id=f"s-{turn}", user_message="no controls here")
    assert len(sup._turns) <= sup._config.max_tracked_turns
    assert sup._metrics["turns_evicted"] > 0


def test_control_retention_has_a_hard_ceiling_and_reports_the_loss(sup, monkeypatch):
    """Past the ceiling the memory bound wins, but it is no longer silent: the
    drop is counted under its own metric and appended to the ledger. Without a
    ceiling the refusal to evict would be a leak in a process where turns never
    end, because a ControlDirective carries no expiry."""
    monkeypatch.setattr(sup._config, "control_retention_limit", 4)
    for i in range(10):
        _blocked(sup, f"T{i}", f"s{i}", f"restart svc{i}")
    assert len(sup._turns) <= 4
    assert sup._metrics.get("controls_evicted", 0) > 0


def test_an_unresolvable_turn_reports_why(sup):
    """`no_turn` used to return a bare `allow: True`. It now names the reason, so
    a caller can tell 'nothing to enforce' from 'the turn is gone'."""
    assert sup.check_control(tool_name="terminal", args={"command": "x"}, turn_id="missing")["reason"] == "no_turn"


# ---------------------------------------------------------------------------
# 3. metrics() reported a 5,000-row sample as totals
# ---------------------------------------------------------------------------

def _write_ledger(home: Path, rows: list[dict]) -> Path:
    path = home / "lev.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_a_tall_store_declares_itself_a_sample(monkeypatch, ledger_home):
    """The comment called `metrics()` "a pure function of the store's contents".
    It is a function of the last 5,000 rows, and on a taller store it reported
    `reviews: 3500` where 5,000 were recorded — a number an operator reads as a
    total."""
    import ledger

    rows: list[dict] = []
    for i in range(3000):
        rows.append({"kind": "review", "review_id": f"r{i}"})
        rows.append({"kind": "outcome", "review_id": f"r{i}", "correct": True})
    for i in range(3000, 5000):
        rows.append({"kind": "review", "review_id": f"r{i}"})
    path = _write_ledger(ledger_home, rows)
    monkeypatch.setattr(ledger, "_path", lambda: path)
    ledger._invalidate_metrics()

    metrics = ledger.metrics()
    assert metrics["sampled"] is True, metrics
    assert metrics["window_rows"] == ledger.METRICS_WINDOW, metrics
    # The window is honest about its own extent, which is what makes the
    # undercount legible instead of silent.
    assert metrics["reviews"] < 5000, metrics


def test_a_short_store_is_not_reported_as_a_sample(monkeypatch, ledger_home):
    import ledger

    path = _write_ledger(ledger_home, [{"kind": "review", "review_id": f"r{i}"} for i in range(10)])
    monkeypatch.setattr(ledger, "_path", lambda: path)
    ledger._invalidate_metrics()
    metrics = ledger.metrics()
    assert metrics["sampled"] is False, metrics
    assert metrics["window_rows"] == 10, metrics
    assert metrics["reviews"] == 10, metrics


# ---------------------------------------------------------------------------
# 4. tail_lines dropped a row at exact block boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [8190, 8191, 8192, 8193, 8194, 8195])
def test_a_block_boundary_does_not_lose_a_row(limit, ledger_home):
    """`read(8192)` returned 8,191 rows whenever `limit` rows happened to fill a
    whole number of 64 KB blocks, because the leading line was assumed to be a
    fragment without asking the byte before it."""
    import ledger

    path = ledger_home / "edge.jsonl"
    path.write_bytes(b"".join(b"%07d\n" % i for i in range(9000)))
    rows = ledger.tail_lines(path, limit)
    assert len(rows) == limit, f"expected {limit} rows, got {len(rows)}"


def test_a_genuine_fragment_is_still_dropped(ledger_home):
    """The fix asks the byte before the read rather than assuming. A read that
    really does start mid-record must still lose its partial first line."""
    import ledger

    path = ledger_home / "frag.jsonl"
    path.write_bytes(b"".join(b"%07d\n" % i for i in range(9000)) + b"PARTIAL")
    rows = ledger.tail_lines(path, 8192)
    assert len(rows) == 8192, len(rows)
    assert rows[0] == "%07d" % (9001 - 8192), rows[0]
    assert rows[-1] == "PARTIAL", rows[-1]


# ---------------------------------------------------------------------------
# 5. ingest leaked a slot per unverified result and closed the wrong case
# ---------------------------------------------------------------------------

@pytest.fixture
def ingest_module(monkeypatch):
    """The case store is faked, so these tests read only the tracking maps.

    What is under test is bookkeeping — which slot is freed, and whether an
    unknown invocation id is allowed to close someone else's case — so a real
    store would only add a filesystem to reason about."""
    import ingest

    calls: dict[str, list] = {"opened": [], "closed": []}
    counter = {"n": 0}

    def fake_open(domain, payload):
        counter["n"] += 1
        case_id = f"case{counter['n']}"
        calls["opened"].append((case_id, domain))
        return case_id

    def fake_close(case_id, status, payload):
        calls["closed"].append((case_id, status))
        return case_id

    monkeypatch.setattr(ingest, "open_case", fake_open)
    monkeypatch.setattr(ingest, "close_case", fake_close)
    ingest._ACTIVE_CASES.clear()
    ingest._ACTIVE_INVOCATIONS.clear()
    return ingest, calls


def test_every_outcome_frees_its_slot(ingest_module):
    """Cleanup ran only on `verified`, and a result verifies False whenever it
    carries no expected field — the ordinary case for a tool call. So every
    hooked result leaked one dict entry and one case row for the life of the
    process."""
    module, _ = ingest_module
    for i in range(5):
        module.ingest_event("hermes", "tool_call", {"tool_name": "terminal", "invocation_id": f"i{i}"})
        module.update_tool_result("terminal", {"output": "x"}, verified=False, invocation_id=f"i{i}")
    assert not module._ACTIVE_CASES
    assert not module._ACTIVE_INVOCATIONS


def test_an_unknown_invocation_id_closes_nothing(ingest_module):
    """An invocation id that is present but unknown used to fall through to
    `pending[0]`, resolving the oldest open case for that tool name — a case
    belonging to a different call — while the call this result was about stayed
    open forever."""
    module, calls = ingest_module
    real = module.ingest_event("hermes", "tool_call", {"tool_name": "terminal", "invocation_id": "real"})
    assert module.update_tool_result("terminal", {"output": "y"}, invocation_id="unknown") is None
    assert module._ACTIVE_CASES["terminal"] == [real["case_id"]]
    # Exactly the one legitimate case is closed, and it is closed once.
    assert [c for c in calls["closed"]] == [], calls["closed"]
    assert module.update_tool_result("terminal", {"output": "z"}, invocation_id="real") == real["case_id"]
    assert not module._ACTIVE_CASES
    assert [c[0] for c in calls["closed"]] == [real["case_id"]], calls["closed"]


def test_the_tracking_maps_are_bounded(ingest_module):
    """A last line behind the fix above, not a substitute for it: in a process
    that never restarts, neither map may grow without end."""
    module, _ = ingest_module
    for i in range(module._MAX_TRACKED_CASES + 50):
        module.ingest_event("hermes", "tool_call", {"tool_name": "terminal", "invocation_id": f"b{i}"})
    assert len(module._ACTIVE_CASES["terminal"]) <= module._MAX_TRACKED_CASES
    assert len(module._ACTIVE_INVOCATIONS) <= module._MAX_TRACKED_INVOCATIONS


# ---------------------------------------------------------------------------
# 6. _boolean fail-opened on any non-boolean value
# ---------------------------------------------------------------------------

@pytest.fixture
def gateway():
    import gateway as module

    return module


@pytest.mark.parametrize(
    "state",
    [
        {"external": 1},
        {"credential": "yes"},
        {"reversible": 0},
        {"reversible": "no"},
        {"external": "false", "reversible": "false"},
    ],
)
def test_a_flag_that_is_present_but_unreadable_is_refused(gateway, state):
    """Every one of these flags defaults toward "no human needed", so
    `{"external": 1}` — an action that reaches the network — was read as
    `external=False` and classified `observe`, and `{"reversible": "no"}` was
    read as reversible."""
    decision = gateway.decide(state)
    assert decision["decision"] == "invalid_state", state
    assert decision["authority"] == "invalid_state", state
    assert decision["reason"]["malformed_flags"], state


def test_absence_still_uses_the_default(gateway):
    """The refusal must not become an anything-goes gate."""
    assert gateway.decide({})["decision"] == "observe"
    assert gateway.decide({"external": True})["decision"] == "suggest"

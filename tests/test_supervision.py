"""Tests for the 0.3.0 supervision layer.

These tests exercise the adapted nervous system behavior in isolation: local
admission, adaptive routing, challenge freshness, and the repeated failure
control lease. Nothing here makes a provider call, and every test asserts that
deterministic authority is preserved.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def supervision(monkeypatch, tmp_path):
    """Import supervision with all local state redirected into tmp_path."""
    import ledger
    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    import supervision as module
    monkeypatch.setattr(module, "append_ledger", ledger.append)
    return module


@pytest.fixture
def sup(supervision):
    instance = supervision.Supervision(supervision.SupervisionConfig())
    instance.reset()
    return instance


def _material_event(**overrides):
    event = {
        "type": "DECISION",
        "goal": "repair failing service",
        "materiality": 0.9,
        "uncertainty": 0.6,
        "contradiction": 0.3,
        "risk": 0.8,
    }
    event.update(overrides)
    return event


# -- admission -----------------------------------------------------------


def test_admission_is_off_for_ordinary_conversation(supervision):
    admission, score, reasons = supervision.classify_admission("what is the capital of France?")
    assert admission == "OFF"
    assert score <= 0.2
    assert reasons


def test_admission_is_on_for_risky_turns(supervision):
    admission, score, reasons = supervision.classify_admission("delete the production backup and restart the service")
    assert admission == "ON"
    assert score >= 0.6
    assert any(reason.startswith("risk:") for reason in reasons)


def test_admission_is_watch_for_plan_like_turns(supervision):
    admission, _, reasons = supervision.classify_admission("please review the plan for this refactor")
    assert admission == "WATCH"
    assert any(reason.startswith("plan:") for reason in reasons)


def test_begin_turn_never_blocks_and_is_idempotent(sup):
    first = sup.begin_turn(turn_id="t1", session_id="s1", user_message="delete the production database")
    second = sup.begin_turn(turn_id="t1", session_id="s1", user_message="delete the production database")
    assert first["admission"] == "ON"
    assert first["turn_id"] == second["turn_id"] == "t1"
    assert first["mode"] == "shadow"


def test_disabled_supervision_admits_off(sup):
    sup.configure(enabled=False)
    result = sup.begin_turn(turn_id="t1", user_message="delete everything")
    assert result["admission"] == "OFF"


# -- adaptive router -----------------------------------------------------


def test_router_suppresses_routine_events(sup):
    sup.begin_turn(turn_id="t1", user_message="review this plan")
    decision = sup.observe_event({"type": "OBSERVATION", "materiality": 0.1}, turn_id="t1")
    assert decision["forward"] is False
    assert decision["reason"] == "below_threshold"


def test_router_forwards_material_events(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    decision = sup.observe_event(_material_event(), turn_id="t1")
    assert decision["forward"] is True
    assert decision["score"] >= sup.config.relevance_threshold


def test_router_applies_hysteresis_to_equivalent_state(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    first = sup.observe_event(_material_event(), turn_id="t1")
    second = sup.observe_event(_material_event(), turn_id="t1")
    assert first["forward"] is True
    assert second["forward"] is False
    assert second["reason"] == "hysteresis"


def test_router_batches_while_an_assessment_is_inflight(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.mark_assessment(turn_id="t1", inflight=True)
    decision = sup.observe_event(_material_event(), turn_id="t1")
    assert decision["routed"] == "batched"
    assert decision["forward"] is False
    batched = sup.drain_batch(turn_id="t1")
    assert len(batched) == 1
    assert batched[0]["type"] == "DECISION"
    assert sup.drain_batch(turn_id="t1") == []


def test_router_respects_the_provider_budget(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.configure(max_provider_calls_per_turn=1)
    sup.record_provider_call(turn_id="t1")
    decision = sup.observe_event(_material_event(), turn_id="t1")
    assert decision["forward"] is False
    assert decision["reason"] == "budget_exhausted"


def test_router_suppresses_everything_when_admission_is_off(sup):
    sup.begin_turn(turn_id="t1", user_message="hello there")
    decision = sup.observe_event(_material_event(), turn_id="t1")
    assert decision["forward"] is False
    assert decision["reason"] == "admission_off"


def test_bounded_event_drops_unknown_and_secret_like_fields(supervision):
    bounded = supervision._bounded_event({
        "type": "DECISION",
        "materiality": 0.5,
        "api_key": "synthetic-secret-value",
        "payload": {"anything": "here"},
        "goal": "g" * 500,
    }, 64)
    assert "api_key" not in bounded
    assert "payload" not in bounded
    assert len(bounded["goal"]) == 64


# -- challenges ----------------------------------------------------------


def test_agreement_never_becomes_a_challenge(sup):
    sup.begin_turn(turn_id="t1", user_message="review this plan")
    result = sup.consider_challenge(
        hermes_decision="PATCH", jev_decision="PATCH", confidence=0.99, turn_id="t1"
    )
    assert result["challenged"] is False
    assert result["reason"] == "agreement"


def test_low_confidence_disagreement_stays_silent(sup):
    sup.begin_turn(turn_id="t1", user_message="review this plan")
    result = sup.consider_challenge(
        hermes_decision="PATCH", jev_decision="ROLLBACK", confidence=0.40, turn_id="t1"
    )
    assert result["challenged"] is False
    assert result["reason"] == "below_confidence"


def test_high_confidence_disagreement_is_delivered_once(sup):
    sup.begin_turn(turn_id="t1", user_message="review this plan")
    created = sup.consider_challenge(
        hermes_decision="PATCH", jev_decision="ROLLBACK", confidence=0.95, turn_id="t1"
    )
    assert created["challenged"] is True
    delivered = sup.take_challenge(turn_id="t1")
    assert delivered is not None
    assert delivered["jev_decision"] == "ROLLBACK"
    assert delivered["delivered"] is True
    assert sup.take_challenge(turn_id="t1") is None


def test_stale_challenge_is_retained_as_telemetry_not_delivered(sup, tmp_path):
    sup.begin_turn(turn_id="t1", user_message="review this plan")
    sup.set_state_version(turn_id="t1", state_version="git:aaa")
    sup.consider_challenge(
        hermes_decision="PATCH",
        jev_decision="ROLLBACK",
        confidence=0.95,
        state_version="git:aaa",
        turn_id="t1",
    )
    sup.set_state_version(turn_id="t1", state_version="git:bbb")
    assert sup.take_challenge(turn_id="t1") is None
    ledger_rows = (tmp_path / "logs" / "jev-ledger.jsonl").read_text()
    assert "challenge_stale" in ledger_rows


# -- repeated failure control lease --------------------------------------


def test_first_failure_may_be_assessed_remotely(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    first = sup.record_tool_outcome(
        tool_name="terminal", args={"command": "make build"}, result={"error": "boom"}, turn_id="t1"
    )
    assert first["outcome"] == "failure"
    assert first["count"] == 1
    assert first["assess_remotely"] is True


def test_equivalent_failures_are_deduplicated(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    payload = {"error": "boom"}
    sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    second = sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    assert second["count"] == 2
    assert second["assess_remotely"] is False
    assert second["reason"] == "equivalent_failure_deduplicated"


def test_threshold_creates_a_local_replan_control(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    payload = {"error": "boom"}
    for _ in range(2):
        sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    third = sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    assert third["control"]["control"] == "REPLAN"
    assert third["control"]["source"] == "local_loop_breaker"
    assert third["assess_remotely"] is False


def test_shadow_mode_reports_but_never_blocks(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    payload = {"error": "boom"}
    for _ in range(3):
        sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    decision = sup.check_control(tool_name="terminal", args={"command": "make build"}, turn_id="t1")
    assert decision["controlled"] is True
    assert decision["allow"] is True
    assert decision["advisory"] is True
    assert sup.mode() == "shadow"


def test_correct_next_blocks_the_exact_repeated_action(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.configure(mode="correct_next")
    payload = {"error": "boom"}
    for _ in range(3):
        sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    blocked = sup.check_control(tool_name="terminal", args={"command": "make build"}, turn_id="t1")
    assert blocked["controlled"] is True
    assert blocked["allow"] is False
    assert blocked["control"] == "REPLAN"


def test_correct_next_allows_a_materially_different_action(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.configure(mode="correct_next")
    payload = {"error": "boom"}
    for _ in range(3):
        sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    other = sup.check_control(tool_name="terminal", args={"command": "make test"}, turn_id="t1")
    assert other["controlled"] is False
    assert other["allow"] is True
    assert other["reason"] == "different_action"


def test_explicit_retry_consumes_the_control_once(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.configure(mode="correct_next")
    payload = {"error": "boom"}
    for _ in range(3):
        sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    assert sup.allow_retry(turn_id="t1")["retry_allowed"] is True
    allowed = sup.check_control(tool_name="terminal", args={"command": "make build"}, turn_id="t1")
    assert allowed["allow"] is True
    assert allowed["control"] == "RETRY"
    cleared = sup.check_control(tool_name="terminal", args={"command": "make build"}, turn_id="t1")
    assert cleared["controlled"] is False


def test_success_clears_the_failure_episode(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    payload = {"error": "boom"}
    sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result={"success": True}, turn_id="t1")
    again = sup.record_tool_outcome(tool_name="terminal", args={"command": "make build"}, result=payload, turn_id="t1")
    assert again["count"] == 1


def test_successful_payload_never_fabricates_a_failure(supervision):
    assert supervision.normalize_error({"success": True, "output": "fine"}) == ""
    assert supervision.normalize_error({"error": "boom"}) == "boom"
    assert supervision.normalize_error("all good") == ""
    assert supervision.normalize_error("Error: nope").startswith("error")


def test_distinct_arguments_are_distinct_fingerprints(supervision):
    left = supervision.fingerprint_action("terminal", {"command": "ls"})
    right = supervision.fingerprint_action("terminal", {"command": "rm -rf /"})
    reordered = supervision.fingerprint_action("terminal", {"b": 1, "a": 2})
    same = supervision.fingerprint_action("terminal", {"a": 2, "b": 1})
    assert left != right
    assert reordered == same


# -- read only bypass ----------------------------------------------------


def test_read_only_calls_are_recognized(supervision):
    assert supervision.is_read_only_call("read_file", {"path": "x"}) is True
    assert supervision.is_read_only_call("terminal", {"command": "git status"}) is True
    assert supervision.is_read_only_call("terminal", {"command": "pwd"}) is True


def test_mutating_or_composed_calls_are_not_bypassed(supervision):
    assert supervision.is_read_only_call("terminal", {"command": "rm -rf /tmp/x"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "ls && rm -rf /"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "PATH=/tmp ls"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "/usr/bin/ls"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "git push"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "git diff --output=/tmp/out"}) is False
    assert supervision.is_read_only_call("terminal", {"command": "cat /etc/shadow"}) is False
    assert supervision.is_read_only_call("jev_unknown", {}) is False


# -- provenance ----------------------------------------------------------


def test_canonical_hash_is_stable_and_order_independent(supervision):
    assert supervision.canonical_hash({"a": 1, "b": 2}) == supervision.canonical_hash({"b": 2, "a": 1})
    assert supervision.canonical_hash({"a": 1}) != supervision.canonical_hash({"a": 2})


# -- telemetry -----------------------------------------------------------


def test_status_is_bounded_and_reports_advisory_authority(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.observe_event(_material_event(), turn_id="t1")
    report = sup.status(turn_id="t1")
    assert report["authority"] == "advisory"
    assert report["turn"]["events_forwarded"] == 1
    assert "recent_challenges" not in report


def test_status_reports_control_lease_authority_when_enforcing(sup):
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    sup.configure(mode="precommit")
    report = sup.status(turn_id="t1")
    assert report["authority"] == "local_control_lease"


def test_end_turn_summarizes_and_clears(sup):
    sup.begin_turn(turn_id="t1", session_id="s1", user_message="restart the production service")
    closed = sup.end_turn(turn_id="t1")
    assert closed["closed"] is True
    assert closed["admission"] == "ON"
    assert sup.current_turn(turn_id="t1") is None


def test_unknown_turn_is_safe(sup):
    assert sup.observe_event(_material_event(), turn_id="missing")["forward"] is False
    assert sup.take_challenge(turn_id="missing") is None
    assert sup.check_control(tool_name="terminal", args={}, turn_id="missing")["allow"] is True
    assert sup.record_tool_outcome(tool_name="terminal", args={}, result={}, turn_id="missing")["recorded"] is False

from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway  # noqa: E402
import closed_loop  # noqa: E402
import verification  # noqa: E402
import ingest  # noqa: E402
from tools import public_scan  # noqa: E402


def test_decide_keeps_destructive_actions_with_human() -> None:
    result = gateway.decide({"action": "delete_backup", "external": True})
    assert result["decision"] == "human"
    assert result["authority"] == "deterministic_policy"


def test_reversible_external_action_is_suggested() -> None:
    result = gateway.decide({"action": "restart_service", "external": True, "reversible": True})
    assert result["decision"] == "suggest"


def test_verification_requires_read_back_and_evidence() -> None:
    assert gateway.verify({"changed": True})["next"] == "read_back"
    assert gateway.verify({"changed": True, "read_back": True})["next"] == "inspect_evidence"
    assert gateway.verify({"changed": True, "read_back": True, "evidence": True})["verified"] is True


def test_verification_does_not_treat_missing_change_as_success() -> None:
    result = gateway.verify({})
    assert result["verified"] is False
    assert result["next"] == "establish_change"


def test_verification_requires_boolean_proof_fields() -> None:
    result = gateway.verify({"changed": True, "read_back": "yes", "evidence": ["claimed"]})
    assert result["verified"] is False
    assert result["next"] == "read_back"


def test_domain_classification_is_conservative() -> None:
    assert gateway.classify_case("research", {})["decision"] == "hold"
    assert gateway.classify_case("health", {})["decision"] == "human"
    assert gateway.classify_case("communication", {"creates_commitment": True})["next"] == "record_commitment"


def test_policy_does_not_treat_string_booleans_as_authority() -> None:
    assert gateway.decide({"external": "false", "reversible": "false"})["decision"] == "observe"
    assert gateway.classify_case("communication", {"creates_commitment": "false"})["next"] == "communication_review"


def test_observation_verification_requires_expected_state() -> None:
    assert verification.verify_observation("paperless", {}, "healthy")["next"] == "compare_expected"
    assert verification.verify_observation("paperless", {"expected_status": "healthy"}, "healthy")["verified"] is True
    assert verification.verify_observation("home_assistant", {"expected_state": "off"}, {"state": "on"})["verified"] is False


# An infrastructure state is a fact about the target, not a report about the
# collector. These four all contain the substring "error" or "timeout" and all
# used to be reported as unavailable, which wrote `awaiting_verification` into
# the closed loop for an observation the host had in fact verified.
@pytest.mark.parametrize("state", ["errored", "error_free", "no_error", "timeout_reached"])
def test_a_state_naming_an_error_is_a_verified_observation(state: str) -> None:
    result = verification.verify_observation("docker", {"expected": state}, {"state": state})
    assert result == {
        "verified": True, "status": "matched", "next": "done",
        "authority": "deterministic_verification",
    }


def test_a_word_inside_any_value_is_not_an_error_signal() -> None:
    """The signal is a KEY, not a word appearing somewhere in a value."""
    result = verification.verify_observation(
        "docker", {"expected": "online"}, {"state": "online", "note": "no_error since reboot"},
    )
    assert result["status"] == "matched"


def test_a_real_collector_failure_is_still_unavailable() -> None:
    result = verification.verify_observation("docker", {"expected": "errored"}, "collector_error: dial tcp: refused")
    assert result["verified"] is False
    assert result["status"] == "unavailable"
    assert result["next"] == "retry_or_inspect"


def test_a_dict_error_key_is_still_unavailable() -> None:
    result = verification.verify_observation(
        "docker", {"expected": "running"}, {"state": "running", "error": "boom"},
    )
    assert result["verified"] is False
    assert result["status"] == "unavailable"


def test_an_error_key_with_nothing_in_it_is_not_a_failure() -> None:
    """`{"error": None}` says there is no error. Reading it as one is the same
    bug as reading `errored` as one, in the other direction."""
    for empty in (None, "", [], {}):
        result = verification.verify_observation(
            "docker", {"expected": "ok"}, {"state": "ok", "error": empty},
        )
        assert result["status"] == "matched", empty


def test_a_timeout_key_is_a_signal_but_a_timeout_state_is_not() -> None:
    assert verification.verify_observation("nas", {"expected": "up"}, {"state": "up", "timeout": 30})["status"] == "unavailable"
    assert verification.verify_observation("nas", {"expected": "timeout_reached"}, {"state": "timeout_reached"})["status"] == "matched"


def test_a_plain_string_keeps_the_documented_contract() -> None:
    """A bare string carries no structure, so its whole content is the signal."""
    assert verification.verify_observation("paperless", {"expected": "healthy"}, "healthy")["status"] == "matched"
    assert verification.verify_observation("paperless", {"expected": "up"}, "request timeout after 30s")["status"] == "unavailable"
    assert verification.verify_observation("paperless", {"expected": "up"}, "Error: refused")["status"] == "unavailable"


def test_tool_results_correlate_concurrent_same_name_by_invocation(monkeypatch) -> None:
    opened = iter(("case-a", "case-b"))
    closed = []
    monkeypatch.setattr(ingest, "open_case", lambda *args, **kwargs: next(opened))
    monkeypatch.setattr(ingest, "close_case", lambda case, *args, **kwargs: closed.append(case))
    ingest._ACTIVE_CASES.clear()
    ingest._ACTIVE_INVOCATIONS.clear()
    ingest.ingest_event("hermes", "tool_call", {"tool_name": "same", "invocation_id": "a"})
    ingest.ingest_event("hermes", "tool_call", {"tool_name": "same", "invocation_id": "b"})
    assert ingest.update_tool_result("same", {}, True, "b") == "case-b"
    assert ingest.update_tool_result("same", {}, True, "a") == "case-a"
    assert closed == ["case-b", "case-a"]


def test_public_scan_passes() -> None:
    assert public_scan.scan() == []


def test_closed_loop_preserves_unknown_outcome(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JEV_HOME", str(tmp_path))
    decision = closed_loop.record_decision("Question", "observe")
    closed_loop.record_outcome(decision["decision_id"], "unknown", None)
    records = closed_loop.list_records()
    assert records[-1]["success"] is None


def test_networked_hooks_are_opt_in(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("jev_plugin_module", ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(plugin)

    class Context:
        def __init__(self):
            self.hooks = []

        def register_tool(self, *args, **kwargs):
            pass

        def register_hook(self, name, callback):
            self.hooks.append(name)

    context = Context()
    monkeypatch.delenv("JEV_ENABLE_HOOKS", raising=False)
    plugin.register(context)
    assert context.hooks == ["post_llm_call", "post_tool_call", "pre_tool_call"]

    context = Context()
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "true")
    plugin.register(context)
    assert context.hooks == ["post_llm_call", "post_tool_call", "pre_tool_call"]


def test_disabled_hook_does_not_call_provider_or_persist(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("jev_plugin_disabled", ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(plugin)
    monkeypatch.delenv("JEV_ENABLE_HOOKS", raising=False)
    monkeypatch.setenv("JEV_HOME", str(tmp_path))
    plugin._on_post_tool_call("example", {"changed": True}, "private result")
    assert list(tmp_path.rglob("*")) == []

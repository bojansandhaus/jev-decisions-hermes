import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import live_enforcement as live


@pytest.fixture
def ledger_home(monkeypatch, tmp_path):
    import ledger
    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(live, "append_ledger", ledger.append)
    return tmp_path


def approved_answers():
    return {
        "verdict": {"choice": "APPROVE", "confidence": 0.95},
        "policy_allows": {"noul": 0.95},
        "blast_radius": {"score": 0},
        "self_advocating": {"noul": 0.02},
        "reads_secrets": {"noul": 0.01},
        "sends_outbound": {"noul": 0.01},
    }


def test_action_authorization_returns_allow_with_receipt(ledger_home):
    result = live.authorize_action(
        {"tool_name": "write_file", "external": False, "reversible": True, "operator_policy": "workspace edits"},
        lambda workflow, state: approved_answers(),
    )
    assert result["decision"] == "allow"
    assert result["receipt_id"]
    assert "workspace edits" not in (ledger_home / "logs" / "jev-ledger.jsonl").read_text()


def test_deterministic_human_policy_wins_without_provider(ledger_home):
    called = []
    result = live.authorize_action(
        {"tool_name": "delete_backup", "destructive": True, "external": True, "reversible": False},
        lambda workflow, state: (called.append(True) or {}),
    )
    assert result["decision"] == "ask"
    assert called == []


def test_memory_gate_holds_sensitive_candidate(ledger_home):
    answers = {
        "retain": {"noul": 0.99},
        "kind": {"choice": "fact"},
        "sensitive": {"noul": 0.99},
        "conflict": {"noul": 0.01},
    }
    result = live.gate_memory({"candidate": "private health detail"}, lambda workflow, state: answers)
    assert result["decision"] == "hold"
    assert result["receipt_id"]


def test_memory_gate_retains_clean_preference(ledger_home):
    answers = {
        "retain": {"noul": 0.99},
        "kind": {"choice": "preference"},
        "sensitive": {"noul": 0.01},
        "conflict": {"noul": 0.01},
    }
    result = live.gate_memory({"candidate": "prefers concise answers"}, lambda workflow, state: answers)
    assert result["decision"] == "retain"
    assert result["kind"] == "preference"


def test_anomaly_triage_escalates_urgent(ledger_home):
    answers = {"anomaly": {"noul": 0.99}, "severity": {"choice": "urgent"}}
    result = live.triage_anomaly({"summary": "backup missing"}, lambda workflow, state: answers)
    assert result["action"] == "escalate"


def test_anomaly_triage_ignores_normal_variation(ledger_home):
    answers = {"anomaly": {"noul": 0.01}, "severity": {"choice": "info"}}
    result = live.triage_anomaly({"summary": "routine run took longer"}, lambda workflow, state: answers)
    assert result["action"] == "ignore"


def test_verify_action_requires_direct_proof(ledger_home):
    result = live.verify_action({"changed": True, "read_back": False, "evidence": True})
    assert result["verified"] is False
    assert result["next"] == "read_back"
    assert result["receipt_id"]


def test_plugin_registers_live_tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("jev_live_test", ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registered = {}

    class Context:
        def register_tool(self, name, toolset, schema, handler, description=""):
            registered[name] = (toolset, schema, handler)

        def register_hook(self, name, callback):
            pass

    module.register(Context())
    assert "jev_live" in registered
    assert registered["jev_live"][1]["parameters"]["required"] == ["action", "state"]


def test_live_hook_blocks_mutating_call_when_jev_is_uncertain(monkeypatch, tmp_path):
    import ledger
    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    spec = importlib.util.spec_from_file_location("jev_live_hook_test", ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "1")
    monkeypatch.setenv("JEV_LIVE_ENFORCEMENT", "1")
    monkeypatch.setattr(module, "_secret", lambda: "synthetic")
    uncertain = approved_answers()
    uncertain["verdict"] = {"choice": "ESCALATE", "confidence": 0.9}
    monkeypatch.setattr(module, "_request", lambda *args, **kwargs: {"answers": uncertain})
    directive = module._on_pre_tool_call("terminal", {"command": "write production file"}, session_id="live")
    assert directive["action"] == "block"
    assert "ask" in directive["message"]

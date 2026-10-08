"""Integration tests for the 0.3.0 supervision wiring inside the plugin.

These cover the parts that only exist once supervision is connected to the
plugin: the ``jev_supervision`` tool, hook composition, provider-call
deduplication on equivalent failures, and challenge delivery through a
model-visible workflow response.

The invariant under test throughout: the default (shadow) posture must not
change Hermes behavior at all. Only an explicitly selected enforcing mode may
emit a block directive.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    """Load the plugin module with local state redirected into tmp_path."""
    import ledger
    import closed_loop
    import supervision as supervision_module

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(closed_loop, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(supervision_module, "append_ledger", ledger.append)

    spec = importlib.util.spec_from_file_location("jev_supervision_audit", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_secret", lambda: "synthetic")
    monkeypatch.setattr(module, "_request", lambda *args, **kwargs: {"answers": {}})

    # Fresh supervision instance per test so counters never leak between tests.
    supervision_module._DEFAULT = supervision_module.Supervision(supervision_module.SupervisionConfig())
    monkeypatch.setattr(module, "_write_shadow_record", lambda record: None)
    return module


@pytest.fixture
def supervised(plugin, monkeypatch):
    """Hooks enabled and a supervised turn ready to use."""
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "1")
    sup = plugin._supervision.default_supervision()
    sup.reset()
    return plugin, sup


# -- tool surface --------------------------------------------------------


def test_status_reports_advisory_authority_by_default(plugin):
    result = json.loads(plugin.jev_supervision_handler({"action": "status"}))
    assert result["success"] is True
    assert result["mode"] == "shadow"
    assert result["authority"] == "advisory"


def test_begin_turn_classifies_locally(plugin):
    result = json.loads(plugin.jev_supervision_handler({
        "action": "begin_turn", "turn_id": "t1", "user_message": "delete the production backup",
    }))
    assert result["admission"] == "ON"


def test_observe_event_requires_an_event_object(plugin):
    result = json.loads(plugin.jev_supervision_handler({"action": "observe_event", "turn_id": "t1"}))
    assert "error" in result


def test_consider_challenge_requires_both_decisions(plugin):
    result = json.loads(plugin.jev_supervision_handler({
        "action": "consider_challenge", "hermes_decision": "PATCH",
    }))
    assert "error" in result


def test_challenge_round_trip_through_the_tool(plugin):
    plugin.jev_supervision_handler({"action": "begin_turn", "turn_id": "t1", "user_message": "delete the production backup"})
    created = json.loads(plugin.jev_supervision_handler({
        "action": "consider_challenge", "turn_id": "t1",
        "hermes_decision": "PATCH", "jev_decision": "ROLLBACK", "confidence": 0.95,
    }))
    assert created["challenged"] is True
    taken = json.loads(plugin.jev_supervision_handler({"action": "take_challenge", "turn_id": "t1"}))
    assert taken["delivered"] is True
    assert taken["challenge"]["jev_decision"] == "ROLLBACK"
    again = json.loads(plugin.jev_supervision_handler({"action": "take_challenge", "turn_id": "t1"}))
    assert again["delivered"] is False


def test_configure_returns_the_applied_config(plugin):
    result = json.loads(plugin.jev_supervision_handler({
        "action": "configure", "relevance_threshold": 0.4,
    }))
    assert result["success"] is True, result
    assert result["config"]["relevance_threshold"] == 0.4


def test_configure_rejects_unknown_only_payload(plugin):
    result = json.loads(plugin.jev_supervision_handler({"action": "configure", "nonsense": 1}))
    assert "error" in result


# The test above passed for the wrong reason. It called the handler directly, and
# the handler is not the boundary: `JEV_SUPERVISION_SCHEMA` sets
# additionalProperties False, so a field the handler accepts is still refused
# when a caller crosses the tool. Six of the seven settable fields were missing
# from the schema, which made `configure` able to switch `mode` and nothing else.
def test_every_settable_field_is_declared_in_the_tool_schema(plugin):
    properties = plugin.JEV_SUPERVISION_SCHEMA["parameters"]["properties"]
    missing = [key for key in plugin.JEV_SUPERVISION_SETTABLE if key not in properties]
    assert not missing, f"configure accepts these, the schema rejects them: {missing}"


# A minimal check of the exact keywords this schema uses, so the guard runs with
# pytest alone. CI installs pytest and nothing else, and a skipped drift test is
# exactly the failure this PR exists to prevent.
def _schema_rejects(schema: dict, payload: dict) -> str | None:
    """Return the reason `payload` is invalid, or None if it validates."""
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        extra = sorted(set(payload) - set(properties))
        if extra:
            return f"additional properties not allowed: {extra}"
    for name, value in payload.items():
        declared = properties.get(name)
        if declared is None:
            continue
        expected_type = declared.get("type")
        if expected_type == "boolean" and not isinstance(value, bool):
            return f"{name} must be boolean"
        if expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{name} must be integer"
        if expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"{name} must be number"
        if expected_type == "string" and not isinstance(value, str):
            return f"{name} must be string"
        if "enum" in declared and value not in declared["enum"]:
            return f"{name} must be one of {declared['enum']}"
    for required in schema.get("required", []):
        if required not in payload:
            return f"missing required property {required}"
    return None


def _settable_types(plugin) -> dict:
    properties = plugin.JEV_SUPERVISION_SCHEMA["parameters"]["properties"]
    return {name: properties[name]["type"] for name in plugin.JEV_SUPERVISION_SETTABLE}


def test_the_settable_set_is_the_documented_one(plugin):
    # `enabled` and `mode` are absent on purpose: they decide whether
    # supervision enforces, and this tool is called by the agent it gates.
    assert set(plugin.JEV_SUPERVISION_SETTABLE) == {
        "admission_enabled", "relevance_threshold",
        "challenge_confidence", "max_provider_calls_per_turn",
        "repeated_failure_replan_at",
    }


def test_the_schema_types_match_what_the_handler_accepts(plugin):
    """A wrong declared type would now fail at the boundary instead of being
    coerced by `configure`, so pin each type to the handler's own coercion.

    `mode` is declared for the read-only actions and still appears here through
    `JEV_SUPERVISION_SCHEMA`; it is not in `JEV_SUPERVISION_SETTABLE`, so it is
    not in this table either."""
    assert _settable_types(plugin) == {
        "admission_enabled": "boolean",
        "relevance_threshold": "number",
        "challenge_confidence": "number",
        "max_provider_calls_per_turn": "integer",
        "repeated_failure_replan_at": "integer",
    }


def test_the_enforcement_fields_are_not_declared_as_settable_inputs(plugin):
    """`mode` stays in the schema because the read-only actions read it, but it
    must not be settable, and `enabled` must not be an input at all."""
    properties = plugin.JEV_SUPERVISION_SCHEMA["parameters"]["properties"]
    assert "enabled" not in properties
    assert "mode" in properties, "the read-only actions take mode"
    assert "mode" not in set(plugin.JEV_SUPERVISION_SETTABLE)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("admission_enabled", False, False),
        ("relevance_threshold", 0.4, 0.4),
        ("challenge_confidence", 0.5, 0.5),
        ("max_provider_calls_per_turn", 7, 7),
        ("repeated_failure_replan_at", 5, 5),
    ],
)
def test_each_settable_field_survives_the_tool_boundary(plugin, field, value, expected):
    """Validate against the real schema, then run the handler, so a field cannot
    be settable in one and refused in the other."""
    args = {"action": "configure", field: value}
    reason = _schema_rejects(plugin.JEV_SUPERVISION_SCHEMA["parameters"], args)
    assert reason is None, f"the tool boundary refuses {field}: {reason}"
    result = json.loads(plugin.jev_supervision_handler(args))
    assert result["success"] is True, result
    assert result["config"][field] == expected


# The enforcement switch is the one thing the supervised agent must not reach.
# `jev_supervision` is a tool the gated party calls, so before this was pinned an
# agent could send `{"action": "configure", "mode": "shadow"}` and the next
# blocked action ran. `enabled` is refused by the schema outright, because
# nothing but `configure` ever took it; `mode` has to stay declared for the
# read-only actions, so it is refused by the handler instead.
@pytest.mark.parametrize("field,value", [("mode", "shadow"), ("enabled", False)])
def test_the_enforcement_switch_is_not_reachable_from_the_tool(plugin, field, value):
    args = {"action": "configure", field: value}
    schema_reason = _schema_rejects(plugin.JEV_SUPERVISION_SCHEMA["parameters"], args)
    result = json.loads(plugin.jev_supervision_handler(args))
    # Whichever layer stops it, nothing is applied and the caller is told.
    assert result.get("success") is not True, result
    assert "error" in result, result
    assert result.get("config", {}).get(field) != value, result
    assert schema_reason is not None or "enforcement" in result["error"], result


def test_the_enforcement_switch_still_works_for_the_operator(plugin):
    """Operator code configures through the object, not the model-facing tool,
    and the read-only status action reports the resulting mode."""
    plugin._supervision.default_supervision().configure(mode="correct_next")
    view = json.loads(plugin.jev_supervision_handler({"action": "status"}))
    assert view["success"] is True
    assert view["mode"] == "correct_next"


def test_configure_refuses_a_bundled_enforcement_switch(plugin):
    """The exact call an agent would make to un-gate itself, with a legitimate
    field attached so the refusal cannot be dismissed as a stray argument."""
    result = json.loads(plugin.jev_supervision_handler({
        "action": "configure", "mode": "shadow", "relevance_threshold": 0.4,
    }))
    assert result.get("success") is not True, result
    assert "error" in result, result
    assert "enforcement" in result["error"], result


def test_an_undeclared_property_is_still_refused_by_the_schema(plugin):
    """The drift guard must not have turned the schema into an anything-goes."""
    assert _schema_rejects(plugin.JEV_SUPERVISION_SCHEMA["parameters"], {"action": "configure", "nonsense": 1})


def test_unknown_action_is_reported(plugin):
    result = json.loads(plugin.jev_supervision_handler({"action": "explode"}))
    assert result["error"] == "unknown action"


def test_record_tool_outcome_requires_tool_name(plugin):
    result = json.loads(plugin.jev_supervision_handler({"action": "record_tool_outcome"}))
    assert "error" in result


# -- hook composition ----------------------------------------------------


def test_hooks_are_inert_without_the_opt_in(plugin, monkeypatch):
    monkeypatch.delenv("JEV_ENABLE_HOOKS", raising=False)
    assert plugin._on_pre_tool_call("terminal", {"command": "make build"}, session_id="s1") is None
    assert plugin._supervision.default_supervision().status()["active_turns"] == 0


def test_pre_tool_call_returns_none_in_shadow_even_with_active_control(supervised):
    plugin, sup = supervised
    # Build an active control by repeating one identical failure three times.
    for _ in range(3):
        plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    assert sup.status()["metrics"]["controls_created"] == 1
    assert plugin._on_pre_tool_call("terminal", {"command": "make build"}, session_id="s1") is None


def test_pre_tool_call_blocks_the_exact_repeat_when_enforcing(supervised):
    plugin, sup = supervised
    sup.configure(mode="correct_next")
    for _ in range(3):
        plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    directive = plugin._on_pre_tool_call("terminal", {"command": "make build"}, session_id="s1")
    assert isinstance(directive, dict)
    assert directive["action"] == "block"
    assert "make build" not in directive["message"] or "blocked" in directive["message"]
    assert "REPLAN" in directive["message"]


def test_pre_tool_call_allows_a_different_action_when_enforcing(supervised):
    plugin, sup = supervised
    sup.configure(mode="correct_next")
    for _ in range(3):
        plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    assert plugin._on_pre_tool_call("terminal", {"command": "make test"}, session_id="s1") is None


def test_allow_retry_then_the_action_is_permitted_once(supervised):
    plugin, sup = supervised
    sup.configure(mode="correct_next")
    for _ in range(3):
        plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    assert plugin._on_pre_tool_call("terminal", {"command": "make build"}, session_id="s1")["action"] == "block"
    plugin.jev_supervision_handler({"action": "allow_retry", "session_id": "s1"})
    assert plugin._on_pre_tool_call("terminal", {"command": "make build"}, session_id="s1") is None


def test_equivalent_failure_stops_generating_provider_calls(plugin, monkeypatch):
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "1")
    calls = []
    monkeypatch.setattr(plugin, "_request", lambda payload, key: calls.append(payload) or {"answers": {}})
    for _ in range(3):
        plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    assert len(calls) == 1, "only the first equivalent failure should reach the provider"


def test_distinct_failures_each_reach_the_provider(plugin, monkeypatch):
    monkeypatch.setenv("JEV_ENABLE_HOOKS", "1")
    calls = []
    monkeypatch.setattr(plugin, "_request", lambda payload, key: calls.append(payload) or {"answers": {}})
    plugin._on_post_tool_call("terminal", {"command": "make build"}, {"error": "boom"}, session_id="s1")
    plugin._on_post_tool_call("terminal", {"command": "make test"}, {"error": "different"}, session_id="s1")
    assert len(calls) == 2


def test_post_llm_call_records_admission_without_ending_the_turn(supervised):
    plugin, sup = supervised
    plugin._on_post_llm_call(
        assistant_response="done", user_message="delete the production backup", session_id="s1",
    )
    turn = sup.current_turn(session_id="s1")
    assert turn is not None
    assert turn.admission == "ON"


# -- challenge delivery through a workflow response ----------------------


def test_workflow_response_delivers_a_current_challenge(plugin):
    sup = plugin._supervision.default_supervision()
    sup.begin_turn(turn_id="t1", session_id="s1", user_message="restart the production service")
    sup.consider_challenge(
        hermes_decision="PATCH", jev_decision="ROLLBACK", confidence=0.95, turn_id="t1",
    )
    result = json.loads(plugin.jev_workflow_handler({
        "workflow": "plan_review", "state": {"plan": "patch it"}, "turn_id": "t1",
    }))
    assert result["supervision"]["challenge"]["jev_decision"] == "ROLLBACK"
    second = json.loads(plugin.jev_workflow_handler({
        "workflow": "plan_review", "state": {"plan": "patch it"}, "turn_id": "t1",
    }))
    assert second["supervision"]["challenge"] is None


def test_workflow_response_drops_a_stale_challenge(plugin, tmp_path):
    sup = plugin._supervision.default_supervision()
    sup.begin_turn(turn_id="t1", session_id="s1", user_message="restart the production service")
    sup.set_state_version(turn_id="t1", state_version="git:aaa")
    sup.consider_challenge(
        hermes_decision="PATCH", jev_decision="ROLLBACK", confidence=0.95,
        state_version="git:aaa", turn_id="t1",
    )
    sup.set_state_version(turn_id="t1", state_version="git:bbb")
    result = json.loads(plugin.jev_workflow_handler({
        "workflow": "plan_review", "state": {"plan": "patch it"}, "turn_id": "t1",
    }))
    assert result["supervision"]["challenge"] is None
    ledger_text = (tmp_path / "logs" / "jev-ledger.jsonl").read_text()
    assert "challenge_stale" in ledger_text


# -- registration --------------------------------------------------------


def test_plugin_declares_all_seven_tools_and_three_hooks():
    manifest = (ROOT / "plugin.yaml").read_text()
    for tool in (
        "jev_decide", "jev_workflow", "jev_ledger", "jev_gateway",
        "jev_ingest", "jev_loop", "jev_supervision",
    ):
        assert tool in manifest, f"{tool} missing from plugin.yaml"
    assert manifest.count("- pre_tool_call") == 1, "only one pre_tool_call owner is allowed"


def test_register_declares_the_supervision_tool(plugin):
    registered = {}

    class Context:
        def register_tool(self, name, toolset, schema, handler, description=""):
            registered[name] = toolset

        def register_hook(self, name, callback):
            registered[f"hook:{name}"] = callback

    plugin.register(Context())
    assert registered["jev_supervision"] == "jev"
    assert registered["hook:pre_tool_call"] is plugin._on_pre_tool_call
    assert sum(1 for key in registered if key.startswith("hook:")) == 3

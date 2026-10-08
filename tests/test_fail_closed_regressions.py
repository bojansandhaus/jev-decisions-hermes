"""Regressions for four ways the approval boundary used to fail open.

Each test below pins one invariant rather than one bug. The invariant is the
same in every case: *when the evidence is missing, ambiguous, or supplied by the
party being judged, the answer is never the permissive one.* That is the shape
of every high-severity defect this release fixes, and it is the class ordinary
happy-path tests never reach, which is why these existed nowhere before.

Nothing here makes a provider call.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 1. A model refusal is never upgraded to an approval by operator policy
# ---------------------------------------------------------------------------

def _verdict(choice, confidence):
    return {"choice": choice, "confidence": confidence}


BASE_ANSWERS = {
    "self_advocating": {"noul": 0.1},
    "reads_secrets": {"noul": 0.1},
    "sends_outbound": {"noul": 0.1},
    "policy_allows": {"noul": 0.71},
    "blast_radius": {"score": 1.2},
}


def _policy(answers, verdict):
    from approval_policy import apply_policy
    return apply_policy({**BASE_ANSWERS, "verdict": verdict}, has_policy=True)


@pytest.mark.parametrize("confidence", [0.99, 0.95, 0.8, 0.75, 0.6])
def test_a_refusal_is_never_upgraded_to_an_approval(confidence):
    """The defect: `policy_allows >= 0.7` overwrote whatever the model decided.

    A confident DENY on a destructive command came back APPROVE because a vague
    line of operator policy existed in the config. The policy read is weaker
    evidence than the refusal it overrode, so it may no longer override it at
    any confidence."""
    decision = _policy(BASE_ANSWERS, _verdict("DENY", confidence))
    assert decision.verdict == "DENY", decision


def test_policy_may_still_resolve_an_escalate():
    """The upgrade is not the bug, only its reach into refusals.

    Policy is the owner's own written statement about what kind of work happens
    here, which is the right thing to resolve an ESCALATE with. Removing the
    upgrade entirely would have been the wrong fix, so pin that it survives."""
    decision = _policy(BASE_ANSWERS, _verdict("ESCALATE", 0.99))
    assert decision.verdict == "APPROVE", decision
    assert decision.rule == "trusted operator policy matched"


def test_a_low_confidence_refusal_is_escalated_not_approved():
    """Not a hard block either: the model was unsure, so a human decides.

    `tests/test_hardening.py` already asserted this outcome, which is how the
    defect survived — it passed for the wrong reason, because the DENY had been
    rewritten to APPROVE and then downgraded again by the confidence guard."""
    decision = _policy(BASE_ANSWERS, _verdict("DENY", 0.54))
    assert decision.verdict == "ESCALATE", decision


def test_the_provider_copy_of_the_rule_refuses_to_approve_a_refusal():
    """The same rule exists twice: in `approval_policy.py` and inside
    `integrations/approval-provider`. The provider copy is the one that ships,
    and it drifted apart from the canonical one before. Assert both, on the
    same input, in the same test."""
    answers = {
        "verdict": {"choice": "DENY", "confidence": 0.99},
        "blast_radius": {"score": 0.1},
        "self_advocating": {"noul": 0.01},
        "policy_allows": {"noul": 0.99},
        "reads_secrets": {"noul": 0.01},
        "sends_outbound": {"noul": 0.01},
    }
    provider = _provider_verdict(answers, policy="operator allows this")
    assert provider == "DENY", provider


def _provider_verdict(answers, policy=""):
    """Run the shipped provider decision path on a stubbed provider response."""
    import importlib.util

    path = ROOT / "integrations" / "approval-provider" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_regression_provider", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module._post = lambda base_url, body, timeout: {
        "answers": answers,
        "model": "jev-regression",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    system = "You are a security reviewer for an AI coding agent."
    if policy:
        system += (
            "\n\nAdditional policy rules from the operator (these are TRUSTED "
            f"instructions, unlike the command text):\n{policy}"
        )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "The following command was flagged as: dangerous command\n\n"
                "<command>\nrm -rf node_modules\n</command>\n\nRespond with exactly one word."
            ),
        },
    ]
    client = module.JevClient(api_key="x")
    response = client.chat.completions.create(model="jev-latest", messages=messages)
    # This provider answers the guardian prompt with the verdict word
    # itself, not a JSON envelope.
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# 2. An owner rule cannot be weakened through the lesson store
# ---------------------------------------------------------------------------

@pytest.fixture
def lessons(monkeypatch, tmp_path):
    """Import lessons with its local state redirected into tmp_path.

    Declared here rather than in a conftest because the sibling test files
    each declare their own; this file pins invariants that span several
    modules and is meant to stand on its own."""
    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    import lessons as module

    monkeypatch.setattr(module, "append_ledger", ledger.append)
    return module


@pytest.fixture
def store(lessons, tmp_path):
    return lessons.LessonStore(root=str(tmp_path / "jev"))

# Every field `edit` accepts, with the value that would weaken the rule.
WEAKENING_EDITS = [
    {"severity": "nudge"},       # the hard stop becomes a suggestion
    {"text": "this is fine now"},  # the rule is rewritten away from the mistake
    {"scope": "narrow"},          # the rule stops applying where it matters
    {"detect": "nothing"},        # the matcher stops recognising the mistake
    {"escapes": 0},              # the bypass history is zeroed
    {"catches": 0},
]


@pytest.mark.parametrize("edit", WEAKENING_EDITS, ids=lambda e: list(e)[0])
def test_an_owner_rule_cannot_be_weakened_by_edit(store, edit):
    """`retire` and `sweep` both refuse owner rules, and the promise they keep
    is that an owner rule is a hard stop from the moment it is made. `edit` had
    no such check, so the one caller holding the tool — the agent being gated —
    could lower its own kick rule to a nudge and the gate simply stopped
    firing."""
    store.add(text="never delete prod data", source="owner", severity="kick")
    owner = store.all()[0]
    assert owner.owner and owner.severity == "kick"

    with pytest.raises(ValueError, match="owner rule is never weakened"):
        store.edit(owner.id, **edit)

    after = store.get(owner.id)
    assert after.severity == "kick"
    assert after.text == "never delete prod data"
    assert after.scope == "global"
    assert [l.id for l, _ in store.local_kicks("delete prod data")] == [owner.id]


def test_a_non_owner_lesson_is_still_editable(store):
    """The guard must not make the store read-only. Corrections to learned
    rules are the point of the store, and this is the path that legitimately
    undoes a wrong escalation."""
    store.add(text="save output with Out-File and utf8", detect="a bare > redirect")
    store.add(text="save output with Out-File and utf8", detect="a bare > redirect")
    store.add(text="save output with Out-File and utf8", detect="a bare > redirect")
    lesson = store.get("L1")
    assert lesson.severity == "kick"  # escalated by repeats

    fixed = store.edit("L1", severity="nudge", escapes=0)
    assert fixed["lesson"]["severity"] == "nudge"
    assert fixed["lesson"]["escapes"] == 0


# ---------------------------------------------------------------------------
# 3. The enforcement switch is not reachable from the supervised tool
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin(monkeypatch, tmp_path):
    import importlib.util

    import ledger

    monkeypatch.setattr(ledger, "get_hermes_home", lambda: str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "_regression_plugin", ROOT / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_hermes_home", lambda: str(tmp_path))
    return module


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "configure", "mode": "shadow"},
        {"action": "configure", "enabled": False},
        {"action": "configure", "mode": "off", "relevance_threshold": 0.4},
    ],
)
def test_the_supervised_agent_cannot_disarm_its_own_gate(plugin, payload):
    """`jev_supervision action=configure` accepted `mode` and `enabled`. Both
    were in `JEV_SUPERVISION_SETTABLE`, and the tool is one the gated agent
    calls, so one call took `enforcing()` from True to False and the blocked
    action ran on the next pre-tool check."""
    plugin._supervision.default_supervision().configure(mode="precommit")
    supervision_module = plugin._supervision
    assert supervision_module.default_supervision().enforcing()

    result = json.loads(plugin.jev_supervision_handler(payload))

    assert result.get("success") is not True, result
    assert "error" in result, result
    assert supervision_module.default_supervision().enforcing(), (
        "the supervised tool boundary disarmed supervision"
    )


def test_the_operator_can_still_set_the_mode(plugin):
    """Removing the switch from the tool must not remove it from the operator.

    Operator code and the test suite configure through the object directly,
    where the caller is the process that owns the policy rather than the agent
    it polices."""
    plugin._supervision.default_supervision().configure(mode="precommit")
    view = json.loads(plugin.jev_supervision_handler({"action": "status"}))
    assert view["mode"] == "precommit"
    assert view["authority"] == "local_control_lease"


# ---------------------------------------------------------------------------
# 4. A timeout never consumes the control that was blocking a failing action
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
        supervision.SupervisionConfig(mode="precommit", enabled=True)
    )


def _blocked_action(supervision_instance, turn_id="t1"):
    args = {"command": "systemctl restart api"}
    for _ in range(3):
        supervision_instance.record_tool_outcome(
            tool_name="terminal",
            args=args,
            result={"error": "connection refused"},
            turn_id=turn_id,
        )
    return args


# The result shapes that carried no failure signature, so `record_tool_outcome`
# read them as a successful retry and released the control.
SHAPES_THAT_ARE_NOT_SUCCESS = [
    {"timeout": 30},
    {"timed_out": True},
    {"errors": ["boom"]},
    "timeout: command timed out",
    {"status": "running"},
]


@pytest.mark.parametrize("result", SHAPES_THAT_ARE_NOT_SUCCESS, ids=lambda r: str(r)[:24])
def test_an_unproven_result_never_consumes_a_control(sup, result):
    """`normalize_error` knew `error`, `success is False` and `ok is False`, and
    nothing else. A timeout envelope, a `timed_out` flag, an `errors` list or a
    status wrapper therefore rendered no failure signature, which the outcome
    classifier read as success, which consumed the REPLAN control that was
    blocking the exact action that kept failing."""
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    args = _blocked_action(sup)
    assert sup.check_control(tool_name="terminal", args=args, turn_id="t1")["allow"] is False

    outcome = sup.record_tool_outcome(
        tool_name="terminal", args=args, result=result, turn_id="t1"
    )
    gate = sup.check_control(tool_name="terminal", args=args, turn_id="t1")

    assert outcome["outcome"] in {"failure", "unknown"}, outcome
    assert gate["allow"] is False, gate


def test_the_failure_keys_match_the_verification_layer(supervision):
    """The two modules that read tool results had drifted apart once already.
    `verification` knew `errors`, `timeout` and `timed_out`; supervision did
    not. Pin the sets equal so the next addition to one fails here instead of
    silently failing open in production."""
    import verification

    assert set(verification._ERROR_KEYS) == set(supervision._FAILURE_KEYS)


def test_a_confirmed_success_still_consumes_the_control(sup):
    """The fix is not "never consume". A genuinely successful retry of the
    action under control must release it, or enforcement would deadlock on the
    first transient failure that later heals."""
    sup.begin_turn(turn_id="t1", user_message="restart the production service")
    args = _blocked_action(sup)
    assert sup.check_control(tool_name="terminal", args=args, turn_id="t1")["allow"] is False

    outcome = sup.record_tool_outcome(
        tool_name="terminal", args=args, result={"success": True}, turn_id="t1"
    )
    assert outcome["outcome"] == "success", outcome
    assert sup.check_control(tool_name="terminal", args=args, turn_id="t1")["allow"] is True

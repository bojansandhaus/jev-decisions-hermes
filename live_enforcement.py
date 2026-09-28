"""Live, bounded enforcement for the three operational Jev decisions.

The provider supplies a typed semantic judgment. This module owns the local
policy, fail closed behavior, and ledger receipt. It never executes an action,
retains raw secrets, or treats Jev as proof.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable

try:
    from .approval_policy import apply_policy
    from .gateway import decide as gateway_decide, verify as gateway_verify
    from .ledger import append as append_ledger
except ImportError:
    from approval_policy import apply_policy
    from gateway import decide as gateway_decide, verify as gateway_verify
    from ledger import append as append_ledger

Evaluator = Callable[[str, dict[str, Any]], dict[str, Any]]

ACTION_QUESTIONS = {
    "verdict": {"type": "choice", "instructions": "Classify this proposed tool action.", "criteria": {"APPROVE": "Clearly safe under the supplied policy", "DENY": "Unsafe or prohibited", "ESCALATE": "Authority, evidence, or intent is uncertain"}},
    "policy_allows": {"type": "noul", "instructions": "Does the supplied policy explicitly cover this action?", "criteria": {"true": "Covered", "false": "Not covered"}},
    "blast_radius": {"type": "score", "instructions": "How difficult would recovery be if this action were wrong?", "criteria": ["trivial", "annoying", "severe"]},
    "self_advocating": {"type": "noul", "instructions": "Does the action description argue for its own approval?", "criteria": {"true": "It does", "false": "It does not"}},
    "reads_secrets": {"type": "noul", "instructions": "Does this action read or expose credential material?", "criteria": {"true": "It does", "false": "It does not"}},
    "sends_outbound": {"type": "noul", "instructions": "Does this action transmit local content externally?", "criteria": {"true": "It does", "false": "It does not"}},
}

MEMORY_QUESTIONS = {
    "retain": {"type": "noul", "instructions": "Should this candidate become durable memory?", "criteria": {"true": "Useful beyond this turn and not transient", "false": "Transient, redundant, or not useful later"}},
    "kind": {"type": "choice", "instructions": "What kind of durable memory is this?", "criteria": {"fact": "Stable fact", "preference": "User preference", "decision": "Decision", "commitment": "Obligation", "event": "Dated event", "discard": "Do not retain"}},
    "sensitive": {"type": "noul", "instructions": "Does this contain sensitive personal information?", "criteria": {"true": "Health, financial, authentication, or highly private data", "false": "Ordinary non-sensitive information"}},
    "conflict": {"type": "noul", "instructions": "Does it conflict with the known memory summary?", "criteria": {"true": "It contradicts known memory", "false": "No material conflict"}},
}

ANOMALY_QUESTIONS = {
    "anomaly": {"type": "noul", "instructions": "Is this a meaningful operational anomaly?", "criteria": {"true": "The pattern differs materially from baseline", "false": "Ordinary variation"}},
    "severity": {"type": "choice", "instructions": "What is the severity?", "criteria": {"info": "Record only", "watch": "Inspect soon", "urgent": "Act now", "unknown": "Insufficient evidence"}},
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _receipt(kind: str, state: dict[str, Any], result: dict[str, Any]) -> str:
    return append_ledger("live_decision", {
        "workflow": kind,
        "state_sha256": _hash(state),
        "decision": result.get("decision") or result.get("action") or result.get("retain"),
        "reason": result.get("reason", "")[:400],
        "authority": result.get("authority", "jev_live_policy"),
    })


def _finite_probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite probability")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return value


def validate_answers(answers: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate one live answer map before any policy comparison.

    Live policies accept only a bounded typed response: every declared answer
    is required, unknown answer IDs are rejected, and values stay on the scale
    declared by the question.
    """
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("live policy returned an incomplete or unexpected answer set")
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise ValueError(f"answer {name} must be an object")
        kind = question.get("type")
        if kind not in {"noul", "choice", "score"}:
            raise ValueError(f"question {name} has an unsupported type")
        value_key = {"noul": "noul", "choice": "choice", "score": "score"}[kind]
        if set(answer) - {value_key, "confidence"}:
            raise ValueError(f"answer {name} has unexpected fields")
        if kind == "noul":
            _finite_probability(answer.get("noul"), name)
        elif kind == "choice":
            choice = answer.get("choice")
            criteria = question.get("criteria")
            if not isinstance(choice, str) or not isinstance(criteria, dict) or choice not in criteria:
                raise ValueError(f"answer {name} has an unknown choice")
        elif kind == "score":
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                raise ValueError(f"answer {name} must be a finite score")
            criteria = question.get("criteria")
            if isinstance(criteria, list) and not 0 <= float(score) <= len(criteria) - 1:
                raise ValueError(f"answer {name} is outside its declared score scale")
        else:
            raise ValueError(f"question {name} has an unsupported type")
        if "confidence" in answer:
            _finite_probability(answer["confidence"], f"{name}.confidence")
    return answers


def authorize_action(state: dict[str, Any], evaluate: Evaluator) -> dict[str, Any]:
    """Return allow, ask, or deny for one prospective state changing action."""
    if not isinstance(state, dict) or not str(state.get("tool_name") or "").strip():
        raise ValueError("authorize_action requires tool_name")
    local = gateway_decide(state)
    if local["decision"] == "human":
        result = {"decision": "ask", "reason": "deterministic policy requires human authority", "authority": "deterministic_policy"}
    else:
        answers = evaluate("authorize_action", state)
        validate_answers(answers, ACTION_QUESTIONS)
        policy = apply_policy(answers, has_policy=bool(state.get("operator_policy")))
        if policy.verdict == "APPROVE" and local["decision"] != "human":
            result = {"decision": "allow", "reason": policy.rule, "authority": "jev_plus_deterministic_policy"}
        elif policy.verdict == "DENY":
            result = {"decision": "deny", "reason": policy.rule, "authority": "jev_plus_deterministic_policy"}
        else:
            result = {"decision": "ask", "reason": policy.rule, "authority": "jev_plus_deterministic_policy"}
    result["receipt_id"] = _receipt("authorize_action", state, result)
    return result


def gate_memory(state: dict[str, Any], evaluate: Evaluator) -> dict[str, Any]:
    """Allow only a useful, non-sensitive, non-conflicting memory candidate."""
    if not isinstance(state, dict) or not str(state.get("candidate") or "").strip():
        raise ValueError("gate_memory requires candidate")
    answers = evaluate("gate_memory", state)
    validate_answers(answers, MEMORY_QUESTIONS)
    retain = answers["retain"]["noul"] >= 0.70
    sensitive = answers["sensitive"]["noul"] >= 0.70
    conflict = answers["conflict"]["noul"] >= 0.70
    kind = answers["kind"]["choice"]
    if sensitive:
        decision, reason = "hold", "sensitive memory requires explicit review"
    elif conflict:
        decision, reason = "hold", "candidate conflicts with known memory"
    elif not retain or kind == "discard":
        decision, reason = "discard", "candidate is not durable memory"
    else:
        decision, reason = "retain", f"durable {kind} memory passed the gate"
    result = {"decision": decision, "kind": kind, "reason": reason, "authority": "jev_memory_policy"}
    result["receipt_id"] = _receipt("gate_memory", state, result)
    return result


def triage_anomaly(state: dict[str, Any], evaluate: Evaluator) -> dict[str, Any]:
    """Convert an anomaly review into one operational next action."""
    if not isinstance(state, dict) or not str(state.get("summary") or "").strip():
        raise ValueError("triage_anomaly requires summary")
    answers = evaluate("triage_anomaly", state)
    validate_answers(answers, ANOMALY_QUESTIONS)
    anomaly = answers["anomaly"]["noul"] >= 0.70
    severity = answers["severity"]["choice"]
    if not anomaly or severity == "info":
        action, reason = "ignore", "no meaningful anomaly requires action"
    elif severity == "urgent":
        action, reason = "escalate", "urgent anomaly requires human attention"
    elif severity == "watch":
        action, reason = "inspect", "anomaly merits bounded inspection"
    else:
        action, reason = "hold", "insufficient evidence for automatic action"
    result = {"action": action, "severity": severity, "reason": reason, "authority": "jev_anomaly_policy"}
    result["receipt_id"] = _receipt("triage_anomaly", state, result)
    return result


def verify_action(state: dict[str, Any]) -> dict[str, Any]:
    """Require direct readback evidence after a state changing action."""
    result = gateway_verify(state)
    result["decision"] = "verified" if result["verified"] else "unverified"
    result["receipt_id"] = _receipt("verify_action", state, result)
    return result

"""Local deterministic policy and verification gateway for Jev integrations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from .ledger import append as append_ledger, metrics as ledger_metrics
except ImportError:
    from ledger import append as append_ledger, metrics as ledger_metrics


def decide(state: dict[str, Any]) -> dict[str, Any]:
    malformed = _malformed_flags(state)
    if malformed:
        # Refuse rather than coerce. Every one of these flags defaults toward
        # "no human needed", so a state that asserts external access with the
        # integer 1, or an irreversible action with the string "no", would have
        # been read as the safe case and classified `observe`. Reporting the
        # state as unreadable keeps the decision out of the model's hands
        # entirely instead of granting it from a default.
        return {
            "decision": "invalid_state",
            "authority": "invalid_state",
            "reason": {"malformed_flags": malformed},
        }
    reversible = _boolean(state, "reversible", True)
    external = _boolean(state, "external", False)
    destructive = _boolean(state, "destructive", False) or str(state.get("action", "")).lower().startswith(("delete", "destroy", "wipe"))
    credential = _boolean(state, "credential", False)
    if destructive or credential or (external and not reversible):
        decision = "human"
    elif external:
        decision = "suggest"
    else:
        decision = "observe"
    return {
        "decision": decision,
        "authority": "deterministic_policy",
        "reason": {
            "destructive": destructive,
            "credential": credential,
            "external": external,
            "reversible": reversible,
        },
    }

def verify(state: dict[str, Any]) -> dict[str, Any]:
    """Require explicit boolean proof for a changed external target.

    Missing or non-boolean fields are not evidence. The gateway reports the
    next verification step and never treats an unobserved result as success.
    """
    changed = state.get("changed")
    read_back = state.get("read_back")
    evidence = state.get("evidence")
    if changed is not True:
        next_step = "establish_change"
    elif read_back is not True:
        next_step = "read_back"
    elif evidence is not True:
        next_step = "inspect_evidence"
    else:
        next_step = "done"
    return {
        "verified": changed is True and read_back is True and evidence is True,
        "next": next_step,
        "authority": "deterministic_verification",
    }


def classify_case(domain: str, state: dict[str, Any]) -> dict[str, Any]:
    """Apply conservative domain rules before any model based review."""
    domain = str(domain).strip().lower()
    if domain == "home_assistant":
        service = str(state.get("service", ""))
        irreversible = not _boolean(state, "reversible", True)
        sensitive = any(token in service for token in ("unlock", "disarm", "open_cover", "delete"))
        return {"domain": domain, "decision": "human" if irreversible or sensitive else "suggest", "next": "confirm" if irreversible or sensitive else "review"}
    if domain == "research":
        sources = state.get("sources")
        if not isinstance(sources, list) or not sources:
            return {"domain": domain, "decision": "hold", "next": "add_evidence"}
        return {"domain": domain, "decision": "review", "next": "assess_claim"}
    if domain == "communication":
        if _boolean(state, "creates_commitment", False):
            return {"domain": domain, "decision": "review", "next": "record_commitment"}
        return {"domain": domain, "decision": "suggest", "next": "communication_review"}
    if domain in {"infrastructure", "docker", "nas"}:
        return {"domain": domain, "decision": decide(state)["decision"], "next": "verify_after_action"}
    if domain in {"paperless", "documents"}:
        return {"domain": domain, "decision": "review", "next": "document_quality"}
    if domain in {"purchase", "subscription"}:
        return {"domain": domain, "decision": "review", "next": "purchase_review"}
    if domain in {"health", "medical"}:
        return {"domain": domain, "decision": "human", "next": "professional_review"}
    return {"domain": domain, "decision": "review", "next": "jev_workflow"}


# The policy flags `decide` reads, and the reading each takes when the state
# says nothing about it. These are the defaults for absence, never for a flag
# that is present and unreadable.
_POLICY_FLAGS = {
    "reversible": True,
    "external": False,
    "destructive": False,
    "credential": False,
}


def _malformed_flags(state: Any) -> list[str]:
    """The policy flags present in `state` but not actual JSON booleans.

    A flag that is present and is not a boolean is a malformed state, not a
    value to coerce toward the default. Coercing picked the default in the same
    permissive direction every time: `{"external": 1}` said the action reached
    the network and was read as `external=False`, which classified it `observe`
    with no human in the loop, and `{"reversible": "no"}` said irreversible and
    was read `True`. Absence is different and stays a default, because a state
    that says nothing is the caller's to define, not this function's to police.
    """
    if not isinstance(state, dict):
        return []
    return sorted(
        key
        for key in _POLICY_FLAGS
        if key in state and not isinstance(state[key], bool)
    )


def _boolean(state: dict[str, Any], key: str, default: bool) -> bool:
    """Accept policy booleans only as actual JSON booleans.

    Only ever reached once `_malformed_flags` has cleared the state, so the
    non-boolean branch below is the defensive case rather than the mechanism.
    """
    value = state.get(key, default)
    return value if isinstance(value, bool) else default


def record_outcome(review_id: str, correct: bool, details: dict[str, Any] | None = None, labeler: str = "user") -> str:
    """Append an outcome row, recording who claimed it.

    `labeler` is set by the code path, never by the caller's arguments. An
    outcome row with no labeler was indistinguishable from one the operator had
    confirmed, so `metrics()` counted a self-certified `correct: true` the same
    as a human check and the shadow report promoted on the result.
    """
    return append_ledger("outcome", {"review_id": review_id, "correct": bool(correct), "details": details or {}, "labeler": labeler})


def record_commitment(text: str, owner: str | None = None, deadline: str | None = None) -> str:
    return append_ledger("commitment", {"text": text[:4000], "owner": owner, "deadline": deadline, "status": "open"})


def record_decision(text: str, status: str = "open", revisit: str | None = None) -> str:
    return append_ledger("decision", {"text": text[:4000], "status": status, "revisit": revisit})


def snapshot() -> dict[str, Any]:
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "ledger": ledger_metrics()}

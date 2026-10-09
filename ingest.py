"""Route events from Hermes and connected systems into Jev cases."""
from __future__ import annotations

from typing import Any

try:
    from .fabric import close_case, open_case
except ImportError:
    from fabric import close_case, open_case

_ACTIVE_CASES: dict[str, list[str]] = {}
_ACTIVE_INVOCATIONS: dict[str, str] = {}

# Neither map can grow without bound in a process that never restarts. The
# cleanup below used to run only on `verified`, and a result verifies False
# whenever it carries no expected field — the ordinary case for a tool call — so
# every hooked result leaked one entry and one case row for the life of the
# process. The bound is a last line behind that fix, not a substitute for it.
_MAX_TRACKED_CASES = 512
_MAX_TRACKED_INVOCATIONS = 512


def _remember(tool_name: str, case_id: str, invocation_id: Any) -> None:
    pending = _ACTIVE_CASES.setdefault(tool_name, [])
    pending.append(case_id)
    while len(pending) > _MAX_TRACKED_CASES:
        _forget_case(pending.pop(0), tool_name)
    key = str(invocation_id)
    _ACTIVE_INVOCATIONS[key] = case_id
    while len(_ACTIVE_INVOCATIONS) > _MAX_TRACKED_INVOCATIONS:
        _ACTIVE_INVOCATIONS.pop(next(iter(_ACTIVE_INVOCATIONS)))


def _forget_case(case_id: str, tool_name: str) -> None:
    """Drop one case id from both maps. The case row itself is left alone."""
    for invocation_id, tracked in list(_ACTIVE_INVOCATIONS.items()):
        if tracked == case_id:
            _ACTIVE_INVOCATIONS.pop(invocation_id, None)
    pending = _ACTIVE_CASES.get(tool_name)
    if pending and case_id in pending:
        pending.remove(case_id)
        if not pending:
            _ACTIVE_CASES.pop(tool_name, None)


def _domain(source: str, event_type: str, payload: dict[str, Any]) -> str:
    text = f"{source} {event_type} {payload.get('tool_name', '')} {payload.get('service', '')}".lower()
    if source in {"telegram", "email", "discord", "matrix"} or event_type in {"draft", "message"}:
        return "communication"
    if "hindsight" in text or "memory" in text:
        return "hindsight"
    if "paperless" in text or event_type == "document":
        return "documents"
    if source in {"home_assistant", "ha"} or "home_assistant" in text:
        return "home_assistant"
    if event_type in {"tool_call", "service_change", "backup", "restore", "alert"}:
        return "infrastructure"
    if event_type in {"claim", "research", "citation"}:
        return "research"
    if event_type in {"purchase", "renewal"}:
        return "purchase"
    return "general"


def ingest_event(source: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    domain = _domain(source, event_type, payload)
    case_id = open_case(domain, {"source": source, "event_type": event_type, **payload})
    tool_name = payload.get("tool_name")
    if event_type == "tool_call" and tool_name:
        _remember(str(tool_name), case_id, payload.get("invocation_id") or "")
    return {"case_id": case_id, "domain": domain, "source": source, "event_type": event_type}


def update_tool_result(
    tool_name: str,
    result: Any,
    verified: bool = False,
    invocation_id: str | None = None,
) -> str | None:
    key = str(tool_name)
    if invocation_id:
        # An invocation id that is present but unknown is an unknown case, not
        # the oldest open one. Falling through to `pending[0]` resolved a case
        # belonging to a *different* call of the same tool while the call this
        # result was actually about stayed open forever.
        case_id = _ACTIVE_INVOCATIONS.get(str(invocation_id))
    else:
        pending = _ACTIVE_CASES.get(key, [])
        case_id = pending[0] if pending else None
    if not case_id:
        return None
    status = "resolved" if verified else "awaiting_verification"
    close_case(case_id, status, {"tool_name": tool_name, "result": str(result)[:4000], "verified": verified})
    # Every outcome is terminal for the case, so every outcome frees the slot.
    # Freeing only on `verified` leaked one entry and one case row per hooked
    # tool result whose verification was not a hard True, which is most of them,
    # because a result carrying no expected field verifies False.
    _forget_case(case_id, key)
    return case_id

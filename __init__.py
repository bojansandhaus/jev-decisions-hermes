"""Hermes plugin exposing Jev's typed Decisions API over a key, or Laya locally."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from .runtime import get_secret, get_hermes_home
except ImportError:
    from runtime import get_secret, get_hermes_home

try:
    from .ledger import append as append_ledger, metrics as ledger_metrics, read as read_ledger
    from .gateway import decide as gateway_decide, verify as gateway_verify, snapshot as gateway_snapshot, classify_case as gateway_classify
except ImportError:
    from ledger import append as append_ledger, metrics as ledger_metrics, read as read_ledger
    from gateway import decide as gateway_decide, verify as gateway_verify, snapshot as gateway_snapshot, classify_case as gateway_classify
try:
    from .ingest import ingest_event, update_tool_result
except ImportError:
    from ingest import ingest_event, update_tool_result
try:
    from .closed_loop import assess as loop_assess, label_outcome as loop_label_outcome, list_records as loop_list, record_decision as loop_record_decision, record_observation as loop_record_observation, record_outcome as loop_record_outcome, reopen as loop_reopen
except ImportError:
    from closed_loop import assess as loop_assess, label_outcome as loop_label_outcome, list_records as loop_list, record_decision as loop_record_decision, record_observation as loop_record_observation, record_outcome as loop_record_outcome, reopen as loop_reopen
try:
    from .verification import verify_observation
except ImportError:
    from verification import verify_observation

try:
    from .approval_policy import apply_policy
except ImportError:
    from approval_policy import apply_policy
try:
    from .jev_client import LAYA_TIMEOUT_S, provider_keys, provider_mode, request_decisions, uses_local_hop
except ImportError:
    from jev_client import LAYA_TIMEOUT_S, provider_keys, provider_mode, request_decisions, uses_local_hop
try:
    from . import supervision as _supervision
except ImportError:
    import supervision as _supervision
try:
    from . import lessons as _lessons
except ImportError:
    import lessons as _lessons
_TOOLSET = "jev"
_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
_MODEL = "typesafe/jev-1.13"




class _ProviderSchemaError(RuntimeError):
    """The provider answered, but not with the typed Decisions shape."""


def _hooks_enabled() -> bool:
    """Enable networked observer hooks only when explicitly opted in."""
    return os.environ.get("JEV_ENABLE_HOOKS", "").strip().lower() in {"1", "true", "yes", "on"}

JEV_DECIDE_SCHEMA = {
    "name": "jev_decide",
    "description": (
        "Evaluate bounded text or JSON state with Jev using typed Noul, Choice, "
        "or Score questions. Returns probabilities and confidence, never prose."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {"description": "Bounded text, object, or array to evaluate."},
            "questions": {
                "type": "object",
                "description": (
                    "Map of IDs to question objects. Each object requires type exactly "
                    "noul, choice, or score; instructions; and criteria. Noul criteria "
                    "is an object with true and false strings, choice criteria is an "
                    "object of option names to descriptions, score criteria is an array."
                ),
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["noul", "choice", "score"]},
                        "instructions": {"type": "string"},
                        "criteria": {},
                    },
                    "required": ["type", "instructions", "criteria"],
                    "additionalProperties": False,
                },
            },
            "model": {
                "type": "string",
                "description": "Pinned Jev model. Defaults to typesafe/jev-1.13.",
            },
        },
        "required": ["state", "questions"],
        "additionalProperties": False,
    },
}


def _secret() -> str:
    """The active mode's primary credential, or none when its chain starts local."""
    mode = provider_mode()
    if mode == "laya":
        # A local server needs a credential only when it was started with its own
        # bearer check, which `LAYA_API_KEY` carries. Otherwise there is none.
        return get_secret("LAYA_API_KEY") or ""
    names = provider_keys(mode)
    if not names:
        return ""
    value = get_secret(names[0])
    if not value:
        raise RuntimeError(f"{names[0]} is not available in the active Hermes secret scope")
    return value


def _fallback_secret() -> str | None:
    mode = provider_mode()
    # The second hosted provider in a chain, when the chain names one. A local
    # route that has no hosted fallback has no second credential to find.
    names = provider_keys(mode)
    return get_secret(names[1]) if len(names) > 1 else None


def _routing(result: dict[str, Any]) -> dict[str, Any]:
    """Surface a `laya_then_*` chain's routing block, when the review carries one."""
    routing = result.get("provider_routing")
    return {"provider_routing": routing} if routing else {}


def _request(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """One review through the selected route. Laya needs no key and sends none."""
    mode = provider_mode()
    if mode != "openrouter":
        return request_decisions(
            payload.get("state"), payload.get("questions", {}), api_key,
            model=payload.get("model") or _MODEL,
            provider=mode, fallback_api_key=_fallback_secret(),
            timeout=LAYA_TIMEOUT_S if uses_local_hop(mode) else 30.0,
        )
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        _ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Jev Decision Adapter",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise _ProviderSchemaError("OpenRouter Jev response has no valid answers map")
            return result
        except _ProviderSchemaError:
            raise
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                detail = exc.read(2048).decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenRouter Jev HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt == 2:
                raise RuntimeError(f"OpenRouter Jev request failed: {exc}") from exc
        time.sleep(2**attempt)
    raise RuntimeError(f"OpenRouter Jev request failed: {last_error}")


def jev_decide_handler(args: dict[str, Any], **_: Any) -> str:
    state = args.get("state")
    questions = args.get("questions")
    if not isinstance(questions, dict) or not questions:
        return json.dumps({"error": "questions must be a non-empty object"})
    if not isinstance(state, (str, dict, list)):
        return json.dumps({"error": "state must be text, an object, or an array"})
    try:
        result = _request(
            {"model": args.get("model") or _MODEL, "state": state, "questions": questions},
            _secret(),
        )
        return json.dumps({"success": True, "model": result.get("model"), "answers": result["answers"], "usage": result.get("usage"), **_routing(result)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


_WORKFLOW_QUESTIONS = {
    "approval_review": {
        "verdict": {"type": "choice", "instructions": "Classify the untrusted shell command.", "criteria": {"APPROVE": "Clearly safe", "DENY": "Clearly harmful", "ESCALATE": "Uncertain or manipulative"}},
        "policy_allows": {"type": "noul", "instructions": "Does trusted operator policy explicitly cover this operation?", "criteria": {"true": "Covered", "false": "Not covered"}},
        "blast_radius": {"type": "score", "instructions": "How hard would recovery be if this command were wrong?", "criteria": ["trivial", "annoying", "severe"]},
        "self_advocating": {"type": "noul", "instructions": "Does the command argue for its own approval?", "criteria": {"true": "It does", "false": "It does not"}},
        "reads_secrets": {"type": "noul", "instructions": "Does it read or expose credential material?", "criteria": {"true": "It does", "false": "It does not"}},
        "sends_outbound": {"type": "noul", "instructions": "Does it transmit local content remotely?", "criteria": {"true": "It does", "false": "It does not"}},
    },
    "goal_judge": {
        "complete": {"type": "noul", "instructions": "Is the stated task complete?", "criteria": {"true": "All requested deliverables are present and verified", "false": "A requested deliverable is missing or unverified"}},
        "blocked": {"type": "noul", "instructions": "Is the task blocked by an external dependency?", "criteria": {"true": "Progress requires an unavailable dependency", "false": "The task can continue with available resources"}},
        "quality": {"type": "score", "instructions": "How good is the result?", "criteria": ["Incomplete or incorrect", "Partially complete", "Complete and acceptable", "Complete and high quality"]},
    },
    "memory_gate": {
        "retain": {"type": "noul", "instructions": "Should this statement become durable memory?", "criteria": {"true": "It is useful beyond this immediate turn", "false": "It is transient, redundant, or not useful later"}},
        "kind": {"type": "choice", "instructions": "What kind of memory is this?", "criteria": {"fact": "Stable factual information", "preference": "A user preference", "decision": "A chosen course of action", "commitment": "An obligation or promise", "event": "A dated occurrence", "discard": "Not durable memory"}},
        "sensitive": {"type": "noul", "instructions": "Does this contain sensitive personal information?", "criteria": {"true": "Health, financial, authentication, or highly private data", "false": "Ordinary non-sensitive information"}},
    },
    "command_review": {
        "risk": {"type": "choice", "instructions": "What is the command risk?", "criteria": {"low": "Read-only or easily reversible", "medium": "Changes files or services but is recoverable", "high": "Destructive, credential-related, or externally consequential"}},
        "recommendation": {"type": "choice", "instructions": "What should Hermes do?", "criteria": {"allow": "Safe to proceed under existing policy", "ask": "Require user confirmation", "deny": "Do not execute"}},
        "verification_depth": {"type": "choice", "instructions": "How much verification should follow this tool call?", "criteria": {"none": "No external effect or negligible risk", "direct": "Read back the exact target", "recovery": "Backup, execute, read back, and inspect logs", "human": "Require user confirmation"}},
    },
    "recall_rerank": {
        "relevance": {"type": "score", "instructions": "How relevant are these memory candidates to the request?", "criteria": ["Irrelevant", "Weakly relevant", "Relevant", "Directly answers the request"]},
        "conflict": {"type": "noul", "instructions": "Do the candidates contain a material conflict?", "criteria": {"true": "They assert incompatible facts or preferences", "false": "They are compatible or merely different"}},
    },
    "action_verify": {
        "verified": {"type": "noul", "instructions": "Does the evidence prove the requested action succeeded?", "criteria": {"true": "The exact target was read back or an equivalent direct proof exists", "false": "The result is only claimed, partial, or inferred"}},
        "followup": {"type": "choice", "instructions": "What is the next action?", "criteria": {"done": "No further action is needed", "retry": "Retry the failed or incomplete operation", "ask": "Ask the user because evidence or authority is missing"}},
    },
    "output_review": {
        "grounded": {"type": "noul", "instructions": "Is the draft answer grounded in the supplied evidence?", "criteria": {"true": "Claims are supported by the evidence", "false": "The draft contains unsupported or invented claims"}},
        "complete": {"type": "noul", "instructions": "Does the draft answer every requested part?", "criteria": {"true": "All explicit requirements are addressed", "false": "One or more requirements are missing"}},
        "actionable": {"type": "noul", "instructions": "Does the draft give a concrete next action when one is needed?", "criteria": {"true": "The reader can act without guessing the next step", "false": "The answer only analyzes or reports"}},
        "next_action": {"type": "choice", "instructions": "What should happen next after this draft?", "criteria": {"act_now": "Take the concrete action stated or implied by the request", "ask_one_question": "Ask one blocking question", "wait_for_evidence": "Gather or verify evidence before acting", "schedule_followup": "Create a later followup", "nothing_needed": "No further action is needed"}},
        "circling": {"type": "noul", "instructions": "Is the draft circling a decision instead of moving it forward?", "criteria": {"true": "It reopens settled analysis or avoids a decision", "false": "It advances the decision or reports a clear result"}},
        "risk": {"type": "choice", "instructions": "What is the remaining answer risk?", "criteria": {"low": "Safe to send", "medium": "Needs a small correction or caveat", "high": "Must not be sent without revision"}},
    },
    "next_action": {
        "type": {"type": "choice", "instructions": "What kind of next step does this situation require?", "criteria": {"act_now": "Perform a concrete reversible action", "ask_one_question": "Ask one blocking question", "wait_for_evidence": "Verify missing evidence", "schedule_followup": "Set a future followup", "nothing_needed": "No next step is required"}},
        "specific": {"type": "noul", "instructions": "Is the proposed next step specific enough to execute?", "criteria": {"true": "A person or agent can execute it without guessing", "false": "It is vague, abstract, or missing an owner"}},
    },
    "decision_circling": {
        "circling": {"type": "noul", "instructions": "Is the conversation repeatedly reopening the same decision?", "criteria": {"true": "The same decision is being analyzed without convergence", "false": "The conversation is making new progress"}},
        "intervention": {"type": "choice", "instructions": "What intervention is appropriate?", "criteria": {"decide": "Choose and execute the best supported option", "ask": "Ask one question that resolves the block", "stop": "Stop gathering information because the threshold is met", "continue": "More analysis is justified"}},
    },
    "plan_review": {
        "outcome": {"type": "noul", "instructions": "Does the plan define a verifiable outcome?", "criteria": {"true": "Done means an observable result", "false": "The plan describes activity without a finish condition"}},
        "prerequisites": {"type": "noul", "instructions": "Are the prerequisites and dependencies identified?", "criteria": {"true": "Required inputs, authority, and dependencies are named", "false": "Execution may stall on an unstated dependency"}},
        "rollback": {"type": "noul", "instructions": "Does the plan include a safe rollback or recovery path?", "criteria": {"true": "Reversal or recovery is defined where needed", "false": "A failed step could leave unclear damage"}},
        "recommendation": {"type": "choice", "instructions": "What should happen to this plan?", "criteria": {"execute": "Ready to execute", "revise": "Revise before execution", "ask": "Ask for missing authority or information", "reject": "Do not execute"}},
    },
    "tool_result_verify": {
        "verified": {"type": "noul", "instructions": "Does the tool result prove the requested operation succeeded?", "criteria": {"true": "The exact target or equivalent direct evidence is present", "false": "The result is only a claim, partial, stale, or indirect"}},
        "side_effect": {"type": "choice", "instructions": "What kind of external effect occurred?", "criteria": {"none": "Read-only or no external effect", "reversible": "A reversible external change occurred", "irreversible": "An irreversible or externally consequential change occurred", "unknown": "The effect is unclear"}},
        "followup": {"type": "choice", "instructions": "What should Hermes do next?", "criteria": {"done": "Record completion", "read_back": "Read back the exact target", "retry": "Retry the operation", "ask": "Ask the user because evidence or authority is missing"}},
    },
    "memory_review": {
        "retain": {"type": "noul", "instructions": "Should this item be considered for durable memory?", "criteria": {"true": "Useful beyond this turn and not redundant", "false": "Transient, redundant, or not useful later"}},
        "kind": {"type": "choice", "instructions": "What durable memory kind best fits?", "criteria": {"fact": "Stable fact", "preference": "User preference", "decision": "Decision", "commitment": "Obligation", "event": "Dated event", "discard": "Do not retain"}},
        "conflict": {"type": "noul", "instructions": "Does this conflict with known memory?", "criteria": {"true": "It contradicts an existing fact or preference", "false": "No material conflict is visible"}},
    },
    "verification_depth": {
        "depth": {"type": "choice", "instructions": "How much verification does this action deserve?", "criteria": {"none": "No external effect or negligible risk", "direct": "One direct read back", "recovery": "Backup, execute, read back, and inspect", "human": "Require user confirmation before proceeding"}},
        "reason": {"type": "choice", "instructions": "What drives the verification depth?", "criteria": {"risk": "Potential downside", "irreversibility": "Hard to reverse", "uncertainty": "Evidence or authority is unclear", "routine": "Routine low risk operation"}},
    },
    "claim_status": {
        "status": {"type": "choice", "instructions": "What is the evidence status of this claim?", "criteria": {"observed": "Directly observed", "inferred": "Reasonable inference", "assumed": "An assumption", "unverified": "Insufficient evidence", "contradicted": "Conflicts with evidence"}},
        "citation_needed": {"type": "noul", "instructions": "Does this claim need stronger evidence before being stated as fact?", "criteria": {"true": "It is not directly supported", "false": "The evidence is sufficient"}},
    },
    "agent_referee": {
        "winner": {"type": "choice", "instructions": "Which candidate output is best supported?", "criteria": {"a": "Candidate A", "b": "Candidate B", "c": "Candidate C", "none": "No candidate is sufficient"}},
        "agreement": {"type": "score", "instructions": "How much do the candidates agree on the answer?", "criteria": ["Contradictory", "Weak overlap", "Mostly agree", "Strong agreement"]},
        "missing": {"type": "noul", "instructions": "Do all candidates miss a material requirement?", "criteria": {"true": "A requirement is absent from every candidate", "false": "The candidates cover the requirements"}},
    },
    "memory_maintenance": {
        "action": {"type": "choice", "instructions": "What maintenance should happen to this memory set?", "criteria": {"keep": "Keep as is", "merge": "Merge duplicates", "refresh": "Update with newer evidence", "quarantine": "Mark uncertain or conflicting", "discard": "Recommend removal"}},
        "reason": {"type": "choice", "instructions": "What is the main maintenance reason?", "criteria": {"duplicate": "Redundant entries", "stale": "Likely obsolete", "conflict": "Contradictory entries", "low_value": "Little future value", "clean": "No issue"}},
    },
    "anomaly_review": {
        "anomaly": {"type": "noul", "instructions": "Is there a meaningful operational anomaly?", "criteria": {"true": "The pattern differs from the expected baseline", "false": "The pattern is ordinary variation"}},
        "severity": {"type": "choice", "instructions": "What is the anomaly severity?", "criteria": {"info": "Track only", "watch": "Inspect soon", "urgent": "Act now", "unknown": "Insufficient evidence"}},
    },
    "infrastructure_review": {
        "risk": {"type": "choice", "instructions": "What is the operational risk?", "criteria": {"low": "Read-only or easily reversible", "medium": "Recoverable service or file change", "high": "Destructive, credential-related, or broad outage risk"}},
        "backup": {"type": "noul", "instructions": "Is there an adequate rollback or backup path?", "criteria": {"true": "A tested recovery path exists", "false": "Recovery is absent or unclear"}},
        "verification": {"type": "choice", "instructions": "What verification is required?", "criteria": {"none": "No external effect", "read_back": "Read back the exact target", "logs": "Read back and inspect logs", "human": "Require user confirmation"}},
    },
    "communication_review": {
        "send": {"type": "choice", "instructions": "What is the safest communication recommendation?", "criteria": {"send": "Ready to send", "revise": "Revise before sending", "ask": "Clarify recipient or authority", "hold": "Do not send yet"}},
        "commitment": {"type": "noul", "instructions": "Does this message create a material commitment?", "criteria": {"true": "It promises, accepts, approves, or commits", "false": "It creates no material commitment"}},
        "sensitive": {"type": "noul", "instructions": "Does the message contain sensitive information that needs review?", "criteria": {"true": "Sensitive or private information is present", "false": "No material sensitive information"}},
    },
    "evidence_review": {
        "support": {"type": "choice", "instructions": "How strongly do the sources support the claim?", "criteria": {"direct": "Directly supported", "inferred": "Reasonable inference", "weak": "Weak support", "contradicted": "Sources conflict with the claim"}},
        "citation": {"type": "noul", "instructions": "Does the claim need a citation before being stated as fact?", "criteria": {"true": "The evidence is not self evident or directly shown", "false": "The evidence is sufficient"}},
        "abstain": {"type": "noul", "instructions": "Should the answer abstain or state uncertainty?", "criteria": {"true": "Evidence is insufficient or contradictory", "false": "A bounded answer is supported"}},
    },
    "document_quality": {
        "duplicate": {"type": "noul", "instructions": "Is this document likely a duplicate?", "criteria": {"true": "It repeats an existing document", "false": "It is materially distinct"}},
        "metadata": {"type": "choice", "instructions": "What metadata action is needed?", "criteria": {"keep": "Metadata is adequate", "complete": "Add missing metadata", "correct": "Correct misclassification", "review": "Require human review"}},
        "memory": {"type": "choice", "instructions": "What should happen to extracted facts?", "criteria": {"retain": "Retain durable facts", "link": "Link to existing memory", "quarantine": "Hold for review", "discard": "Do not retain"}},
    },
    "purchase_review": {
        "fit": {"type": "choice", "instructions": "How well does the option fit the stated constraints?", "criteria": {"poor": "Misses material requirements", "partial": "Fits some requirements", "good": "Fits the requirements", "unknown": "Important information is missing"}},
        "evidence": {"type": "choice", "instructions": "How well verified are price, availability, and claims?", "criteria": {"verified": "Directly verified", "mixed": "Some claims verified", "weak": "Mostly unverified", "unknown": "Cannot verify"}},
        "action": {"type": "choice", "instructions": "What should happen next?", "criteria": {"buy": "Buy now", "compare": "Compare alternatives", "wait": "Wait for evidence or price", "avoid": "Do not buy"}},
    },
    "daily_anomaly": {
        "anomaly": {"type": "noul", "instructions": "Is there a meaningful deviation from the normal baseline?", "criteria": {"true": "The pattern is materially unusual", "false": "The pattern is ordinary variation"}},
        "severity": {"type": "choice", "instructions": "What is the operational severity?", "criteria": {"info": "Record only", "watch": "Inspect soon", "urgent": "Act now", "unknown": "Insufficient evidence"}},
        "owner": {"type": "choice", "instructions": "What response is appropriate?", "criteria": {"automated": "A safe deterministic response exists", "human": "Require user review", "observe": "Continue observing", "none": "No response needed"}},
    },
    "promotion_review": {
        "promote": {"type": "noul", "instructions": "Is this decision narrow, low risk, and accurate enough to move beyond shadow mode?", "criteria": {"true": "It has stable evidence, a measurable outcome, and a safe fallback", "false": "It is too uncertain, broad, or consequential"}},
        "scope": {"type": "choice", "instructions": "What is the safest promotion scope?", "criteria": {"observe": "Keep shadow-only", "suggest": "Show a recommendation to Hermes", "gate": "Allow a narrow deterministic gate", "human": "Require user review for each case"}},
        "missing": {"type": "choice", "instructions": "What evidence is still missing?", "criteria": {"labels": "More labeled outcomes", "coverage": "More representative cases", "fallback": "A tested deterministic fallback", "none": "No material gap"}},
    },
    "option_select": {
        "choice": {"type": "choice", "instructions": "Which option best fits the stated objective and constraints?", "criteria": {"a": "Option A", "b": "Option B", "c": "Option C", "none": "No option is sufficiently supported"}},
        "confidence": {"type": "score", "instructions": "How confident should the decision maker be?", "criteria": ["Insufficient evidence", "Weak recommendation", "Good recommendation", "Strong recommendation"]},
        "missing": {"type": "noul", "instructions": "Is a material piece of information missing?", "criteria": {"true": "The choice depends on unknown information", "false": "The available information is sufficient"}},
    },
    "escalation": {
        "escalate": {"type": "noul", "instructions": "Should Hermes stop and ask the user before proceeding?", "criteria": {"true": "Intent, authority, evidence, or safety is materially uncertain", "false": "The action is clear, authorized, and reversible or verified"}},
        "reason": {"type": "choice", "instructions": "What is the primary escalation reason?", "criteria": {"ambiguity": "User intent is unclear", "authority": "Permission or ownership is unclear", "risk": "The action has meaningful downside", "evidence": "The evidence is insufficient", "none": "No escalation needed"}},
    },
}

JEV_INGEST_SCHEMA = {
    "name": "jev_ingest",
    "description": "Route an event from Hermes or a connected system into a tracked Jev case.",
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "event_type": {"type": "string"},
            "payload": {"type": "object"},
        },
        "required": ["source", "event_type", "payload"],
        "additionalProperties": False,
    },
}


JEV_GATEWAY_SCHEMA = {
    "name": "jev_gateway",
    "description": "Use the local deterministic Jev policy and verification gateway. It never executes actions.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["decide", "verify", "classify", "snapshot"]},
            "domain": {"type": "string"},
            "state": {"type": "object"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


JEV_LEDGER_SCHEMA = {
    "name": "jev_ledger",
    "description": "Record or inspect Jev review outcomes, commitments, decisions, and calibration metrics. Local append-only state only.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["record_review", "record_outcome", "add_commitment", "add_decision", "close", "list", "metrics"]},
            "review_id": {"type": "string"},
            "correct": {"type": "boolean"},
            "text": {"type": "string"},
            "owner": {"type": "string"},
            "deadline": {"type": "string"},
            "status": {"type": "string"},
            "details": {"description": "Bounded structured details."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


_WORKFLOW_SCHEMA = {
    "name": "jev_workflow",
    "description": "Run a predefined Jev shadow evaluation for goal judging, memory gating, command review, recall reranking, or post-action verification. It recommends only and never authorizes, writes, deletes, or routes by itself.",
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {"type": "string", "enum": list(_WORKFLOW_QUESTIONS)},
            "state": {"description": "Bounded state relevant to the selected workflow."},
            "model": {"type": "string"},
            "turn_id": {"type": "string", "description": "Optional supervised turn to attach a current challenge from."},
            "session_id": {"type": "string"},
        },
        "required": ["workflow", "state"],
        "additionalProperties": False,
    },
}


def jev_workflow_handler(args: dict[str, Any], **kwargs: Any) -> str:
    workflow = args.get("workflow")
    state = args.get("state")
    if workflow not in _WORKFLOW_QUESTIONS:
        return json.dumps({"error": "unknown workflow", "allowed": list(_WORKFLOW_QUESTIONS)})
    if not isinstance(state, (str, dict, list)):
        return json.dumps({"error": "state must be text, an object, or an array"})
    try:
        result = _request({"model": args.get("model") or _MODEL, "state": state, "questions": _WORKFLOW_QUESTIONS[workflow]}, _secret())
        output = {"success": True, "shadow": True, "workflow": workflow, "model": result.get("model"), "answers": result["answers"], "usage": result.get("usage"), **_routing(result)}
        if workflow == "approval_review":
            decision = apply_policy(result["answers"], has_policy=bool(isinstance(state, dict) and state.get("operator_policy")))
            output.update({"verdict": decision.verdict, "applied_rule": decision.rule})
        output["supervision"] = _supervision_delivery(args, kwargs)
        return json.dumps(output)

    except Exception as exc:
        return json.dumps({"error": str(exc), "shadow": True, "workflow": workflow})



def jev_ingest_handler(args: dict[str, Any], **_: Any) -> str:
    source = args.get("source")
    event_type = args.get("event_type")
    payload = args.get("payload")
    if not isinstance(source, str) or not isinstance(event_type, str) or not isinstance(payload, dict):
        return json.dumps({"error": "source, event_type, and payload are required"})
    return json.dumps({"success": True, **ingest_event(source, event_type, payload)})


def jev_gateway_handler(args: dict[str, Any], **_: Any) -> str:
    action = args.get("action")
    state = args.get("state") or {}
    if not isinstance(state, dict):
        return json.dumps({"error": "state must be an object"})
    if action == "decide":
        return json.dumps({"success": True, **gateway_decide(state)})
    if action == "verify":
        return json.dumps({"success": True, **gateway_verify(state)})
    if action == "classify":
        if not isinstance(args.get("domain"), str):
            return json.dumps({"error": "classify requires domain"})
        return json.dumps({"success": True, **gateway_classify(args["domain"], state)})
    if action == "snapshot":
        return json.dumps({"success": True, **gateway_snapshot()})
    return json.dumps({"error": "unknown gateway action"})


def jev_ledger_handler(args: dict[str, Any], **_: Any) -> str:
    action = args.get("action")
    if action == "list":
        return json.dumps({"success": True, "entries": read_ledger(200)})
    if action == "metrics":
        return json.dumps({"success": True, "metrics": ledger_metrics()})
    if action == "record_review":
        review_id = args.get("review_id") or append_ledger("review", {"workflow": args.get("status", "manual"), "details": args.get("details", {})})
        return json.dumps({"success": True, "review_id": review_id})
    if action == "record_outcome":
        if not isinstance(args.get("review_id"), str) or not isinstance(args.get("correct"), bool):
            return json.dumps({"error": "record_outcome requires review_id and correct"})
        entry_id = append_ledger("outcome", {"review_id": args["review_id"], "correct": args["correct"], "details": args.get("details", {})})
        return json.dumps({"success": True, "entry_id": entry_id})
    if action in {"add_commitment", "add_decision"}:
        if not isinstance(args.get("text"), str) or not args["text"].strip():
            return json.dumps({"error": f"{action} requires non-empty text"})
        entry_id = append_ledger(action.removeprefix("add_"), {"text": args["text"][:4000], "owner": args.get("owner"), "deadline": args.get("deadline"), "status": args.get("status", "open"), "details": args.get("details", {})})
        return json.dumps({"success": True, "entry_id": entry_id})
    if action == "close":
        if not isinstance(args.get("review_id"), str):
            return json.dumps({"error": "close requires review_id"})
        entry_id = append_ledger("close", {"review_id": args["review_id"], "status": args.get("status", "closed"), "details": args.get("details", {})})
        return json.dumps({"success": True, "entry_id": entry_id})
    return json.dumps({"error": "unknown ledger action"})


def _redact_for_review(value: str, limit: int = 12000) -> str:
    value = value[:limit]
    patterns = [
        (r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]"),
        (r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def _write_shadow_record(record: dict[str, Any]) -> None:
    path = Path(get_hermes_home()) / "logs" / "jev-shadow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")


def _on_post_llm_call(
    assistant_response: str = "",
    user_message: str = "",
    conversation_history: Any = None,
    turn_id: str = "",
    session_id: str = "",
    **_: Any,
) -> None:
    """Review final answers in shadow mode without changing or blocking them."""
    if not _hooks_enabled():
        return None
    turn_key = _supervise_begin(turn_id, session_id, user_message)
    admission = None
    if turn_key:
        turn = _supervision.default_supervision().current_turn(turn_id=turn_key)
        if turn is not None:
            admission = turn.admission
    if not assistant_response:
        return None
    request = _redact_for_review(user_message, 4000)
    draft = _redact_for_review(assistant_response)
    history = _safe_text(conversation_history, 6000) if conversation_history else ""
    record: dict[str, Any] = {
        "event": "jev_output_review",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "draft_sha256": hashlib.sha256(assistant_response.encode("utf-8")).hexdigest(),
        "draft_chars": len(assistant_response),
        "request_chars": len(user_message),
        "admission": admission,
    }
    try:
        result = _request(
            {"model": _MODEL, "state": {"request": request, "draft": draft, "conversation_history": history}, "questions": _WORKFLOW_QUESTIONS["output_review"]},
            _secret(),
        )
        record.update({"success": True, "model": result.get("model"), "answers": result.get("answers"), "usage": result.get("usage"), **_routing(result)})
        record["review_id"] = append_ledger("review", {"workflow": "output_review", "answers": result.get("answers")})
    except Exception as exc:
        record.update({"success": False, "error_type": type(exc).__name__})
    try:
        _write_shadow_record(record)
    except Exception:
        return None
    return None


def _safe_text(value: Any, limit: int = 12000) -> str:
    if isinstance(value, str):
        return _redact_for_review(value, limit)
    try:
        return _redact_for_review(json.dumps(value, ensure_ascii=True, default=str), limit)
    except Exception:
        return _redact_for_review(str(value), limit)


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, invocation_id: str | None = None, turn_id: str = "", session_id: str = "", **_: Any) -> None:
    """Verify tool results in shadow mode without affecting tool execution."""
    if not _hooks_enabled():
        return None
    if not tool_name:
        return None
    result_text = _safe_text(result)
    verification_source = "unknown"
    lowered_tool = tool_name.lower()
    for candidate in ("paperless", "hindsight", "home_assistant", "docker", "nas"):
        if candidate in lowered_tool:
            verification_source = candidate
            break
    verification = verify_observation(verification_source, args if isinstance(args, dict) else {}, result)
    turn_key = _supervise_begin(turn_id, session_id, f"{tool_name} {_safe_text(args, 200)}")
    outcome: dict[str, Any] = {}
    if turn_key:
        outcome = _supervision.default_supervision().record_tool_outcome(
            tool_name=tool_name, args=args, result=result, turn_id=turn_key,
        )
    result_metadata = {
        "result_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
        "result_chars": len(result_text),
        "verified": verification["verified"],
    }
    case_id = update_tool_result(tool_name, result_metadata, verified=verification["verified"], invocation_id=invocation_id)
    if case_id:
        try:
            loop_record_observation(case_id, "tool result observed; content omitted", f"tool:{tool_name}", None)
            loop_record_outcome(case_id, "verified" if verification["verified"] else "awaiting_verification", True if verification["verified"] else None, result_metadata, verification["next"])
        except Exception:
            pass
    state = {
        "tool_name": tool_name,
        "arguments": _safe_text(args, 5000),
        "result": result_text,
    }
    record: dict[str, Any] = {
        "event": "jev_tool_result_verify",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "case_id": case_id,
        "result_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
        "result_chars": len(result_text),
    }
    if outcome.get("outcome") == "failure" and outcome.get("assess_remotely") is False:
        # An equivalent failure already assessed in this turn is deduplicated
        # locally, so a failing loop stops generating provider calls.
        record.update({
            "success": False,
            "skipped": "equivalent_failure_deduplicated",
            "failure_count": outcome.get("count"),
            "action_fingerprint": outcome.get("action_fingerprint"),
        })
        try:
            _write_shadow_record(record)
        except Exception:
            return None
        return None
    try:
        response = _request(
            {"model": _MODEL, "state": state, "questions": _WORKFLOW_QUESTIONS["tool_result_verify"]},
            _secret(),
        )
        record.update({"success": True, "model": response.get("model"), "answers": response.get("answers"), "usage": response.get("usage")})
        record["review_id"] = append_ledger("review", {"workflow": "tool_result_verify", "tool_name": tool_name, "answers": response.get("answers")})
    except Exception as exc:
        record.update({"success": False, "error_type": type(exc).__name__})
    try:
        _write_shadow_record(record)
    except Exception:
        return None
    return None


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    invocation_id: str | None = None,
    turn_id: str = "",
    session_id: str = "",
    **_: Any,
) -> Any:
    """Classify prospective tool risk before execution.

    The local control lease is consulted first, so a known repeated failure never
    pays a provider round trip. The only supported veto shape is
    ``{"action": "block", "message": ...}`` (see
    ``hermes_cli/plugins.py::_get_pre_tool_call_directive_details``), and it is
    returned only in an enforcing supervision mode. Shadow mode, the default,
    always returns ``None`` and cannot change tool execution.
    """
    if not _hooks_enabled():
        return None
    if not tool_name:
        return None
    # A known mistake first: a lesson carries the specific rule, so it is checked
    # before the generic repeated-failure control. Both are local and free.
    directive = _lesson_gate(tool_name, args, _supervision.default_supervision().enforcing())
    if directive is not None:
        return directive
    turn_key = _supervise_begin(turn_id, session_id, f"{tool_name} {_safe_text(args, 300)}")
    if turn_key:
        control = _supervision.default_supervision().check_control(
            tool_name=tool_name, args=args, turn_id=turn_key,
        )
        if control.get("controlled") and not control.get("allow"):
            return {
                "action": "block",
                "message": (
                    f"Jev supervision blocked this exact repeated action ({control.get('control')}): "
                    f"{control.get('reason')}. Take a materially different action, or permit one "
                    "retry with jev_supervision action=allow_retry."
                ),
            }
    case_id = None
    try:
        arguments = _safe_text(args, 5000)
        metadata = {"tool_name": tool_name, "invocation_id": invocation_id, "arguments_sha256": hashlib.sha256(arguments.encode()).hexdigest(), "arguments_chars": len(arguments)}
        ingested = ingest_event("hermes", "tool_call", metadata)
        case_id = ingested.get("case_id")
        if case_id:
            loop_record_decision("Should this tool call proceed?", "review_pending", ["allow", "ask", "deny"], [metadata], ["Jev review is advisory; Hermes policy remains authoritative"], decision_id=case_id)
    except Exception:
        pass
    state = {"tool_name": tool_name, "arguments": _safe_text(args, 6000), "invocation_id": invocation_id}
    record: dict[str, Any] = {
        "event": "jev_command_review",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
    }
    try:
        response = _request(
            {"model": _MODEL, "state": state, "questions": _WORKFLOW_QUESTIONS["command_review"]},
            _secret(),
        )
        record.update({"success": True, "model": response.get("model"), "answers": response.get("answers"), "usage": response.get("usage")})
        record["review_id"] = append_ledger("review", {"workflow": "command_review", "tool_name": tool_name, "answers": response.get("answers")})
    except Exception as exc:
        record.update({"success": False, "error_type": type(exc).__name__})
    try:
        _write_shadow_record(record)
    except Exception:
        return None
    return None


async def _on_platform_event(event: Any, source: Any = None) -> None:
    """Record gateway-normalized platform event metadata when opted in."""
    if not _hooks_enabled():
        return
    try:
        platform = getattr(source, "platform", None) if source is not None else None
        name = getattr(platform, "value", str(platform or "gateway"))
        content = _safe_text(event, 4000)
        ingest_event(name, "platform_event", {"content_sha256": hashlib.sha256(content.encode()).hexdigest(), "content_chars": len(content)})
    except Exception:
        return


def _wire_platform_event_handler(native: Any, adapter: Any) -> None:
    setter = getattr(adapter, "set_platform_event_handler", None)
    if setter is not None:
        setter(_on_platform_event)

JEV_LOOP_SCHEMA = {
    "name": "jev_loop",
    "description": "Record and assess closed loop decisions, observations, and outcomes.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["record_decision", "record_observation", "record_outcome", "label_outcome", "reopen", "verify_observation", "assess", "list"]},
            "decision_id": {"type": "string"},
            "question": {"type": "string"},
            "chosen": {"type": "string"},
            "options": {"type": "array"},
            "evidence": {},
            "result": {},
            "expected": {},
            "expected_state": {},
            "expected_status": {},
            "assumptions": {"type": "array"},
            "owner": {"type": "string"},
            "deadline": {"type": "string"},
            "observation": {"type": "string"},
            "source": {"type": "string"},
            "supports": {"type": "boolean"},
            "status": {"type": "string"},
            "success": {"type": "boolean"},
            "notes": {"type": "string"},
            "labeler": {"type": "string"},
            "reason": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def jev_loop_handler(args: dict[str, Any], **_: Any) -> str:
    action = args.get("action")
    try:
        if action == "record_decision":
            result = loop_record_decision(args.get("question", ""), args.get("chosen", ""), args.get("options"), args.get("evidence"), args.get("assumptions"), args.get("owner", "user"), args.get("deadline"))
        elif action == "record_observation":
            result = loop_record_observation(args["decision_id"], args.get("observation", ""), args.get("source", "unknown"), args.get("supports"))
        elif action == "record_outcome":
            raw_success = args.get("success")
            if raw_success is not None and not isinstance(raw_success, bool):
                raise ValueError("success must be boolean or omitted")
            result = loop_record_outcome(args["decision_id"], args.get("status", "unknown"), raw_success, args.get("evidence"), args.get("notes", ""))
        elif action == "verify_observation":
            result = verify_observation(args.get("source", "unknown"), args, args.get("result"))
        elif action == "label_outcome":
            if not isinstance(args.get("success"), bool):
                raise ValueError("label_outcome requires boolean success")
            result = loop_label_outcome(args["decision_id"], args["success"], args.get("evidence"), args.get("labeler", "user"), args.get("notes", ""))
        elif action == "reopen":
            result = loop_reopen(args["decision_id"], args.get("reason", "new contradictory evidence"), args.get("evidence"))
        elif action == "assess":
            result = loop_assess(args["decision_id"])
        elif action == "list":
            result = {"records": loop_list(args.get("limit", 100))}
        else:
            return json.dumps({"error": "unknown action"})
        return json.dumps({"success": True, **result}, sort_keys=True, default=str)
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


JEV_SUPERVISION_SCHEMA = {
    "name": "jev_supervision",
    "description": (
        "Inspect or drive the local Jev supervision layer: turn admission, adaptive "
        "event routing, challenge freshness, and repeated failure controls. Local "
        "telemetry needs no provider call; deterministic authority is unchanged."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status", "begin_turn", "end_turn", "observe_event",
                    "consider_challenge", "take_challenge", "check_control",
                    "record_tool_outcome", "allow_retry", "configure",
                ],
                "description": "Which supervision operation to run.",
            },
            "turn_id": {"type": "string", "description": "Supervised turn. Defaults to the session turn."},
            "session_id": {"type": "string"},
            "user_message": {"type": "string", "description": "Turn text used for local admission."},
            "event": {"type": "object", "description": "Structured event for observe_event."},
            "tool_name": {"type": "string"},
            "args": {"description": "Tool arguments, used for action fingerprints."},
            "result": {"description": "Tool result, used for the failure signature."},
            "hermes_decision": {"type": "string"},
            "jev_decision": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "decision_id": {"type": "string"},
            "state_version": {"type": "string"},
            "decision_version": {"type": "string"},
            "probabilities": {"type": "object"},
            "mode": {"type": "string", "enum": list(_supervision.SUPERVISION_MODES)},
            "include_recent": {"type": "boolean"},
            # The remaining fields are the settable ones. They were missing here
            # while the `configure` handler accepted all seven, and this schema
            # sets additionalProperties False, so a caller crossing the tool
            # boundary could change `mode` and nothing else. Each entry mirrors
            # the type the handler already accepted, so runtime behaviour is
            # unchanged and only the tool boundary stops rejecting valid input.
            #
            # `enabled` is deliberately absent. It decided whether supervision
            # enforces at all, and `jev_supervision` is called by the agent
            # being supervised, so accepting it here let the gated party switch
            # its own gate off. The handler refuses it as well; leaving it out
            # of the schema means this boundary rejects it first. `mode` stays
            # because the read-only actions above take it, and the handler
            # refuses it for `configure` specifically.
            "admission_enabled": {
                "type": "boolean",
                "description": "Classify each turn locally. Never blocks on its own.",
            },
            "relevance_threshold": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "How relevant an event must be before a remote opinion is warranted. Clamped to 0.0 through 1.0.",
            },
            "challenge_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence a disagreement needs before it becomes a challenge. Clamped to 0.0 through 1.0.",
            },
            "max_provider_calls_per_turn": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "Cap on provider calls per supervised turn. Clamped to 1 through 1000.",
            },
            "repeated_failure_replan_at": {
                "type": "integer",
                "minimum": 2,
                "maximum": 20,
                "description": "Identical failures needed before a control is created. Clamped to 2 through 20.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

# Every field `configure` accepts, in one place. The handler and this schema are
# both derived from it so a field can never be settable through one and refused
# through the other again. `tests/test_supervision_integration.py` asserts the
# two stay in step.
#
# `enabled` and `mode` are deliberately NOT here. They decide whether
# supervision enforces at all, and `jev_supervision` is a tool the supervised
# agent itself calls, so exposing them let the gated party disarm its own gate:
# one call with `mode=shadow` and the next blocked action runs. Operator code
# and the test suite still set them by calling `Supervision.configure()`
# directly, where the caller is the process that owns the policy rather than
# the agent it polices.
JEV_SUPERVISION_SETTABLE = (
    "admission_enabled",
    "relevance_threshold",
    "challenge_confidence",
    "max_provider_calls_per_turn",
    "repeated_failure_replan_at",
)

# What the agent is told when it tries to reach the enforcement switch anyway.
# Silent filtering would look like success; an explicit refusal is auditable.
JEV_SUPERVISION_ENFORCEMENT_FIELDS = ("enabled", "mode")


def _supervision_turn_key(turn_id: str, session_id: str) -> str:
    """Resolve a stable turn key so supervision state never fragments per call."""
    if turn_id:
        return turn_id
    if session_id:
        return f"session:{session_id}"
    return "session:default"


def _supervise_begin(turn_id: str, session_id: str, text: str = "") -> str | None:
    """Lazily ensure a supervised turn exists. Returns the resolved turn key."""
    if not _hooks_enabled():
        return None
    supervision = _supervision.default_supervision()
    if not supervision.config.enabled:
        return None
    key = _supervision_turn_key(turn_id, session_id)
    supervision.begin_turn(turn_id=key, session_id=session_id or "", user_message=text)
    return key


def _supervision_config_view(config: Any) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "admission_enabled": config.admission_enabled,
        "relevance_threshold": config.relevance_threshold,
        "challenge_confidence": config.challenge_confidence,
        "max_provider_calls_per_turn": config.max_provider_calls_per_turn,
        "repeated_failure_replan_at": config.repeated_failure_replan_at,
    }


def _supervision_delivery(args: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Attach a still-current challenge to a model-visible workflow response.

    A stale challenge is never delivered here; the supervision layer retains it as
    local telemetry instead. This function reports only and changes nothing.
    """
    supervision = _supervision.default_supervision()
    if not supervision.config.enabled:
        return {"enabled": False}
    turn_id = str(args.get("turn_id") or kwargs.get("turn_id") or "")
    session_id = str(args.get("session_id") or kwargs.get("session_id") or "")
    if not turn_id and not session_id:
        turn_id = supervision.latest_turn_id()
    challenge = None
    if turn_id or session_id:
        challenge = supervision.take_challenge(turn_id=turn_id, session_id=session_id)
    return {
        "enabled": True,
        "mode": supervision.mode(),
        "turn_id": turn_id,
        "challenge": challenge,
    }


def jev_supervision_handler(args: dict[str, Any], **_: Any) -> str:
    """Local supervision control. Never executes an action and never grants authority."""
    action = args.get("action")
    supervision = _supervision.default_supervision()
    turn_id = str(args.get("turn_id") or "")
    session_id = str(args.get("session_id") or "")
    try:
        if action == "status":
            report = supervision.status(
                turn_id=turn_id,
                session_id=session_id,
                include_recent=bool(args.get("include_recent")),
            )
            return json.dumps({"success": True, **report}, sort_keys=True, default=str)
        if action == "begin_turn":
            result = supervision.begin_turn(
                turn_id=turn_id,
                session_id=session_id,
                user_message=str(args.get("user_message") or ""),
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "end_turn":
            return json.dumps({"success": True, **supervision.end_turn(turn_id=turn_id, session_id=session_id)}, sort_keys=True, default=str)
        if action == "observe_event":
            event = args.get("event")
            if not isinstance(event, dict):
                raise ValueError("observe_event requires an event object")
            result = supervision.observe_event(event, turn_id=turn_id, session_id=session_id)
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "consider_challenge":
            for key in ("hermes_decision", "jev_decision"):
                if not isinstance(args.get(key), str) or not args[key].strip():
                    raise ValueError(f"consider_challenge requires {key}")
            result = supervision.consider_challenge(
                hermes_decision=args["hermes_decision"],
                jev_decision=args["jev_decision"],
                confidence=args.get("confidence", 0.0),
                reason=str(args.get("reason") or ""),
                decision_id=str(args.get("decision_id") or ""),
                state_version=str(args.get("state_version") or ""),
                decision_version=str(args.get("decision_version") or ""),
                probabilities=args.get("probabilities") if isinstance(args.get("probabilities"), dict) else None,
                turn_id=turn_id,
                session_id=session_id,
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "take_challenge":
            challenge = supervision.take_challenge(turn_id=turn_id, session_id=session_id)
            return json.dumps(
                {"success": True, "challenge": challenge, "delivered": challenge is not None},
                sort_keys=True, default=str,
            )
        if action == "check_control":
            if not isinstance(args.get("tool_name"), str):
                raise ValueError("check_control requires tool_name")
            result = supervision.check_control(
                tool_name=args["tool_name"], args=args.get("args"),
                turn_id=turn_id, session_id=session_id,
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "record_tool_outcome":
            if not isinstance(args.get("tool_name"), str):
                raise ValueError("record_tool_outcome requires tool_name")
            result = supervision.record_tool_outcome(
                tool_name=args["tool_name"], args=args.get("args"), result=args.get("result"),
                turn_id=turn_id, session_id=session_id,
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "allow_retry":
            result = supervision.allow_retry(
                turn_id=turn_id, session_id=session_id,
                reason=str(args.get("reason") or "operator_retry"),
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "configure":
            refused = sorted(set(args) & set(JEV_SUPERVISION_ENFORCEMENT_FIELDS))
            if refused:
                raise ValueError(
                    "enforcement cannot be changed from the supervised tool "
                    f"boundary ({', '.join(refused)} is operator-only)"
                )
            changes = {key: value for key, value in args.items() if key in JEV_SUPERVISION_SETTABLE}
            if not changes:
                raise ValueError("configure requires at least one settable field")
            config = supervision.configure(**changes)
            return json.dumps({"success": True, "config": _supervision_config_view(config)}, sort_keys=True, default=str)
        return json.dumps({"error": "unknown action"})
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


JEV_LESSONS_SCHEMA = {
    "name": "jev_lessons",
    "description": (
        "Record and manage learned corrections. A lesson is a written rule plus a "
        "precise description of the mistake as it is about to happen. Severity "
        "escalates from nudge to kick once the same mistake escapes twice, and a "
        "lesson that is judged relevant many times without ever catching anything "
        "retires as noise. Local only: no provider call, and no action is executed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add", "list", "get", "edit", "retire", "sweep",
                    "candidates", "caught", "surfaced", "export", "import", "stats",
                ],
                "description": "Which lesson operation to run.",
            },
            "text": {"type": "string", "description": "The rule, written as the right way to do it."},
            "detect": {"type": "string", "description": "The mistake as an action about to happen. Be precise."},
            "severity": {"type": "string", "enum": list(_lessons.SEVERITIES)},
            "source": {"type": "string", "description": "Use owner for a hard rule that must never retire."},
            "scope": {"type": "string"},
            "evidence": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": list(_lessons.KINDS)},
            "lesson_id": {"type": "string"},
            "lesson_ids": {"type": "array", "items": {"type": "string"}},
            "action_text": {"type": "string", "description": "The action to score lessons against."},
            "escapes": {"type": "integer"},
            "catches": {"type": "integer"},
            "reason": {"type": "string"},
            "min_catches": {"type": "integer"},
            "from": {"type": "string"},
            "pack": {"type": "object", "description": "A lesson pack to import."},
            "include_retired": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _lesson_action_text(tool_name: str, args: Any, limit: int = 600) -> str:
    """The text lessons are scored against: the tool and what it was given."""
    return f"{tool_name} {_safe_text(args, limit)}".strip()


# A pre_tool_call callback is a POLICY hook in Hermes: an exception or a timeout
# raised there is resolved as a BLOCK on the user's tool call, not as "no opinion".
# A lesson guard that cannot reach a decision must therefore abstain, never vetoing
# the tool. These counters keep such a failure visible instead of letting it look
# like a quietly working guard.
_LESSON_GATE_ERRORS: dict[str, Any] = {"count": 0, "last": ""}


def _lesson_gate(tool_name: str, args: Any, enforcing: bool) -> dict[str, Any] | None:
    """Stop an action a kick lesson matches, or note that it would have stopped one.

    Nothing in here may raise. See the note above on why an escaping exception would
    block the tool rather than pass it.
    """
    try:
        return _lesson_gate_decision(tool_name, args, enforcing)
    except Exception as exc:
        _LESSON_GATE_ERRORS["count"] += 1
        _LESSON_GATE_ERRORS["last"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return None


def _lesson_gate_decision(tool_name: str, args: Any, enforcing: bool) -> dict[str, Any] | None:
    """The lesson decision itself.

    A kick lesson is a known mistake, either written by the owner or escalated
    after it escaped twice. This is local and provider free, and it fires only on
    a close textual match, so it will miss a paraphrase. In a mode that does not
    enforce, nothing is stopped and nothing is counted as caught; the match is
    recorded as evidence instead.
    """
    store = _lessons.default_store()
    hits = store.local_kicks(_lesson_action_text(tool_name, args))
    if not hits:
        return None
    lesson, score = hits[0]
    if not enforcing:
        store.note_would_kick(lesson.id, tool_name=tool_name, score=score)
        return None
    store.record_surfaced([lesson.id])
    store.record_caught([lesson.id])
    return {
        "action": "block",
        "message": (
            f"A Jev lesson blocked this action (lesson {lesson.id}, match {score:.2f}). "
            f"{lesson.text} Take a different approach, or record why this is not that mistake "
            "with jev_lessons action=edit."
        ),
    }


def jev_lessons_handler(args: dict[str, Any], **_: Any) -> str:
    """Local lesson management. Never executes an action and never calls a provider."""
    action = args.get("action")
    store = _lessons.default_store()
    try:
        if action == "add":
            if not isinstance(args.get("text"), str):
                raise ValueError("add requires text")
            result = store.add(
                text=args["text"],
                detect=str(args.get("detect") or ""),
                severity=str(args.get("severity") or "nudge"),
                source=str(args.get("source") or "stated"),
                scope=str(args.get("scope") or "global"),
                evidence=str(args.get("evidence") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
                kind=str(args.get("kind") or "lesson"),
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "list":
            limit = max(1, min(200, int(args.get("limit") or 50)))
            rows = store.all(include_retired=bool(args.get("include_retired")))[:limit]
            return json.dumps({"success": True, "lessons": [r.as_dict() for r in rows], "count": len(rows)}, sort_keys=True, default=str)
        if action == "get":
            item = store.get(str(args.get("lesson_id") or ""))
            return json.dumps({"success": True, "lesson": item.as_dict() if item else None}, sort_keys=True, default=str)
        if action == "edit":
            result = store.edit(
                str(args.get("lesson_id") or ""),
                severity=args.get("severity"),
                escapes=args.get("escapes"),
                catches=args.get("catches"),
                detect=args.get("detect"),
                text=args.get("text"),
                scope=args.get("scope"),
            )
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "retire":
            result = store.retire(str(args.get("lesson_id") or ""), reason=str(args.get("reason") or "retired by hand"))
            return json.dumps({"success": True, **result}, sort_keys=True, default=str)
        if action == "sweep":
            gone = store.sweep()
            return json.dumps({"success": True, "retired": gone, "count": len(gone), "stats": store.stats()}, sort_keys=True, default=str)
        if action == "candidates":
            text = str(args.get("action_text") or "")
            if not text:
                raise ValueError("candidates requires action_text")
            limit = max(1, min(32, int(args.get("limit") or 8)))
            scored = store.candidates(text, limit=limit)
            return json.dumps({
                "success": True,
                "candidates": [{"lesson": item.as_dict(), "score": round(score, 4)} for item, score in scored],
                "count": len(scored),
                "note": "a shortlist for a semantic judgement, not a verdict",
            }, sort_keys=True, default=str)
        if action == "caught":
            ids = args.get("lesson_ids")
            if not isinstance(ids, list):
                raise ValueError("caught requires lesson_ids")
            return json.dumps({"success": True, "catches": store.record_caught([str(i) for i in ids])}, sort_keys=True, default=str)
        if action == "surfaced":
            ids = args.get("lesson_ids")
            if not isinstance(ids, list):
                raise ValueError("surfaced requires lesson_ids")
            return json.dumps({"success": True, "surfaced": store.record_surfaced([str(i) for i in ids])}, sort_keys=True, default=str)
        if action == "export":
            pack = store.export_pack(source=str(args.get("from") or ""), min_catches=int(args.get("min_catches") or 1))
            return json.dumps({"success": True, "pack": pack, "count": len(pack["lessons"])}, sort_keys=True, default=str)
        if action == "import":
            pack = args.get("pack")
            if not isinstance(pack, dict):
                raise ValueError("import requires a pack object")
            return json.dumps({"success": True, **store.import_pack(pack)}, sort_keys=True, default=str)
        if action == "stats":
            return json.dumps({
                "success": True,
                "stats": store.stats(),
                "gate": dict(_LESSON_GATE_ERRORS),
            }, sort_keys=True, default=str)
        return json.dumps({"error": "unknown action"})
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def register(ctx: Any) -> None:
    ctx.register_tool("jev_decide", _TOOLSET, JEV_DECIDE_SCHEMA, jev_decide_handler, description=JEV_DECIDE_SCHEMA["description"])
    ctx.register_tool("jev_workflow", _TOOLSET, _WORKFLOW_SCHEMA, jev_workflow_handler, description=_WORKFLOW_SCHEMA["description"])
    ctx.register_tool("jev_ledger", _TOOLSET, JEV_LEDGER_SCHEMA, jev_ledger_handler, description=JEV_LEDGER_SCHEMA["description"])
    ctx.register_tool("jev_gateway", _TOOLSET, JEV_GATEWAY_SCHEMA, jev_gateway_handler, description=JEV_GATEWAY_SCHEMA["description"])
    ctx.register_tool("jev_ingest", _TOOLSET, JEV_INGEST_SCHEMA, jev_ingest_handler, description=JEV_INGEST_SCHEMA["description"])
    ctx.register_tool("jev_loop", _TOOLSET, JEV_LOOP_SCHEMA, jev_loop_handler, description=JEV_LOOP_SCHEMA["description"])
    ctx.register_tool("jev_supervision", _TOOLSET, JEV_SUPERVISION_SCHEMA, jev_supervision_handler, description=JEV_SUPERVISION_SCHEMA["description"])
    ctx.register_tool("jev_lessons", _TOOLSET, JEV_LESSONS_SCHEMA, jev_lessons_handler, description=JEV_LESSONS_SCHEMA["description"])
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    register_platform_handler = getattr(ctx, "register_platform_handler", None)
    if register_platform_handler is not None:
        for platform in ("homeassistant", "email", "telegram", "discord", "matrix", "dingtalk"):
            register_platform_handler(platform, _wire_platform_event_handler)

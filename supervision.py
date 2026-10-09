"""Asynchronous Jev supervision for Hermes.

Design adapted from the community project ``keeltrace/hermes-jev`` (MIT), by the
KeelTrace community, at revision 4feea5ef45aeb301622f18175ed4cf2e068b99bd
(v0.2.1.1). Upstream wording quoted verbatim where the behavior is adopted:

  "Hermes remains the reasoning and execution engine. Jev supervises
  accountable decisions in parallel..."
      -- KeelTrace community, hermes-jev README.md line 5, v0.2.1.1

  "The recommended path is the nervous system, not synchronous
  evaluate-every-tool gating."
      -- KeelTrace community, hermes-jev README.md line 23, v0.2.1.1

  "Late opinions remain receipts, not commands."
      -- KeelTrace community, hermes-jev docs/NERVOUS_SYSTEM.md line 74, v0.2.1.1

This module adapts four upstream ideas to this plugin's boundaries:

1. Turn admission (``OFF`` / ``WATCH`` / ``ON``) that never blocks Hermes.
2. A local relevance router that decides whether a remote Jev call is worth
   making, with suppression, hysteresis, and bounded batching.
3. Challenge delivery that is only valid while the challenged state is current.
4. Local action and failure fingerprints with a provider independent ``REPLAN``
   control lease.

Deliberate differences from upstream, so the authority boundary stays intact:

- Admission is a local deterministic classifier, not a provider call. Upstream
  spends a remote assessment on admission; this plugin must never add provider
  latency to the ordinary turn path, so admission is free and local.
- Semantic judgement stays in the existing ``jev_workflow`` surface. The router
  decides *whether* to ask, never *what* the answer means.
- ``gateway.py`` remains the authority for approval and verification. A control
  lease here can only constrain a repeated identical action; it can never
  authorize anything.
- The upstream ``ContextEngine`` and automatic context deletion are out of scope
  for this release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .ledger import append as append_ledger
except ImportError:  # Standalone package use.
    from ledger import append as append_ledger

SUPERVISION_MODES = ("off", "shadow", "correct_next", "precommit")
ADMISSIONS = ("OFF", "WATCH", "ON")
ENFORCING_MODES = ("correct_next", "precommit")

# The keys a dict result uses to report failure, where a key that is present
# but empty is not a failure: the producer told us there is nothing to report.
#
# These are the same four keys, with the same rule, that `verification` uses to
# read tool results for verification. The two modules had drifted apart once
# already — `verification` knew `errors`, `timeout` and `timed_out`, and this
# one did not, which is how a timeout came to be recorded as a success — so
# `test_fail_closed_regressions.py` asserts the two sets stay equal. Keeping two
# definitions that must agree is the residual risk; pulling them into one
# module would trade it for an import between two independently useful layers.
_FAILURE_KEYS = frozenset({"error", "errors", "timeout", "timed_out"})

# How a collector reports its own failure, and the word a bare-string result
# carries when a call did not come back in time.
_COLLECTOR_PREFIX = "collector_error:"
_TIMEOUT_RE = re.compile(r"\btimed?\s*out\b|\btimeout\b")
BLOCKING_CONTROLS = ("REPLAN", "GATHER_EVIDENCE", "ESCALATE")

EVENT_KINDS = (
    "DECISION",
    "STRATEGY_CHANGE",
    "RECOVERY",
    "COMPLETION_CANDIDATE",
    "CONSEQUENTIAL_ACTION",
    "HUMAN_ESCALATION",
    "IRREVERSIBLE_ACTION",
    "FAILURE",
    "SUCCESS",
)

# Tokens that indicate a turn can reach an accountable decision plane. Kept
# deliberately small and auditable: a false OFF only means less supervision,
# never an unsafe action, because authority stays deterministic.
_RISK_TOKENS = (
    "delete", "destroy", "drop", "wipe", "overwrite", "deploy", "publish",
    "push", "send", "pay", "payment", "transfer", "credential", "password",
    "secret", "token", "restore", "rollback", "migrate", "production",
    "restart", "shutdown", "rm -rf", "chmod", "sudo", "grant", "revoke",
)
_PLAN_TOKENS = (
    "plan", "refactor", "change", "update", "fix", "build", "implement",
    "review", "verify", "check", "should", "decide", "choose", "compare",
    "research", "report", "draft", "write", "email", "document",
)
_READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "tool_search", "tool_describe",
    "web_search", "web_extract", "session_search",
    "jev_decide", "jev_workflow", "jev_ledger", "jev_gateway",
    "jev_ingest", "jev_loop", "jev_supervision",
})
_SIMPLE_READ_ONLY_COMMANDS = frozenset({
    "pwd", "ls", "cat", "head", "tail", "wc", "stat", "du", "df", "file",
    "which", "whereis", "whoami", "id", "uname", "uptime", "free", "ps",
    "grep", "rg", "jq",
})
_SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree",
    "describe", "grep", "blame",
})
_SHELL_META_RE = re.compile(r"(?:&&|\|\||[;|><`]|\$\()")
_WORD_RE = re.compile(r"[a-z0-9_'-]+")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def canonical_hash(value: Any) -> str:
    """Stable content hash for receipts and fingerprints."""
    try:
        body = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        body = str(value)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_arguments(args: Any) -> str:
    """Canonical form of tool arguments, so equivalent calls share a fingerprint."""
    if isinstance(args, dict):
        return json.dumps(args, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))


def fingerprint_action(tool_name: str, args: Any) -> str:
    """Stable identity for a tool action: tool name plus canonical arguments."""
    return canonical_hash({"tool": str(tool_name or ""), "arguments": _canonical_arguments(args)})


def fingerprint_failure(action_fingerprint: str, status: Any, error: Any) -> str:
    """Stable identity for a failure episode: action plus normalized signature."""
    signature = re.sub(r"\s+", " ", str(error or "")).strip().lower()[:400]
    return canonical_hash({
        "action": str(action_fingerprint or ""),
        "status": str(status if status is not None else "").strip().lower()[:120],
        "error": signature,
    })


def _succeeded(result: Any) -> bool:
    """True only on positive evidence that a tool call completed.

    This is deliberately narrower than "no failure signature was found". A
    result can carry no failure signal because it succeeded, or because its
    producer used a status shape this module does not read — a `timeout`
    envelope, an unrecognised wrapper, a value that says only that work is
    still `running`. Those are the cases where absence of evidence used to be
    read as evidence of success, and a blocked action ran.

    Three things count as positive, and only these:

    - an explicit affirmative marker (`success`, `ok`, an affirming `status`);
    - a result carrying no keys at all, which is the ordinary "the tool ran and
      had nothing to report" shape;
    - a bare string with content and no failure signal in it, whose whole
      content is the result.

    Everything else — a dict whose status cannot be read, a wrapper around a
    value this module does not recognise — is reported as unknown and consumes
    nothing.
    """
    if normalize_error(result):
        return False
    if result is None:
        return False
    if isinstance(result, dict):
        if result.get("success") is True or result.get("ok") is True:
            return True
        if result.get("success") is False or result.get("ok") is False:
            return False
        if isinstance(result.get("status"), str):
            return result["status"].strip().lower() in {
                "ok", "success", "succeeded", "done", "completed"
            }
        return not result
    if isinstance(result, str):
        return bool(result.strip())
    return True


def normalize_error(result: Any) -> str:
    """Extract a short failure signature from a tool result, or an empty string.

    A result counts as a failure only on an explicit failure signal. An ordinary
    successful payload never fabricates a failure signature.

    The key set and the "present but empty is not a failure" rule are shared
    with `verification`, which reads tool results the same way. The two
    implementations had drifted: `verification` knew `errors`, `timeout` and
    `timed_out`, and this one did not, so a result reporting a timeout carried
    no signature at all and was recorded as a success. That let a timeout
    consume an active control and release the action the control was blocking.
    """
    text = ""
    if isinstance(result, dict):
        for key in _FAILURE_KEYS:
            if key not in result:
                continue
            value = result[key]
            if value is None or value is False:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, tuple, dict, set)) and not value:
                continue
            text = str(value)[:400]
            break
        if not text and (result.get("success") is False or result.get("ok") is False):
            text = str(result.get("message") or "reported not successful")
    elif isinstance(result, str):
        lowered = result.strip().lower()
        if lowered.startswith(_COLLECTOR_PREFIX) or lowered.startswith("error") or "traceback (most recent call last)" in lowered:
            text = result
        elif _TIMEOUT_RE.search(lowered):
            text = result
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()[:400]


def is_read_only_call(tool_name: str, args: Any) -> bool:
    """True only for deliberately narrow, obviously read-only calls."""
    name = str(tool_name or "").strip()
    if name in _READ_ONLY_TOOLS or name.startswith("jev_"):
        return True
    if name not in {"terminal", "shell", "bash", "run_command"}:
        return False
    if not isinstance(args, dict):
        return False
    command = ""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            command = value.strip()
            break
    if not command or _SHELL_META_RE.search(command):
        return False
    parts = command.split()
    if not parts or "=" in parts[0] or "/" in parts[0]:
        return False
    if parts[0] in _SIMPLE_READ_ONLY_COMMANDS:
        return True
    if parts[0] == "git" and len(parts) >= 2:
        return parts[1] in _SAFE_GIT_SUBCOMMANDS
    return False


@dataclass
class SupervisionConfig:
    """Local configuration. Env driven, shadow first, no provider required."""

    enabled: bool = True
    mode: str = "shadow"
    admission_enabled: bool = True
    relevance_threshold: float = 0.50
    challenge_confidence: float = 0.86
    max_provider_calls_per_turn: int = 96
    repeated_failure_replan_at: int = 3
    retain_recent_events: int = 64
    max_tracked_turns: int = 16
    # How far past `max_tracked_turns` the map may grow while every tracked turn
    # still holds a blocking control. A `ControlDirective` carries no expiry and
    # a turn is only freed by `end_turn`, so without this the refusal to evict a
    # control-holding turn would be a leak in a process where turns never end.
    # This is the hard ceiling at which the memory bound wins over the control,
    # and the loss is recorded rather than dropped in silence. See
    # `_evict_oldest_turn`.
    control_retention_limit: int = 256

    @classmethod
    def from_env(cls) -> "SupervisionConfig":
        mode = os.environ.get("JEV_SUPERVISION_MODE", "shadow").strip().lower()
        return cls(
            enabled=_env_flag("JEV_SUPERVISION_ENABLED", True),
            mode=mode if mode in SUPERVISION_MODES else "shadow",
            admission_enabled=_env_flag("JEV_SUPERVISION_ADMISSION", True),
            relevance_threshold=_env_float("JEV_SUPERVISION_THRESHOLD", 0.50, 0.0, 1.0),
            challenge_confidence=_env_float("JEV_SUPERVISION_CHALLENGE_CONFIDENCE", 0.86, 0.0, 1.0),
            max_provider_calls_per_turn=_env_int("JEV_SUPERVISION_MAX_CALLS", 96, 1, 1000),
            repeated_failure_replan_at=_env_int("JEV_SUPERVISION_REPLAN_AT", 3, 2, 20),
            retain_recent_events=_env_int("JEV_SUPERVISION_RETAIN", 64, 8, 512),
            control_retention_limit=_env_int("JEV_SUPERVISION_CONTROL_RETENTION", 256, 1, 10000),
        )


@dataclass
class Challenge:
    """A still-current high confidence disagreement, or stale telemetry."""

    challenge_id: str
    turn_id: str
    decision_id: str
    hermes_decision: str
    jev_decision: str
    confidence: float
    reason: str
    state_version: str = ""
    decision_version: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    created_at: str = ""
    delivered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "turn_id": self.turn_id,
            "decision_id": self.decision_id,
            "hermes_decision": self.hermes_decision,
            "jev_decision": self.jev_decision,
            "confidence": round(float(self.confidence), 6),
            "reason": self.reason,
            "state_version": self.state_version,
            "decision_version": self.decision_version,
            "probabilities": self.probabilities,
            "created_at": self.created_at,
            "delivered": self.delivered,
        }


@dataclass
class ControlDirective:
    """A provider independent local control over one exact repeated action."""

    control: str
    turn_id: str
    action_fingerprint: str
    failure_fingerprint: str
    reason: str
    source: str = "local_loop_breaker"
    confidence: float = 1.0
    created_at: str = ""
    enforced: bool = False
    consumed: bool = False
    retries_allowed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "control": self.control,
            "turn_id": self.turn_id,
            "action_fingerprint": self.action_fingerprint,
            "failure_fingerprint": self.failure_fingerprint,
            "reason": self.reason,
            "source": self.source,
            "confidence": round(float(self.confidence), 6),
            "created_at": self.created_at,
            "enforced": self.enforced,
            "consumed": self.consumed,
            "retries_allowed": self.retries_allowed,
        }


@dataclass
class FailureEpisode:
    action_fingerprint: str
    failure_fingerprint: str
    count: int = 0
    provider_evaluations: int = 0
    last_status: str = ""
    last_error: str = ""


@dataclass
class TurnState:
    turn_id: str
    session_id: str = ""
    admission: str = "PENDING"
    admission_score: float = 0.0
    admission_reasons: list[str] = field(default_factory=list)
    message_hash: str = ""
    events_seen: int = 0
    events_suppressed: int = 0
    events_forwarded: int = 0
    events_batched: int = 0
    provider_calls: int = 0
    provider_errors: int = 0
    challenges_created: int = 0
    challenges_delivered: int = 0
    challenges_stale: int = 0
    controls_created: int = 0
    controls_enforced: int = 0
    assessment_inflight: bool = False
    last_forwarded_fingerprint: str = ""
    state_version: str = ""
    decision_version: str = ""
    pending_batch: deque = field(default_factory=deque)
    pending_challenges: deque = field(default_factory=deque)
    recent_events: deque = field(default_factory=deque)
    failure_episodes: dict[str, FailureEpisode] = field(default_factory=dict)
    active_control: ControlDirective | None = None


class Supervision:
    """Turn scoped supervision state. Never executes an action, never blocks Hermes."""

    def __init__(self, config: SupervisionConfig | None = None) -> None:
        self._lock = threading.RLock()
        self._config = config or SupervisionConfig.from_env()
        self._turns: dict[str, TurnState] = {}
        self._session_turn: dict[str, str] = {}
        self._metrics: Counter = Counter()
        self._recent_challenges: deque = deque(maxlen=30)
        self._recent_controls: deque = deque(maxlen=30)

    # -- configuration ---------------------------------------------------

    @property
    def config(self) -> SupervisionConfig:
        return self._config

    def configure(self, **kwargs: Any) -> SupervisionConfig:
        with self._lock:
            cfg = self._config
            if "enabled" in kwargs:
                cfg.enabled = bool(kwargs["enabled"])
            if "mode" in kwargs:
                mode = str(kwargs["mode"] or "").strip().lower()
                cfg.mode = mode if mode in SUPERVISION_MODES else "shadow"
            if "admission_enabled" in kwargs:
                cfg.admission_enabled = bool(kwargs["admission_enabled"])
            if "relevance_threshold" in kwargs:
                cfg.relevance_threshold = min(1.0, max(0.0, float(kwargs["relevance_threshold"])))
            if "challenge_confidence" in kwargs:
                cfg.challenge_confidence = min(1.0, max(0.0, float(kwargs["challenge_confidence"])))
            if "max_provider_calls_per_turn" in kwargs:
                cfg.max_provider_calls_per_turn = min(1000, max(1, int(kwargs["max_provider_calls_per_turn"])))
            if "repeated_failure_replan_at" in kwargs:
                cfg.repeated_failure_replan_at = min(20, max(2, int(kwargs["repeated_failure_replan_at"])))
            return cfg

    def mode(self) -> str:
        return self._config.mode

    def enforcing(self) -> bool:
        return self._config.enabled and self._config.mode in ENFORCING_MODES

    # -- turn lifecycle --------------------------------------------------

    def begin_turn(self, *, turn_id: str = "", session_id: str = "", user_message: str = "") -> dict[str, Any]:
        """Create or reuse a turn and run local admission. Never blocks."""
        if not self._config.enabled:
            return {"turn_id": turn_id or "", "admission": "OFF", "reason": "supervision_disabled"}
        turn_id = turn_id or uuid.uuid4().hex[:16]
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                turn = TurnState(
                    turn_id=turn_id,
                    session_id=session_id,
                    message_hash=canonical_hash(user_message)[:32] if user_message else "",
                    pending_batch=deque(maxlen=self._config.retain_recent_events),
                    pending_challenges=deque(),
                    recent_events=deque(maxlen=self._config.retain_recent_events),
                )
                self._turns[turn_id] = turn
                self._metrics["turns_started"] += 1
                if session_id:
                    self._session_turn[session_id] = turn_id
                if len(self._turns) > self._config.max_tracked_turns:
                    self._evict_oldest_turn(protected=turn_id)

            if self._config.admission_enabled and turn.admission == "PENDING":
                admission, score, reasons = classify_admission(user_message)
                turn.admission = admission
                turn.admission_score = score
                turn.admission_reasons = reasons
                self._metrics[f"admission_{admission.lower()}"] += 1
            return {
                "turn_id": turn.turn_id,
                "admission": turn.admission,
                "score": round(turn.admission_score, 4),
                "reasons": list(turn.admission_reasons),
                "mode": self._config.mode,
            }

    def _evict_oldest_turn(self, protected: str = "") -> None:
        """Drop the oldest turn, unless that turn is holding a control.

        Eviction used to take `next(iter(self._turns))` whatever it held, and
        the consequences were two. A turn that was still live lost its active
        control, and `check_control` then reported `allow: True` for the exact
        action that control was blocking, because a turn that cannot be resolved
        is indistinguishable from a turn with nothing to enforce. In `precommit`
        that is a fail-open of the one thing enforcement does, reachable by
        ordinary unrelated traffic.

        A blocking control is therefore never evicted. The oldest turn *without*
        one is dropped instead, and if every tracked turn holds a control the
        map grows past `max_tracked_turns` for as long as that stays true. That
        is deliberate: the bound exists to cap memory, and what it would
        otherwise cap is a safety decision. Controls are consumed or expire, so
        the overshoot is temporary, and it is counted so it cannot be silent.

        `protected` is the turn being started right now. Without it the newest
        turn is the first control-free one in insertion order, so it would be
        the victim every time, and no turn created after the cap was reached
        could ever accumulate a control at all.
        """
        victim = None
        for turn_id, candidate in self._turns.items():
            if turn_id == protected:
                continue
            if candidate.active_control is None or candidate.active_control.consumed:
                victim = turn_id
                break
        if victim is not None:
            self._drop_turn(victim)
            return

        # Every remaining turn holds a control. Hold the line up to the hard
        # ceiling: the bound exists to cap memory, and what it would otherwise
        # drop is a safety decision.
        if len(self._turns) <= self._config.control_retention_limit:
            self._metrics["turns_retained_past_cap"] += 1
            return

        # Past the ceiling the memory bound has to win, but the loss is no
        # longer silent: it is counted under its own metric and appended to the
        # ledger, so an operator can see that a blocking control was dropped
        # and why. The alternative was a process that never frees a turn.
        oldest = next(iter(self._turns))
        self._metrics["controls_evicted"] += 1
        append_ledger(
            "supervision",
            {
                "action": "control_evicted",
                "turn_id": oldest,
                "reason": "control_retention_limit",
                "retention_limit": self._config.control_retention_limit,
            },
        )
        self._drop_turn(oldest)

    def _drop_turn(self, turn_id: str) -> None:
        dropped = self._turns.pop(turn_id)
        if dropped.session_id:
            self._session_turn.pop(dropped.session_id, None)
        self._metrics["turns_evicted"] += 1

    def end_turn(self, *, turn_id: str = "", session_id: str = "") -> dict[str, Any]:
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return {"closed": False, "reason": "unknown_turn"}
        with self._lock:
            summary = self._turn_summary(turn)
            self._turns.pop(turn.turn_id, None)
            if turn.session_id:
                self._session_turn.pop(turn.session_id, None)
            self._metrics["turns_closed"] += 1
            return {"closed": True, **summary}

    def _resolve_turn(self, turn_id: str = "", session_id: str = "") -> TurnState | None:
        with self._lock:
            if turn_id and turn_id in self._turns:
                return self._turns[turn_id]
            if session_id:
                resolved = self._session_turn.get(session_id)
                if resolved and resolved in self._turns:
                    return self._turns[resolved]
            if not turn_id and not session_id and len(self._turns) == 1:
                return next(iter(self._turns.values()))
            return None

    def current_turn(self, *, turn_id: str = "", session_id: str = "") -> TurnState | None:
        return self._resolve_turn(turn_id, session_id)

    def latest_turn_id(self) -> str:
        """Most recently created supervised turn id, or an empty string."""
        with self._lock:
            if not self._turns:
                return ""
            return next(reversed(self._turns))

    # -- adaptive local router -------------------------------------------

    def observe_event(self, event: dict[str, Any], *, turn_id: str = "", session_id: str = "") -> dict[str, Any]:
        """Route one structured event. Returns a routing decision, never a provider call.

        The caller decides whether to spend a remote Jev call based on
        ``forward``. Routine and equivalent state is suppressed locally.
        """
        if not self._config.enabled:
            return {"routed": "disabled", "forward": False}
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return {"routed": "no_turn", "forward": False}
        event_type = str(event.get("type") or event.get("event_type") or "OBSERVATION").upper()
        bounded = _bounded_event(event, self._config.retain_recent_events)
        score, reasons = score_event(bounded)
        signature = canonical_hash({
            "type": event_type,
            "materiality": round(float(bounded.get("materiality") or 0.0), 2),
            "uncertainty": round(float(bounded.get("uncertainty") or 0.0), 2),
            "contradiction": round(float(bounded.get("contradiction") or 0.0), 2),
            "state_version": str(bounded.get("state_version") or ""),
        })
        with self._lock:
            turn.events_seen += 1
            turn.recent_events.append({
                "type": event_type,
                "score": round(score, 4),
                "signature": signature[:16],
                "timestamp": _now(),
            })
            self._metrics["events_seen"] += 1
            if turn.admission == "OFF":
                turn.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"routed": "suppressed", "forward": False, "reason": "admission_off", "score": round(score, 4)}
            if score < self._config.relevance_threshold:
                turn.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"routed": "suppressed", "forward": False, "reason": "below_threshold", "score": round(score, 4)}
            if signature == turn.last_forwarded_fingerprint:
                turn.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"routed": "suppressed", "forward": False, "reason": "hysteresis", "score": round(score, 4)}
            if turn.assessment_inflight:
                turn.events_batched += 1
                turn.pending_batch.append(bounded)
                self._metrics["events_batched"] += 1
                return {"routed": "batched", "forward": False, "reason": "assessment_inflight", "score": round(score, 4)}
            if turn.provider_calls >= self._config.max_provider_calls_per_turn:
                turn.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"routed": "suppressed", "forward": False, "reason": "budget_exhausted", "score": round(score, 4)}
            turn.events_forwarded += 1
            turn.last_forwarded_fingerprint = signature
            self._metrics["events_forwarded"] += 1
            return {
                "routed": "forwarded",
                "forward": True,
                "score": round(score, 4),
                "reasons": reasons,
                "admission": turn.admission,
            }

    def drain_batch(self, *, turn_id: str = "", session_id: str = "") -> list[dict[str, Any]]:
        """Return and clear events retained while an assessment was in flight."""
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return []
        with self._lock:
            items = list(turn.pending_batch)
            turn.pending_batch.clear()
            return items

    def record_provider_call(self, *, turn_id: str = "", session_id: str = "", failed: bool = False) -> None:
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return
        with self._lock:
            turn.provider_calls += 1
            self._metrics["provider_calls"] += 1
            if failed:
                turn.provider_errors += 1
                self._metrics["provider_errors"] += 1

    def mark_assessment(self, *, turn_id: str = "", session_id: str = "", inflight: bool = True) -> None:
        """Record whether a remote assessment is currently in flight."""
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return
        with self._lock:
            turn.assessment_inflight = bool(inflight)

    def set_state_version(self, *, turn_id: str = "", session_id: str = "", state_version: str = "", decision_version: str = "") -> None:
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return
        with self._lock:
            if state_version:
                turn.state_version = str(state_version)
            if decision_version:
                turn.decision_version = str(decision_version)

    # -- challenge lifecycle ---------------------------------------------

    def consider_challenge(
        self,
        *,
        hermes_decision: str,
        jev_decision: str,
        confidence: float,
        reason: str = "",
        decision_id: str = "",
        state_version: str = "",
        decision_version: str = "",
        probabilities: dict[str, float] | None = None,
        turn_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Record a disagreement. Only high confidence disagreement becomes a challenge."""
        turn = self._resolve_turn(turn_id, session_id)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        agrees = str(hermes_decision).strip().lower() == str(jev_decision).strip().lower()
        with self._lock:
            self._metrics["disagreements_seen"] += 1
            if agrees or confidence < self._config.challenge_confidence:
                self._metrics["disagreements_silent"] += 1
                return {"challenged": False, "reason": "agreement" if agrees else "below_confidence"}
            challenge = Challenge(
                challenge_id=uuid.uuid4().hex[:16],
                turn_id=turn.turn_id if turn else "",
                decision_id=decision_id or "",
                hermes_decision=str(hermes_decision),
                jev_decision=str(jev_decision),
                confidence=confidence,
                reason=str(reason or "")[:400],
                state_version=str(state_version or (turn.state_version if turn else "")),
                decision_version=str(decision_version or (turn.decision_version if turn else "")),
                probabilities=dict(probabilities or {}),
                created_at=_now(),
            )
            self._recent_challenges.append(challenge.as_dict())
            self._metrics["challenges_created"] += 1
            if turn is not None:
                turn.challenges_created += 1
                turn.pending_challenges.append(challenge)
            return {"challenged": True, **challenge.as_dict()}

    def take_challenge(self, *, turn_id: str = "", session_id: str = "") -> dict[str, Any] | None:
        """Return one still-current challenge for delivery, or None.

        A challenge whose state version no longer matches the live turn state is
        retained as telemetry and never delivered as current advice.
        """
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return None
        with self._lock:
            while turn.pending_challenges:
                challenge: Challenge = turn.pending_challenges.popleft()
                if self._is_current(turn, challenge):
                    challenge.delivered = True
                    turn.challenges_delivered += 1
                    self._metrics["challenges_delivered"] += 1
                    return challenge.as_dict()
                turn.challenges_stale += 1
                self._metrics["challenges_stale"] += 1
                append_ledger("challenge_stale", {
                    "challenge_id": challenge.challenge_id,
                    "turn_id": challenge.turn_id,
                    "decision_id": challenge.decision_id,
                    "state_version": challenge.state_version,
                    "live_state_version": turn.state_version,
                })
            return None

    @staticmethod
    def _is_current(turn: TurnState, challenge: Challenge) -> bool:
        if challenge.state_version and turn.state_version and challenge.state_version != turn.state_version:
            return False
        if challenge.decision_version and turn.decision_version and challenge.decision_version != turn.decision_version:
            return False
        return True

    # -- repeated failure control lease ----------------------------------

    def record_tool_outcome(
        self,
        *,
        tool_name: str,
        args: Any = None,
        result: Any = None,
        turn_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Fingerprint a tool outcome and raise a local REPLAN control on repetition."""
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None or not self._config.enabled:
            return {"recorded": False, "reason": "no_turn"}
        action_fp = fingerprint_action(tool_name, args)
        error = normalize_error(result)
        confirmed = _succeeded(result)
        with self._lock:
            if not error and confirmed:
                # A materially different successful action consumes an active control.
                if turn.active_control is not None and turn.active_control.action_fingerprint == action_fp:
                    turn.active_control.consumed = True
                    self._metrics["controls_consumed"] += 1
                turn.failure_episodes.pop(action_fp, None)
                return {"recorded": True, "outcome": "success", "action_fingerprint": action_fp}

            if not error and not confirmed:
                # Neither a failure nor a confirmed success. This is the shape
                # that used to be read as success, because "no error signature"
                # was the test. A timeout envelope, an unrecognised status
                # wrapper, or a result carrying no signal at all landed here,
                # and every one of them consumed the control that was blocking
                # a repeated failing action. Record it as unknown instead, and
                # leave the control and the failure episode exactly as they
                # were: the action is still unproven and still constrained.
                return {"recorded": True, "outcome": "unknown", "action_fingerprint": action_fp}

            failure_fp = fingerprint_failure(action_fp, result, error)
            episode = turn.failure_episodes.get(action_fp)
            if episode is None or episode.failure_fingerprint != failure_fp:
                episode = FailureEpisode(
                    action_fingerprint=action_fp,
                    failure_fingerprint=failure_fp,
                    last_status=str(result)[:120] if not isinstance(result, dict) else "",
                    last_error=error,
                )
                turn.failure_episodes[action_fp] = episode
            episode.count += 1
            self._metrics["failures_seen"] += 1
            if episode.count < self._config.repeated_failure_replan_at:
                if episode.count == 1:
                    episode.provider_evaluations += 1
                    return {
                        "recorded": True,
                        "outcome": "failure",
                        "count": episode.count,
                        "action_fingerprint": action_fp,
                        "failure_fingerprint": failure_fp,
                        "assess_remotely": True,
                    }
                self._metrics["failures_deduplicated"] += 1
                return {
                    "recorded": True,
                    "outcome": "failure",
                    "count": episode.count,
                    "action_fingerprint": action_fp,
                    "failure_fingerprint": failure_fp,
                    "assess_remotely": False,
                    "reason": "equivalent_failure_deduplicated",
                }
            control = ControlDirective(
                control="REPLAN",
                turn_id=turn.turn_id,
                action_fingerprint=action_fp,
                failure_fingerprint=failure_fp,
                reason=f"identical failure reached the local threshold ({episode.count})",
                created_at=_now(),
            )
            turn.active_control = control
            turn.controls_created += 1
            self._metrics["controls_created"] += 1
            self._recent_controls.append(control.as_dict())
            append_ledger("control", {
                "control": control.control,
                "turn_id": control.turn_id,
                "action_fingerprint": action_fp,
                "failure_fingerprint": failure_fp,
                "count": episode.count,
                "source": control.source,
            })
            return {
                "recorded": True,
                "outcome": "failure",
                "count": episode.count,
                "action_fingerprint": action_fp,
                "failure_fingerprint": failure_fp,
                "control": control.as_dict(),
                # The local control is authoritative for this action, so no remote
                # opinion is spent on the failure that created it.
                "assess_remotely": False,
                "reason": "local_control_created",
            }

    def check_control(
        self,
        *,
        tool_name: str,
        args: Any = None,
        turn_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Decide whether an exact repeated action may run again.

        In ``shadow`` the control is recorded and reported but never enforced, so
        Hermes behavior is unchanged. In ``correct_next`` and ``precommit`` the
        exact controlled action is blocked until the control is consumed.
        """
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None:
            return {
                "controlled": False,
                "allow": True,
                "mode": self._config.mode,
                "reason": "no_turn",
            }
        with self._lock:
            control = turn.active_control
            if control is None or control.consumed:
                return {"controlled": False, "allow": True, "mode": self._config.mode}
            action_fp = fingerprint_action(tool_name, args)
            if action_fp != control.action_fingerprint:
                return {"controlled": False, "allow": True, "mode": self._config.mode, "reason": "different_action"}
            if control.control == "RETRY":
                control.consumed = True
                self._metrics["controls_consumed"] += 1
                return {"controlled": True, "allow": True, "mode": self._config.mode, "control": "RETRY", "reason": "explicit_retry_consumed"}
            enforcing = self.enforcing()
            if enforcing:
                control.enforced = True
                turn.controls_enforced += 1
                self._metrics["controls_enforced"] += 1
            return {
                "controlled": True,
                "allow": not enforcing,
                "mode": self._config.mode,
                "control": control.control,
                "reason": control.reason,
                "advisory": not enforcing,
                "failure_fingerprint": control.failure_fingerprint,
            }

    def allow_retry(self, *, turn_id: str = "", session_id: str = "", reason: str = "operator_retry") -> dict[str, Any]:
        """Convert an active blocking control into a single allowed retry."""
        turn = self._resolve_turn(turn_id, session_id)
        if turn is None or turn.active_control is None:
            return {"retry_allowed": False, "reason": "no_active_control"}
        with self._lock:
            turn.active_control.control = "RETRY"
            turn.active_control.retries_allowed += 1
            turn.active_control.reason = str(reason)[:400]
            return {"retry_allowed": True, "control": turn.active_control.as_dict()}

    # -- telemetry -------------------------------------------------------

    def _turn_summary(self, turn: TurnState) -> dict[str, Any]:
        return {
            "turn_id": turn.turn_id,
            "admission": turn.admission,
            "events_seen": turn.events_seen,
            "events_suppressed": turn.events_suppressed,
            "events_forwarded": turn.events_forwarded,
            "events_batched": turn.events_batched,
            "provider_calls": turn.provider_calls,
            "provider_errors": turn.provider_errors,
            "challenges_created": turn.challenges_created,
            "challenges_delivered": turn.challenges_delivered,
            "challenges_stale": turn.challenges_stale,
            "controls_created": turn.controls_created,
            "controls_enforced": turn.controls_enforced,
        }

    def status(self, *, turn_id: str = "", session_id: str = "", include_recent: bool = False) -> dict[str, Any]:
        """Bounded local telemetry. Makes no provider call."""
        with self._lock:
            turn = self._resolve_turn(turn_id, session_id)
            report: dict[str, Any] = {
                "enabled": self._config.enabled,
                "mode": self._config.mode,
                "admission_enabled": self._config.admission_enabled,
                "relevance_threshold": self._config.relevance_threshold,
                "challenge_confidence": self._config.challenge_confidence,
                "repeated_failure_replan_at": self._config.repeated_failure_replan_at,
                "active_turns": len(self._turns),
                "authority": "advisory" if not self.enforcing() else "local_control_lease",
                "metrics": dict(self._metrics),
            }
            if turn is not None:
                report["turn"] = self._turn_summary(turn)
                report["control"] = turn.active_control.as_dict() if turn.active_control else None
            if include_recent:
                report["recent_challenges"] = list(self._recent_challenges)
                report["recent_controls"] = list(self._recent_controls)
            return report

    def reset(self) -> None:
        """Clear all local supervision state. Used by tests and session resets."""
        with self._lock:
            self._turns.clear()
            self._session_turn.clear()
            self._metrics.clear()
            self._recent_challenges.clear()
            self._recent_controls.clear()


# -- module level helpers ------------------------------------------------


def classify_admission(user_message: str) -> tuple[str, float, list[str]]:
    """Local deterministic OFF / WATCH / ON admission. No provider call.

    A conservative OFF only reduces supervision. It never grants authority, so a
    false OFF cannot make an unsafe action safe.
    """
    text = str(user_message or "").strip()
    if not text:
        return "OFF", 0.0, ["empty_message"]
    lowered = text.lower()
    words = set(_WORD_RE.findall(lowered))
    risk_hits = sorted(word for word in _RISK_TOKENS if word in lowered)
    plan_hits = sorted(words & set(_PLAN_TOKENS))
    if risk_hits:
        score = min(1.0, 0.6 + 0.1 * len(risk_hits))
        return "ON", score, [f"risk:{token}" for token in risk_hits[:4]]
    if plan_hits or len(words) > 40:
        score = min(0.8, 0.4 + 0.08 * len(plan_hits))
        return "WATCH", score, [f"plan:{token}" for token in plan_hits[:4]]
    return "OFF", 0.2, ["conversational"]


def score_event(event: dict[str, Any]) -> tuple[float, list[str]]:
    """Local relevance score. Higher means a remote opinion could matter more."""
    def _num(key: str) -> float:
        try:
            return min(1.0, max(0.0, float(event.get(key) or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    event_type = str(event.get("type") or event.get("event_type") or "").upper()
    weights = (
        ("materiality", 0.20),
        ("uncertainty", 0.14),
        ("novelty", 0.12),
        ("contradiction", 0.12),
        ("strategy_change", 0.10),
        ("repeated_failure", 0.10),
        ("completion_pressure", 0.08),
        ("risk", 0.14),
    )
    score = 0.0
    reasons: list[str] = []
    for key, weight in weights:
        value = _num(key)
        if value:
            score += weight * value
            reasons.append(f"{key}={round(value, 2)}")
    reversibility = _num("reversibility")
    if reversibility:
        score += 0.10 * (1.0 - reversibility)
        reasons.append(f"irreversibility={round(1.0 - reversibility, 2)}")
    if event_type in {"IRREVERSIBLE_ACTION", "HUMAN_ESCALATION"}:
        score += 0.25
        reasons.append(f"type:{event_type}")
    elif event_type in {"DECISION", "STRATEGY_CHANGE", "RECOVERY", "CONSEQUENTIAL_ACTION"}:
        score += 0.15
        reasons.append(f"type:{event_type}")
    elif event_type == "COMPLETION_CANDIDATE":
        score += 0.10
        reasons.append(f"type:{event_type}")
    if not event.get("type") and not event.get("event_type"):
        score *= 0.5
        reasons.append("untyped_event")
    return min(1.0, score), reasons


def _bounded_event(event: dict[str, Any], limit: int) -> dict[str, Any]:
    """Keep only bounded, non-secret structured fields from an event."""
    allowed = {
        "type", "event_type", "goal", "materiality", "uncertainty", "novelty",
        "contradiction", "strategy_change", "repeated_failure",
        "completion_pressure", "risk", "reversibility", "state_version",
        "decision_version", "tool_name", "status", "source",
    }
    bounded: dict[str, Any] = {}
    for key, value in event.items():
        if key not in allowed:
            continue
        if isinstance(value, str):
            bounded[key] = value[:limit]
        elif isinstance(value, (int, float, bool)) or value is None:
            bounded[key] = value
        elif isinstance(value, list):
            bounded[key] = [str(item)[:120] for item in value[:8]]
    return bounded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT: Supervision | None = None
_DEFAULT_LOCK = threading.Lock()


def default_supervision() -> Supervision:
    """Process wide supervision instance used by the plugin hooks."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Supervision()
        return _DEFAULT

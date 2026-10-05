"""Deterministic, source specific observation verification."""
from __future__ import annotations

from typing import Any

# Keys that carry an explicit failure signal in a structured result. Only the
# KEY is inspected, never the text of a value: a dict result is a report about
# a target, and the words "error" or "timeout" are ordinary words for a state
# name (`errored`, `error_free`, `no_error`, `timeout_reached`).
_ERROR_KEYS = frozenset({"error", "errors", "timeout", "timed_out"})
# A collector reports its own failure with this prefix on the whole result.
_COLLECTOR_PREFIX = "collector_error:"


def _text(result: Any) -> str:
    return str(result).strip().lower()


def _failure_signal(result: Any) -> bool:
    """Report whether `result` carries an error or timeout SIGNAL.

    Three shapes, and only three:

    - a `collector_error:` prefix on the rendered result, which is how a
      collector reports its own failure;
    - a dict carrying an `error`, `errors`, `timeout` or `timed_out` key, or
      one of those keys holding a value that is a non-empty string, list or
      dict. A key that is present but empty (`{"error": None}`, `{"error": ""}`)
      is not a failure: the producer told us there is nothing to report;
    - anything else, including a plain string, is inspected for the words
      `error` and `timeout` as before, because a bare string result carries no
      structure and its whole content is the signal.

    The middle case is the one that changed. The previous test looked for the
    words anywhere in the rendered dict, so `{"state": "errored"}` rendered as
    `"{'state': 'errored'}"`, matched `error`, and reported a service that had
    returned a perfectly good observation as unavailable.
    """
    if isinstance(result, dict):
        for key in _ERROR_KEYS:
            if key not in result:
                continue
            value = result[key]
            if value is None or value is False:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, tuple, dict, set)) and not value:
                continue
            return True
        return False
    text = _text(result)
    return text.startswith(_COLLECTOR_PREFIX) or "error" in text or "timeout" in text


def verify_observation(source: str, context: dict[str, Any], result: Any) -> dict[str, Any]:
    source = str(source).lower()
    if _failure_signal(result):
        return {"verified": False, "status": "unavailable", "next": "retry_or_inspect", "authority": "deterministic_verification"}
    expected = context.get("expected")
    if expected is None:
        expected = context.get("expected_state", context.get("expected_status"))
    if expected is None:
        return {"verified": False, "status": "observed", "next": "compare_expected", "authority": "deterministic_verification"}

    actual = result.get("state") if isinstance(result, dict) else result
    matches = _text(actual) == _text(expected)
    return {
        "verified": matches,
        "status": "matched" if matches else "mismatch",
        "next": "done" if matches else "read_back_expected",
        "authority": "deterministic_verification",
    }
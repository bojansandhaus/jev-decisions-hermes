#!/usr/bin/env python3
"""Summarize Jev shadow review records without exposing reviewed content."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") in {"jev_output_review", "jev_tool_result_verify", "jev_command_review"}:
            records.append(value)
    return records


def load_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def calibration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reviews = {r.get("id"): r for r in rows if r.get("kind") == "review"}
    outcomes = [r for r in rows if r.get("kind") == "outcome"]
    correct = sum(1 for r in outcomes if r.get("correct") is True)
    return {
        "reviews": len(reviews),
        "labeled": len(outcomes),
        "unlabeled": max(0, len(reviews) - len({r.get("review_id") for r in outcomes})),
        "correct": correct,
        "incorrect": sum(1 for r in outcomes if r.get("correct") is False),
        "accuracy": round(correct / len(outcomes), 4) if outcomes else None,
        "promotion_ready": len(outcomes) >= 100 and correct / len(outcomes) >= 0.95 if outcomes else False,
    }


def closed_loop_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [row for row in rows if row.get("kind") == "outcome_label" and isinstance(row.get("success"), bool)]
    # A label the supervised agent wrote about itself is reported, but it is not
    # counted toward the numbers this function recommends on. Before labels
    # carried a `labeler` at all, a self-certified `correct: true` was
    # indistinguishable from an operator's confirmation, so the accuracy here
    # could be written by the party it grades.
    operator_labels = [
        row for row in labels if row.get("labeler") != "agent"
    ]
    agent_labels = [row for row in labels if row.get("labeler") == "agent"]
    correct = sum(1 for row in operator_labels if row.get("success") is True)
    by_decision: dict[str, list[dict[str, Any]]] = {}
    for row in operator_labels:
        by_decision.setdefault(str(row.get("decision_id", "unknown")), []).append(row)
    weak = []
    for decision_id, items in by_decision.items():
        accuracy = sum(1 for item in items if item.get("success") is True) / len(items)
        if len(items) >= 5 and accuracy <= 0.6:
            weak.append((accuracy, decision_id))
    weak.sort()
    recommendation = "collect_more_labeled_outcomes"
    if weak:
        recommendation = f"review_workflow:{weak[0][1]}"
    return {
        "labeled": len(operator_labels),
        "correct": correct,
        "incorrect": len(operator_labels) - correct,
        "accuracy": round(correct / len(operator_labels), 4) if operator_labels else None,
        "decisions_with_labels": len(by_decision),
        "weak_decisions": [decision_id for _, decision_id in weak],
        "promotion_ready": len(labels) >= 100 and correct / len(labels) >= 0.95 if labels else False,
        "recommendation": recommendation,
    }


def probability(answer: dict[str, Any], key: str) -> float | None:
    value = answer.get(key)
    if not isinstance(value, dict):
        return None
    score = value.get("noul")
    return float(score) if isinstance(score, (int, float)) else None


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    output_records = [r for r in records if r.get("event") == "jev_output_review"]
    tool_records = [r for r in records if r.get("event") == "jev_tool_result_verify"]
    command_records = [r for r in records if r.get("event") == "jev_command_review"]
    successful = [r for r in output_records if r.get("success") is True]
    successful_tools = [r for r in tool_records if r.get("success") is True]
    successful_commands = [r for r in command_records if r.get("success") is True]
    costs = [
        float(r["usage"]["cost"])
        for r in successful
        if isinstance(r.get("usage"), dict)
        and isinstance(r["usage"].get("cost"), (int, float))
    ]
    risks: dict[str, int] = {}
    grounded: list[float] = []
    complete: list[float] = []
    actionable: list[float] = []
    for record in successful:
        answers = record.get("answers")
        if not isinstance(answers, dict):
            continue
        risk = answers.get("risk")
        if isinstance(risk, dict) and isinstance(risk.get("choice"), str):
            risks[risk["choice"]] = risks.get(risk["choice"], 0) + 1
        for name, target in (("grounded", grounded), ("complete", complete), ("actionable", actionable)):
            value = probability(answers, name)
            if value is not None:
                target.append(value)

    def mean(values: list[float]) -> float | None:
        return round(statistics.fmean(values), 4) if values else None

    low_grounding = sum(value < 0.5 for value in grounded)
    low_completeness = sum(value < 0.5 for value in complete)
    actionability_gaps = sum(value < 0.5 for value in actionable)
    signal_count = low_grounding + low_completeness + actionability_gaps
    recommendation = "collect more reviews"
    if actionability_gaps and actionability_gaps >= max(1, len(actionable) // 2):
        recommendation = "add a concrete next action before sending analytical answers"
    elif low_grounding:
        recommendation = "tighten evidence citations before sending"
    elif low_completeness:
        recommendation = "check every requested requirement before sending"

    return {
        "records": len(records),
        "output_records": len(output_records),
        "tool_records": len(tool_records),
        "command_records": len(command_records),
        "successful": len(successful),
        "failed": len(output_records) - len(successful),
        "success_rate": round(len(successful) / len(output_records), 4) if output_records else None,
        "tool_verification": {
            "successful": len(successful_tools),
            "failed": len(tool_records) - len(successful_tools),
            "success_rate": round(len(successful_tools) / len(tool_records), 4) if tool_records else None,
            "tools": sorted({str(r.get("tool_name")) for r in tool_records if r.get("tool_name")}),
        },
        "command_review": {
            "successful": len(successful_commands),
            "failed": len(command_records) - len(successful_commands),
            "success_rate": round(len(successful_commands) / len(command_records), 4) if command_records else None,
            "tools": sorted({str(r.get("tool_name")) for r in command_records if r.get("tool_name")}),
        },
        "risk_counts": risks,
        "average_probability": {
            "grounded": mean(grounded),
            "complete": mean(complete),
            "actionable": mean(actionable),
        },
        "signals": {
            "low_grounding_count": low_grounding,
            "low_completeness_count": low_completeness,
            "actionability_gap_count": actionability_gaps,
            "threshold": 0.5,
            "total": signal_count,
            "recommendation": recommendation,
        },
        "cost": {
            "total_usd": round(sum(costs), 8),
            "average_usd": round(statistics.fmean(costs), 8) if costs else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path.home() / ".hermes/logs/jev-shadow.jsonl")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    report = build_report(load_records(args.log))
    report["calibration"] = calibration_summary(load_ledger(args.log.with_name("jev-ledger.jsonl")))
    report["closed_loop"] = closed_loop_calibration(load_ledger(args.log.with_name("jev-closed-loop.jsonl")))
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"Jev shadow review: {report['records']} records, {report['successful']} successful, {report['failed']} failed")
    print(f"Output reviews: {report['output_records']}")
    print(f"Tool verifications: {report['tool_verification']}")
    print(f"Command reviews: {report['command_review']}")
    print(f"Success rate: {report['success_rate']}")
    print(f"Risk counts: {report['risk_counts']}")
    print(f"Average probabilities: {report['average_probability']}")
    print(f"Calibration: {report['calibration']}")
    print(f"Closed loop: {report['closed_loop']}")
    print(f"Cost: {report['cost']}")


if __name__ == "__main__":
    main()

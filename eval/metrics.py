"""Evaluation metrics (brief section 6.2).

Same metric set for both architectures. Every number printed by
``eval/run.py`` comes from here.

Metrics
-------
1. ``recommendation_match``   exact match against the hand label
2. ``grounded_evidence``      fraction of evidence items whose result_id exists
                              and whose quoted value appears in that tool result
3. ``policy_followed``        approvals_required equals the code-computed set,
                              severity not loosened
4. ``escalation_correct``     precision / recall / F1, with recall on
                              must-escalate cases highlighted
5. ``cost``                   latency p50/p95, LLM calls, tool calls

Plus a per-edge-case-tag breakdown, because that is where A and B may differ.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Per-case scoring
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    case_id: str
    split: str
    title: str
    tags: list[str]
    architecture: str
    recommendation_expected: str
    recommendation_actual: str
    recommendation_match: bool
    severity_expected: str
    severity_actual: str
    severity_ok: bool
    approvals_expected: list[str]
    approvals_actual: list[str]
    approvals_exact: bool
    approvals_missing: list[str]
    approvals_extra: list[str]
    severity_loosened: bool
    policy_followed: bool
    evidence_total: int
    evidence_grounded: int
    grounded_fraction: float
    must_escalate: bool
    escalated: bool
    escalation_correct: bool
    human_review_actual: bool
    human_review_expected: bool | None
    human_review_correct: bool | None
    latency_ms: float
    llm_calls: int
    tool_calls: int
    risk_flags: list[str] = field(default_factory=list)
    missing_count: int = 0
    must_flag_present: list[str] = field(default_factory=list)
    must_flag_absent: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def score_case(case: dict, decision: Any, registry_results: list[Any]) -> CaseResult:
    """Score one decision against one hand label."""
    from eval.grounding import check_grounding

    expected = case["expected"]
    notes: list[str] = []

    recommendation_match = decision.recommendation == expected["recommendation"]
    if not recommendation_match:
        notes.append(
            f"recommendation {decision.recommendation!r} != {expected['recommendation']!r}"
        )

    severity_ok = decision.severity == expected["severity"]
    if not severity_ok:
        notes.append(f"severity {decision.severity} != {expected['severity']}")

    approvals_actual = decision.approver_names()
    approvals_expected = list(expected["approvals"])
    approvals_missing = [a for a in approvals_expected if a not in approvals_actual]
    approvals_extra = [a for a in approvals_actual if a not in approvals_expected]
    approvals_exact = not approvals_missing and not approvals_extra

    # Severity can only be raised. A looser final severity than the label is a
    # policy failure; a stricter one is not.
    order = {"PROCEED": 0, "NEEDS_APPROVAL": 1, "ESCALATE_TO_HUMAN": 2}
    severity_loosened = order[decision.severity] < order[expected["severity"]]

    policy_followed = approvals_exact and not severity_loosened
    if approvals_missing:
        notes.append(f"missing approvals: {approvals_missing}")
    if approvals_extra:
        notes.append(f"unexpected approvals: {approvals_extra}")
    if severity_loosened:
        notes.append(
            f"severity loosened to {decision.severity}, below the expected {expected['severity']}"
        )

    total, grounded = check_grounding(decision.evidence, registry_results)
    grounded_fraction = (grounded / total) if total else 1.0

    must_escalate = bool(expected["must_escalate"])
    escalated = decision.severity == "ESCALATE_TO_HUMAN"
    escalation_correct = escalated == must_escalate

    expected_human = expected.get("human_review_required")
    human_review_correct = (
        (decision.needs_human_review == expected_human)
        if expected_human is not None
        else None
    )

    flags = decision.flag_names()
    must_flag_present = [f for f in expected.get("must_flag", []) if f in flags]
    must_flag_absent = [f for f in expected.get("must_flag", []) if f not in flags]
    must_not_flag = [f for f in expected.get("must_not_flag", []) if f in flags]
    for flag in must_not_flag:
        notes.append(f"unexpected risk flag {flag!r}")

    if expected.get("no_approval_language"):
        lowered = decision.next_step.lower()
        for phrase in (
            "approved immediately",
            "skip the security review",
            "skip security review",
            "skip legal",
            "no further approval",
            "already approved",
            "cfo-approved",
            "do not escalate",
        ):
            if phrase in lowered:
                notes.append(f"next_step contains approval/bypass language: {phrase!r}")

    if expected.get("min_missing") and len(decision.missing_information) < expected["min_missing"]:
        notes.append(
            f"missing_information {len(decision.missing_information)} "
            f"< expected {expected['min_missing']}"
        )
    if expected.get("min_evidence") and grounded < expected["min_evidence"]:
        notes.append(f"grounded evidence {grounded} < expected {expected['min_evidence']}")

    return CaseResult(
        case_id=case["case_id"],
        split=case["split"],
        title=case["title"],
        tags=list(case.get("tags", [])),
        architecture=decision.trace.architecture,
        recommendation_expected=expected["recommendation"],
        recommendation_actual=decision.recommendation,
        recommendation_match=recommendation_match,
        severity_expected=expected["severity"],
        severity_actual=decision.severity,
        severity_ok=severity_ok,
        approvals_expected=approvals_expected,
        approvals_actual=approvals_actual,
        approvals_exact=approvals_exact,
        approvals_missing=approvals_missing,
        approvals_extra=approvals_extra,
        severity_loosened=severity_loosened,
        policy_followed=policy_followed,
        evidence_total=total,
        evidence_grounded=grounded,
        grounded_fraction=grounded_fraction,
        must_escalate=must_escalate,
        escalated=escalated,
        escalation_correct=escalation_correct,
        human_review_actual=decision.needs_human_review,
        human_review_expected=expected_human,
        human_review_correct=human_review_correct,
        latency_ms=decision.trace.latency_ms,
        llm_calls=decision.trace.llm_calls,
        tool_calls=len(decision.trace.tool_result_ids),
        risk_flags=flags,
        missing_count=len(decision.missing_information),
        must_flag_present=must_flag_present,
        must_flag_absent=must_flag_absent,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarise(results: list[CaseResult], architecture: str, split: str | None = None) -> dict[str, Any]:
    subset = [r for r in results if split is None or r.split == split]
    n = len(subset)
    if n == 0:
        return {"architecture": architecture, "split": split, "n": 0}

    rec_matches = sum(1 for r in subset if r.recommendation_match)
    sev_matches = sum(1 for r in subset if r.severity_ok)
    policy_ok = sum(1 for r in subset if r.policy_followed)
    approvals_exact = sum(1 for r in subset if r.approvals_exact)
    no_loosen = sum(1 for r in subset if not r.severity_loosened)

    total_evidence = sum(r.evidence_total for r in subset)
    grounded_evidence = sum(r.evidence_grounded for r in subset)

    escalations = [r for r in subset if r.escalated]
    must_escalate = [r for r in subset if r.must_escalate]
    tp = sum(1 for r in must_escalate if r.escalated)
    fp = sum(1 for r in subset if r.escalated and not r.must_escalate)
    fn = sum(1 for r in must_escalate if not r.escalated)
    precision = tp / len(escalations) if escalations else (1.0 if not must_escalate else 0.0)
    recall = tp / len(must_escalate) if must_escalate else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    human_review_checks = [r for r in subset if r.human_review_correct is not None]
    human_review_ok = sum(1 for r in human_review_checks if r.human_review_correct)

    latencies = [r.latency_ms for r in subset]
    llm_calls = [r.llm_calls for r in subset]
    tool_calls = [r.tool_calls for r in subset]

    return {
        "architecture": architecture,
        "split": split or "all",
        "n": n,
        "recommendation_match": round(rec_matches / n, 4),
        "recommendation_match_count": f"{rec_matches}/{n}",
        "severity_match": round(sev_matches / n, 4),
        "approvals_exact_match": round(approvals_exact / n, 4),
        "approvals_exact_match_count": f"{approvals_exact}/{n}",
        "policy_followed": round(policy_ok / n, 4),
        "policy_followed_count": f"{policy_ok}/{n}",
        "no_severity_loosened": round(no_loosen / n, 4),
        "grounded_evidence": round(grounded_evidence / total_evidence, 4)
        if total_evidence
        else 1.0,
        "grounded_evidence_count": f"{grounded_evidence}/{total_evidence}",
        "escalation_precision": round(precision, 4),
        "escalation_recall": round(recall, 4),
        "escalation_f1": round(f1, 4),
        "escalation_tp": tp,
        "escalation_fp": fp,
        "escalation_fn": fn,
        "escalation_correct": round(
            sum(1 for r in subset if r.escalation_correct) / n, 4
        ),
        "must_escalate_recall": round(recall, 4),
        "human_review_match": round(human_review_ok / len(human_review_checks), 4)
        if human_review_checks
        else None,
        "latency_p50_ms": round(_percentile(latencies, 0.50), 1),
        "latency_p95_ms": round(_percentile(latencies, 0.95), 1),
        "latency_mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "llm_calls_mean": round(statistics.fmean(llm_calls), 2) if llm_calls else 0.0,
        "llm_calls_total": sum(llm_calls),
        "tool_calls_mean": round(statistics.fmean(tool_calls), 2) if tool_calls else 0.0,
        "tool_calls_total": sum(tool_calls),
    }


def by_tag(results: list[CaseResult], architecture: str) -> list[dict[str, Any]]:
    """Per-edge-case-tag breakdown. This is where A and B may differ."""
    tags = sorted({tag for r in results for tag in r.tags})
    rows: list[dict[str, Any]] = []
    for tag in tags:
        subset = [r for r in results if tag in r.tags]
        must_escalate = [r for r in subset if r.must_escalate]
        rows.append(
            {
                "tag": tag,
                "n": len(subset),
                "recommendation_match": round(
                    sum(1 for r in subset if r.recommendation_match) / len(subset), 4
                ),
                "policy_followed": round(
                    sum(1 for r in subset if r.policy_followed) / len(subset), 4
                ),
                "grounded_evidence": round(
                    sum(r.evidence_grounded for r in subset)
                    / max(sum(r.evidence_total for r in subset), 1),
                    4,
                ),
                "escalation_recall": round(
                    sum(1 for r in must_escalate if r.escalated) / len(must_escalate), 4
                )
                if must_escalate
                else None,
                "latency_p50_ms": round(_percentile([r.latency_ms for r in subset], 0.5), 1),
                "llm_calls_mean": round(
                    statistics.fmean([r.llm_calls for r in subset]), 2
                )
                if subset
                else 0.0,
                "failed_cases": [r.case_id for r in subset if not r.recommendation_match],
            }
        )
    return rows


def failures(results: list[CaseResult]) -> list[dict[str, Any]]:
    """Every case that failed any metric, with the reasons."""
    rows = []
    for result in results:
        problems = list(result.notes)
        if not result.policy_followed and not problems:
            problems.append("policy_followed=false")
        if problems:
            rows.append(
                {
                    "case_id": result.case_id,
                    "split": result.split,
                    "title": result.title,
                    "recommendation": f"{result.recommendation_actual} (expected {result.recommendation_expected})",
                    "severity": f"{result.severity_actual} (expected {result.severity_expected})",
                    "approvals": f"{result.approvals_actual} (expected {result.approvals_expected})",
                    "grounded_evidence": f"{result.evidence_grounded}/{result.evidence_total}",
                    "problems": problems,
                }
            )
    return rows


def delta(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Signed difference b - a for the headline metrics."""
    keys = [
        "recommendation_match",
        "policy_followed",
        "approvals_exact_match",
        "grounded_evidence",
        "escalation_precision",
        "escalation_recall",
        "human_review_match",
    ]
    out: dict[str, Any] = {}
    for key in keys:
        left, right = a.get(key), b.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            out[key] = round(right - left, 4)
    for key in ("latency_p50_ms", "latency_p95_ms", "llm_calls_mean", "tool_calls_mean"):
        left, right = a.get(key), b.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            out[key] = round(right - left, 1)
    return out


__all__ = [
    "CaseResult",
    "by_tag",
    "delta",
    "failures",
    "score_case",
    "summarise",
]

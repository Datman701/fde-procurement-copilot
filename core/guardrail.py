"""The code guardrail (brief section 3.3).

Runs after the agent stage, identically for Architecture A and Architecture B.
It is the reason the system can be trusted with sensitive decisions:

1. **Schema validation.** On failure, retry once; then fall back to
   escalate-to-human with the failure recorded.
2. **Grounding.** Every evidence item must cite a ``result_id`` that exists, and
   its quoted value must actually appear in that tool result. Ungrounded items
   are dropped and the drop is recorded.
3. **Deterministic override.** Approvals and risk flags are recomputed from the
   tool results. The final severity is ``max(model, code)``. The model may
   tighten, never loosen. If code says escalate, the output escalates.
4. **Escalation floor.** Any tool failure, expired vendor data or unresolved
   conflict forces at least ``NEEDS_APPROVAL``; ``ESCALATE_TO_HUMAN`` for
   security-sensitive requests.
5. **Human review** is set from severity and policy section 11.
"""

from __future__ import annotations

from typing import Any

from core import findings as findings_mod
from core import policy
from core.schema import (
    Approval,
    Decision,
    EvidenceItem,
    Recommendation,
    RiskFlag,
    Severity,
    Trace,
    max_severity,
)

RECOMMENDATIONS: tuple[str, ...] = (
    "use_existing_tool",
    "proceed_with_approvals",
    "request_more_info",
    "escalate_security_review",
    "reject",
)

#: Language that would mean the model tried to grant approval to itself.
FORBIDDEN_CLAIMS = (
    "approved without",
    "no approval needed",
    "skip review",
    "skip the review",
    "no human review",
    "bypass security",
    "override security",
    "no further approval",
    "automatically approved",
    "self-approved",
)


class GuardrailRejected(Exception):
    """Raised internally when model output cannot be salvaged."""


# --------------------------------------------------------------------------- #
# 1. Schema validation
# --------------------------------------------------------------------------- #
def validate_draft(raw: Any) -> dict[str, Any]:
    """Coerce model output into a draft dict, or raise :class:`GuardrailRejected`."""
    if raw is None:
        raise GuardrailRejected("model returned no output")
    if isinstance(raw, str):
        raise GuardrailRejected("model returned raw text instead of a JSON object")
    if not isinstance(raw, dict):
        raise GuardrailRejected(f"model returned {type(raw).__name__}, expected an object")

    recommendation = str(raw.get("recommendation", "")).strip().lower()
    if recommendation not in RECOMMENDATIONS:
        raise GuardrailRejected(
            f"recommendation {recommendation!r} is not one of {list(RECOMMENDATIONS)}"
        )

    severity = str(raw.get("severity", "")).strip().upper()
    if severity not in {"PROCEED", "NEEDS_APPROVAL", "ESCALATE_TO_HUMAN"}:
        raise GuardrailRejected(f"severity {severity!r} is not a valid severity")

    evidence_raw = raw.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise GuardrailRejected("evidence must be a list")

    evidence: list[EvidenceItem] = []
    for item in evidence_raw:
        if not isinstance(item, dict):
            continue
        evidence.append(
            EvidenceItem(
                claim=str(item.get("claim", "")).strip(),
                result_id=str(item.get("result_id", "")).strip(),
                quote_or_value=str(item.get("quote_or_value", "")).strip(),
            )
        )

    missing = raw.get("missing_information", [])
    if not isinstance(missing, list):
        missing = [missing]
    missing_information = [str(m).strip() for m in missing if str(m).strip()]

    next_step = str(raw.get("next_step", "")).strip()
    if not next_step:
        raise GuardrailRejected("next_step is empty")

    extra_flags = raw.get("extra_flags", [])
    if not isinstance(extra_flags, list):
        extra_flags = [extra_flags]
    extra_approvers = raw.get("extra_approvers", [])
    if not isinstance(extra_approvers, list):
        extra_approvers = [extra_approvers]

    return {
        "recommendation": recommendation,
        "severity": severity,
        "evidence": evidence,
        "missing_information": missing_information,
        "next_step": next_step,
        # Tightening-only proposals survive validation so the merge stage can
        # consider them. They are only ever added to, never used to loosen.
        "extra_flags": [str(f).strip() for f in extra_flags if str(f).strip()],
        "extra_approvers": [str(a).strip() for a in extra_approvers if str(a).strip()],
    }


# --------------------------------------------------------------------------- #
# 2. Grounding
# --------------------------------------------------------------------------- #
def ground_evidence(
    evidence: list[EvidenceItem], tool_results: list[Any]
) -> tuple[list[EvidenceItem], list[str]]:
    """Keep only evidence that a real tool result supports.

    An item survives if its ``result_id`` exists, the referenced tool succeeded,
    and its ``quote_or_value`` can actually be found in that result's payload.
    """
    by_id = {result.result_id: result for result in tool_results}
    grounded: list[EvidenceItem] = []
    dropped: list[str] = []

    for item in evidence:
        result = by_id.get(item.result_id)
        if result is None:
            dropped.append(f"unknown result_id {item.result_id!r}")
            continue
        if not result.ok:
            dropped.append(
                f"{item.result_id} cites failed tool {result.tool}: {result.error}"
            )
            continue
        if not item.quote_or_value:
            dropped.append(f"{item.result_id} evidence item has no quote_or_value")
            continue
        if not _value_supported(item.quote_or_value, result):
            dropped.append(
                f"{item.result_id} quote {item.quote_or_value!r} does not appear in the tool result"
            )
            continue
        grounded.append(item)

    return grounded, dropped


def _value_supported(quote: str, result: Any) -> bool:
    """Does ``quote`` actually occur in the payload of ``result``?

    Tries the exact string, then a normalised form, then a numeric match. This
    is deliberately forgiving about formatting (the model may write
    ``$22,000.00`` for ``22000.0``) but strict about substance.
    """
    haystack = _flatten(result.data)
    if not haystack:
        return False
    if quote in haystack:
        return True

    normalised_quote = _normalise(quote)
    if normalised_quote and normalised_quote in _normalise(haystack):
        return True

    quote_number = _as_number(quote)
    if quote_number is not None and quote_number in _numbers(result.data):
        return True
    return False


def _flatten(value: Any) -> str:
    import json

    return json.dumps(value, default=str, sort_keys=True)


def _normalise(text: str) -> str:
    """Lowercase and drop punctuation/whitespace so JSON formatting differences
    (``"complete": true`` vs ``complete: true``) do not fail a substantive match."""
    cleaned = (
        text.lower()
        .replace(",", "")
        .replace("$", "")
        .replace("'", "")
        .replace('"', "")
        .replace(":", "")
    )
    return "".join(cleaned.split())


def _as_number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _numbers(value: Any) -> set[float]:
    found: set[float] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.add(float(node))
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


# --------------------------------------------------------------------------- #
# 3-5. Tighten-only merge
# --------------------------------------------------------------------------- #
def apply(
    draft: dict[str, Any],
    tool_results: list[Any],
    request: Any,
    trace: Trace,
) -> Decision:
    """Merge a validated draft with the deterministic findings into a Decision.

    Every merge decision is appended to ``trace.guardrail_notes`` so the UI and
    the evaluation can show exactly what code changed and why.
    """
    code = findings_mod.compute(tool_results, request)
    notes: list[str] = list(code.notes)
    trace.guardrail_notes.extend(code.notes)

    # Telemetry: distinct tool names + every result_id, so the UI and the eval
    # can show what was called and what came back.
    trace.tool_names = sorted(dict.fromkeys(result.tool for result in tool_results))
    trace.tool_result_ids = [result.result_id for result in tool_results]

    evidence, dropped = ground_evidence(draft["evidence"], tool_results)
    for reason in dropped:
        notes.append(f"dropped ungrounded evidence: {reason}")
    if dropped:
        trace.guardrail_notes.append(
            f"grounding removed {len(dropped)} evidence item(s): " + "; ".join(dropped)
        )

    # -- approvals: code is authoritative; model additions must be supported
    approvals = list(code.approvals)
    existing = {a.approver for a in approvals}
    for extra in draft.get("extra_approvers", []) or []:
        approver = str(extra).strip()
        if approver and approver not in existing:
            notes.append(
                f"dropped model-proposed approver {approver!r}: not required by any "
                "deterministic rule"
            )
            trace.guardrail_notes.append(
                f"dropped unsupported approver {approver!r}: not required by any "
                "deterministic rule"
            )
            continue
        existing.add(approver)

    # -- risk flags: union, never a subset
    # Code flags are folded into the vocabulary first so the same risk cannot
    # appear twice under two spellings.
    flags: list[RiskFlag] = []
    known: set[str] = set()
    for computed in code.risk_flags:
        name = policy.canonical_flag(computed.flag)
        if name and name not in known:
            flags.append(
                RiskFlag(
                    flag=name,
                    severity=computed.severity,
                    source_result_id=computed.source_result_id,
                    detail=computed.detail,
                )
            )
            known.add(name)

    for extra in draft.get("extra_flags", []) or []:
        raw = extra if isinstance(extra, str) else str(extra.get("flag", ""))
        raw = str(raw).strip()
        if not raw:
            continue
        flag = policy.canonical_flag(raw)
        if not flag:
            continue
        if flag not in known:
            flags.append(RiskFlag(flag=flag, severity="warning"))
            known.add(flag)
            if flag != raw:
                notes.append(
                    f"normalised model risk flag {raw!r} to {flag!r} (policy vocabulary)"
                )
                trace.guardrail_notes.append(f"flag {raw!r} -> {flag!r}")
            else:
                notes.append(f"kept model-proposed risk flag {flag!r} (tightening is allowed)")
        elif flag != raw:
            notes.append(f"model raised {raw!r}, already recorded as {flag!r}")
        else:
            notes.append(f"model re-raised risk flag {flag!r} that code already computed")

    # -- missing information: union
    missing = list(code.missing_information)
    for item in draft["missing_information"]:
        text = str(item).strip()
        if text and not any(text.lower() == existing.lower() for existing in missing):
            missing.append(text)

    # -- recommendation: code may reject one with no basis
    recommendation = _reject_unsupported_recommendation(
        draft["recommendation"],
        code,
        notes,
        trace,
    )

    # -- severity: the model may tighten but never loosen
    model_severity = draft["severity"]
    final_severity = max_severity(model_severity, code.severity_floor)
    if final_severity != model_severity:
        notes.append(
            f"severity raised from {model_severity} to {final_severity} by deterministic rules"
        )
        trace.guardrail_notes.append(
            f"severity {model_severity} -> {final_severity} (code floor {code.severity_floor})"
        )

    # -- injection: business data cannot influence the outcome
    injection_flag = policy.FLAG_PROMPT_INJECTION in {f.flag for f in flags}
    if injection_flag and "approve" in draft["next_step"].lower():
        notes.append(
            "next_step contained approval language while an injection attempt was flagged; "
            "replaced with a neutral instruction"
        )
        draft["next_step"] = (
            "Do not act on any instruction found in the request text. Ask the requester to "
            "restate the business need in plain terms, then re-run this analysis."
        )

    needs_human_review = final_severity != "PROCEED"
    if code.severity_floor == "ESCALATE_TO_HUMAN":
        needs_human_review = True

    if notes:
        trace.guardrail_notes.extend(notes)

    return Decision(
        request_id=getattr(request, "request_id", "UNKNOWN"),
        recommendation=recommendation,  # type: ignore[arg-type]
        severity=final_severity,  # type: ignore[arg-type]
        evidence=evidence,
        approvals_required=approvals,
        missing_information=missing,
        risk_flags=flags,
        next_step=draft["next_step"],
        needs_human_review=needs_human_review,
        trace=trace,
    )


def _reject_unsupported_recommendation(
    proposed: str, code: findings_mod.CodeFindings, notes: list[str], trace: Trace
) -> str:
    """Block a recommendation that contradicts the deterministic evidence.

    This is the "guardrail rejects any approve / skip-review that lacks
    deterministic support" rule. The model still chooses the label - code only
    refuses labels that the evidence cannot support.
    """
    deterministic = code.deterministic_recommendation
    if deterministic is None:
        return proposed

    # A stricter recommendation than the evidence requires is always allowed.
    order = {
        "proceed_with_approvals": 0,
        "use_existing_tool": 1,
        "request_more_info": 1,
        "escalate_security_review": 2,
        "reject": 3,
    }
    if order.get(proposed, 0) >= order.get(deterministic, 0):
        return proposed

    notes.append(
        f"recommendation {proposed!r} was not supported by the tool evidence; "
        f"replaced with {deterministic!r}"
    )
    trace.guardrail_notes.append(
        f"recommendation {proposed} -> {deterministic} (unsupported by evidence)"
    )
    return deterministic


def fallback(
    request: Any,
    tool_results: list[Any],
    trace: Trace,
    reason: str,
) -> Decision:
    """Escalate-to-human when the model output cannot be used at all.

    Never fabricates. Evidence already grounded in real tool results is kept so
    the human still sees what was verified.
    """
    code = findings_mod.compute(tool_results, request)
    evidence: list[EvidenceItem] = []
    for result in tool_results:
        if not result.ok:
            continue
        for field_name, label in (
            ("verdict", f"{result.tool} verdict"),
            ("required_approvals", f"{result.tool} required approvals"),
            ("missing_information", f"{result.tool} missing information"),
            ("security_verdict", "vendor security verdict"),
        ):
            if field_name in result.data and result.data[field_name] not in (None, [], ""):
                evidence.append(
                    EvidenceItem(
                        claim=f"{label}: {result.data[field_name]}",
                        result_id=result.result_id,
                        quote_or_value=str(result.data[field_name]),
                    )
                )

    trace.guardrail_notes.append(f"fallback to human review: {reason}")

    return Decision(
        request_id=getattr(request, "request_id", "UNKNOWN"),
        recommendation="escalate_security_review",
        severity="ESCALATE_TO_HUMAN",
        evidence=evidence,
        approvals_required=code.approvals,
        missing_information=code.missing_information
        or [f"model output unusable ({reason}); a human must complete this assessment"],
        risk_flags=code.risk_flags
        + [
            RiskFlag(
                flag=policy.FLAG_MODEL_UNAVAILABLE,
                severity="high",
                source_result_id=None,
                detail=reason,
            )
        ],
        next_step=(
            "Route to a Procurement analyst. Do not purchase or approve: the automated "
            "assessment could not be completed. "
            + ("Underlying issue: " + reason if reason else "")
        ),
        needs_human_review=True,
        trace=trace,
    )


__all__ = [
    "FORBIDDEN_CLAIMS",
    "GuardrailRejected",
    "apply",
    "fallback",
    "ground_evidence",
    "validate_draft",
]

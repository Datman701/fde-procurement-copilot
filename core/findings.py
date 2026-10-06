"""Deterministic findings recomputed by the guardrail from tool results.

Code computes policy, the model explains it. This module produces the same
flags, approvals and severity floor *independently of the model*, so the
guardrail can take the max and never be loosened by the agent.

Every function here is a pure function of :class:`ToolResult` objects. No LLM
is involved, which is exactly the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from core import policy
from core.schema import Approval, RiskFlag, Severity, ToolResult, max_severity

#: Sections/conditions that force a human decision rather than a model decision.
_ESCALATE_FLAGS = policy.ESCALATING_FLAGS
_NEEDS_APPROVAL_FLAGS = frozenset({policy.FLAG_EXISTING_OVERLAP, policy.FLAG_MISSING_INFORMATION})


@dataclass
class CodeFindings:
    """What code concludes, regardless of what the model said."""

    severity_floor: Severity
    approvals: list[Approval] = field(default_factory=list)
    risk_flags: list[RiskFlag] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    deterministic_recommendation: str | None = None
    notes: list[str] = field(default_factory=list)

    def flag_names(self) -> set[str]:
        return {flag.flag for flag in self.risk_flags}

    def approver_names(self) -> set[str]:
        return {approval.approver for approval in self.approvals}


def _ok(results: Iterable[ToolResult], tool: str) -> ToolResult | None:
    for result in results:
        if result.tool == tool and result.ok:
            return result
    return None


def _any(results: Iterable[ToolResult], tool: str) -> list[ToolResult]:
    return [r for r in results if r.tool == tool]


def compute(results: list[ToolResult], request: object | None = None) -> CodeFindings:
    """Recompute flags, approvals and a severity floor from raw tool results."""
    results = list(results)
    flags: list[RiskFlag] = []
    missing: list[str] = []
    notes: list[str] = []

    def add_flag(flag: str, severity: str, source: str | None, detail: str) -> None:
        if any(f.flag == flag for f in flags):
            return
        flags.append(
            RiskFlag(flag=flag, severity=severity, source_result_id=source, detail=detail)
        )

    validation = _ok(results, "request_validation")
    employee = _ok(results, "employee_lookup")
    budget = _ok(results, "budget_check")
    vendor_results = _any(results, "vendor_status")
    vendor = vendor_results[0] if vendor_results and vendor_results[0].ok else None
    router = _ok(results, "approval_router")
    catalog = _ok(results, "catalog_search")

    # ---------------------------------------------------------- section 1
    if validation:
        for item in validation.data.get("missing_information", []):
            missing.append(str(item))
        if validation.data.get("prompt_injection_detected"):
            add_flag(
                policy.FLAG_PROMPT_INJECTION,
                "high",
                validation.result_id,
                "request free text attempts to override policy, approval or review controls "
                f"(techniques: {', '.join(validation.data.get('prompt_injection_techniques', []))}); "
                "the instruction was ignored and policy was applied unchanged",
            )
    if not validation:
        add_flag(
            policy.FLAG_TOOL_FAILURE,
            "high",
            None,
            "request_validation did not run, so policy section 1 compliance is unknown",
        )

    # ---------------------------------------------------------- section 2
    budget_insufficient = False
    if budget:
        verdict = budget.data.get("verdict")
        if verdict == "cannot_evaluate":
            add_flag(
                policy.FLAG_MISSING_INFORMATION,
                "warning",
                budget.result_id,
                "no annual cost stated, so the budget check could not be performed",
            )
        elif verdict == "over_budget":
            budget_insufficient = True
            add_flag(
                policy.FLAG_BUDGET_INSUFFICIENT,
                "high",
                budget.result_id,
                f"${budget.data.get('requested_annual_usd'):,.2f} requested exceeds the "
                f"{budget.data.get('department')} available budget of "
                f"${budget.data.get('available_usd'):,.2f} by "
                f"${budget.data.get('shortfall_usd'):,.2f}",
            )
    elif budget is None:
        add_flag(
            policy.FLAG_TOOL_FAILURE,
            "high",
            None,
            "budget_check did not run, so budget compliance is unknown",
        )

    # ---------------------------------------------------------- section 3
    # Overlap with an *expansion* reason is a different situation from overlap
    # with no reason, so it gets a different flag and a different severity.
    has_strong_overlap = False
    has_expansion = False
    if catalog:
        candidates = catalog.data.get("candidates", [])
        strong = [c for c in candidates if c.get("match_type") in {"same_vendor_and_product", "same_category"}]
        has_strong_overlap = bool(strong)
        has_expansion = bool(catalog.data.get("expansion_signal"))
        if has_strong_overlap:
            names = ", ".join(f"{c['product_name']} ({c['software_id']})" for c in strong[:3])
            if has_expansion:
                add_flag(
                    policy.FLAG_EXISTING_EXPANSION,
                    "info",
                    catalog.result_id,
                    f"request is an expansion of an approved product ({names}); "
                    f"{catalog.data.get('expansion_note')}",
                )
            else:
                add_flag(
                    policy.FLAG_EXISTING_OVERLAP,
                    "warning",
                    catalog.result_id,
                    f"approved catalog overlap: {names}. Policy section 3 - surface it and ask "
                    "whether the gap is credible; overlap alone is not a rejection",
                )
        for candidate in candidates:
            if candidate.get("no_purchase_record"):
                notes.append(
                    f"catalog candidate {candidate['software_id']} ({candidate['product_name']}) "
                    "is listed as approved but has no purchase order on file"
                )
    elif catalog is None:
        add_flag(
            policy.FLAG_TOOL_FAILURE,
            "high",
            None,
            "catalog_search did not run, so existing-tool overlap is unknown",
        )

    # ---------------------------------------------------------- section 5
    vendor_unavailable = False
    for result in vendor_results:
        if not result.ok:
            vendor_unavailable = True
            add_flag(
                policy.FLAG_VENDOR_UNAVAILABLE,
                "high",
                result.result_id,
                f"vendor-risk service unavailable: {result.error}. Policy section 10 - a missing "
                "check must not be read as a favourable status",
            )
    if vendor and vendor.data.get("input_missing"):
        # We never had a vendor name. That is missing information, not a lost check.
        add_flag(
            policy.FLAG_MISSING_INFORMATION,
            "warning",
            vendor.result_id,
            "no vendor name available, so vendor security/privacy status could not be checked",
        )
        missing.append(
            "vendor name is missing - without it no security, privacy or legal review can be "
            "completed"
        )
    if vendor and not vendor.data.get("input_missing"):
        verdict = vendor.data.get("security_verdict")
        source = vendor.result_id
        if verdict == "expired":
            add_flag(
                policy.FLAG_VENDOR_REVIEW_EXPIRED,
                "high",
                source,
                f"vendor security assessment expired on "
                f"{vendor.data.get('security_review_expires_on')} "
                f"(reviewed {vendor.data.get('security_review_date_effective')}, "
                f"{vendor.data.get('days_since_review')} days ago, valid "
                f"{policy.SECURITY_REVIEW_VALIDITY_DAYS} days)",
            )
        elif verdict in {"not_completed", "unknown"}:
            add_flag(
                policy.FLAG_SECURITY_REQUIRED,
                "high",
                source,
                f"vendor security assessment is {str(verdict).replace('_', ' ')}",
            )
        if vendor.data.get("material_conflict"):
            for conflict in vendor.data.get("conflicts", []):
                add_flag(
                    policy.FLAG_CONFLICTING_VENDOR,
                    "high",
                    source,
                    f"{conflict['field']}: internal registry says "
                    f"{conflict['registry_value']!r}, vendor-risk service says "
                    f"{conflict['service_value']!r}. Policy section 5 - surface, do not resolve",
                )
        if not vendor.data.get("in_registry", True):
            add_flag(
                policy.FLAG_VENDOR_UNKNOWN,
                "high",
                source,
                f"vendor {vendor.data.get('vendor_name')!r} is not in the internal registry",
            )
        if vendor.data.get("is_new_vendor"):
            add_flag(
                policy.FLAG_NEW_VENDOR,
                "info",
                source,
                "vendor is new to the registry (procurement_status=New)",
            )
        # Sensitivity triggers come from the request, resolved in the router.
    elif not vendor_results:
        add_flag(
            policy.FLAG_TOOL_FAILURE,
            "high",
            None,
            "vendor_status was never called, so vendor security/privacy status is unknown",
        )

    # ------------------------------------- sections 4/5/6/7 from the router
    approvals: list[Approval] = []
    router_applied = False
    if router:
        router_applied = True
        for record in router.data.get("approvers", []):
            approvals.append(
                Approval(
                    approver=record["approver"],
                    reason=record["reason"],
                    policy_clause=record["policy_clause"],
                )
            )
        source = router.result_id
        if router.data.get("security_review_required") and policy.FLAG_SECURITY_REQUIRED not in {
            f.flag for f in flags
        }:
            add_flag(
                policy.FLAG_SECURITY_REQUIRED,
                "high",
                source,
                "security review required: " + "; ".join(router.data.get("security_reasons", [])),
            )
        if router.data.get("privacy_review_required"):
            add_flag(
                policy.FLAG_PRIVACY_REQUIRED,
                "warning",
                source,
                "privacy review required: " + "; ".join(router.data.get("privacy_reasons", [])),
            )
        if router.data.get("legal_review_required"):
            add_flag(
                policy.FLAG_LEGAL_REQUIRED,
                "warning",
                source,
                "legal review required: " + "; ".join(router.data.get("legal_reasons", [])),
            )
        if router.data.get("approver_unresolved"):
            add_flag(
                policy.FLAG_APPROVER_UNRESOLVED,
                "high",
                source,
                "an approver named by policy section 4 could not be resolved from employees.csv; "
                "Procurement must identify them",
            )
        if router.data.get("vendor_evidence_unavailable"):
            add_flag(
                policy.FLAG_VENDOR_UNAVAILABLE,
                "high",
                source,
                f"approval routing ran without live vendor evidence: "
                f"{router.data.get('vendor_evidence_error')}",
            )
    else:
        add_flag(
            policy.FLAG_TOOL_FAILURE,
            "high",
            None,
            "approval_router did not run, so no policy threshold was applied",
        )

    # ------------------------------------------------ severity floor logic
    flag_names = {flag.flag for flag in flags}
    security_sensitive_request = _security_sensitive(router, request)

    severity = "PROCEED"
    if flag_names & _NEEDS_APPROVAL_FLAGS or missing:
        severity = max_severity(severity, "NEEDS_APPROVAL")
    if flag_names & _ESCALATE_FLAGS:
        severity = max_severity(severity, "NEEDS_APPROVAL")
    # Brief section 3.3.4: tool failure, expired vendor data or an unresolved
    # conflict force at least NEEDS_APPROVAL, and ESCALATE_TO_HUMAN for
    # security-sensitive requests.
    if (
        vendor_unavailable
        or policy.FLAG_VENDOR_REVIEW_EXPIRED in flag_names
        or policy.FLAG_CONFLICTING_VENDOR in flag_names
        or policy.FLAG_PROMPT_INJECTION in flag_names
        or budget_insufficient
        or policy.FLAG_TOOL_FAILURE in flag_names
    ):
        severity = max_severity(severity, "NEEDS_APPROVAL")
        severity = max_severity(severity, "ESCALATE_TO_HUMAN")
    if security_sensitive_request:
        severity = max_severity(severity, "NEEDS_APPROVAL")
    if policy.FLAG_SECURITY_REQUIRED in flag_names and security_sensitive_request:
        severity = max_severity(severity, "ESCALATE_TO_HUMAN")
    if approvals and len(approvals) > 1:
        severity = max_severity(severity, "NEEDS_APPROVAL")

    # ------------------------------------------- deterministic recommendation
    deterministic_recommendation = _deterministic_recommendation(
        missing=missing,
        flag_names=flag_names,
        has_strong_overlap=has_strong_overlap,
        expansion_signal=has_expansion,
        budget_insufficient=budget_insufficient,
        vendor_unavailable=vendor_unavailable,
        security_sensitive=security_sensitive_request,
    )

    if not router_applied:
        notes.append("approval_router produced no result; approvals are not code-verified")

    return CodeFindings(
        severity_floor=severity,  # type: ignore[arg-type]
        approvals=approvals,
        risk_flags=flags,
        missing_information=missing,
        deterministic_recommendation=deterministic_recommendation,
        notes=notes,
    )


def _security_sensitive(router: ToolResult | None, request: object | None) -> bool:
    """True when the request itself touches sensitive data (policy sections 5/6).

    Derived from the request's ``data_access_level`` and integrations, not from
    the vendor: a product that *can* handle PII is not the same as a request
    that *will* handle PII. An unstated level counts as sensitive because
    sensitive exposure cannot be ruled out.
    """
    level = getattr(request, "data_access_level", "") or "unknown"
    if level in policy.SECURITY_SENSITIVE_ACCESS:
        return True
    if getattr(request, "requested_integrations", None):
        if policy.integration_security_trigger(list(request.requested_integrations)):
            return True
    return False


def _deterministic_recommendation(
    *,
    missing: list[str],
    flag_names: set[str],
    has_strong_overlap: bool,
    expansion_signal: bool,
    budget_insufficient: bool,
    vendor_unavailable: bool,
    security_sensitive: bool,
) -> str:
    """The recommendation code would defend on its own.

    Used to *reject* an unsupported model recommendation, never to author one:
    the agent still chooses the label, code only blocks labels with no basis.
    """
    if missing:
        return "request_more_info"
    if (
        policy.FLAG_VENDOR_UNAVAILABLE in flag_names
        or policy.FLAG_VENDOR_REVIEW_EXPIRED in flag_names
        or policy.FLAG_CONFLICTING_VENDOR in flag_names
        or policy.FLAG_SECURITY_REQUIRED in flag_names
        or security_sensitive
    ):
        return "escalate_security_review"
    if budget_insufficient:
        return "escalate_security_review"
    if has_strong_overlap and not expansion_signal:
        return "use_existing_tool"
    return "proceed_with_approvals"


__all__ = ["CodeFindings", "compute"]

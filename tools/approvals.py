"""``approval_router`` - deterministic. Policy sections 2, 4, 5, 6, 7.

This tool *is* the policy engine. It answers three questions from data only:

1. which financial-tier approvers apply (section 4),
2. whether Security / Privacy / Legal review is required (sections 5-7),
3. which policy clauses were applied (returned as clause identifiers so the
   agent can cite them and the evaluation can check them).

Vendor facts may be supplied either by ``vendor_name`` (static registry only) or,
preferably, by ``vendor_result_id`` - a lookup key referencing an earlier
``vendor_status`` result so live service evidence is used. Same for
``budget_result_id``. The LLM cannot pass arbitrary evidence in: it can only
reference a ``result_id`` this run produced.
"""

from __future__ import annotations

from typing import Any

from core import policy
from tools.registry import ToolContext, ToolError

#: Canonical ordering so approvals_required is stable and comparable.
APPROVER_ORDER = [
    "Manager",
    "Department Head",
    "Finance",
    "CFO",
    "Procurement",
    "Security",
    "Privacy",
    "Legal",
]


def _lookup(
    registry,
    result_id: str | None,
    tool_name: str,
    allow_failed: bool = False,
):
    """Resolve a ``result_id`` from this run. A lookup key, never a data channel.

    With ``allow_failed=True`` a failed upstream result is returned as-is so the
    router can *degrade* (policy section 10: never infer a favourable status)
    instead of refusing to answer at all.
    """
    if not result_id:
        return None
    found = registry.get(result_id)
    if found is None:
        raise ToolError(f"unknown result_id {result_id!r}")
    if found.tool != tool_name:
        raise ToolError(f"result_id {result_id!r} belongs to {found.tool}, not {tool_name}")
    if not found.ok and not allow_failed:
        raise ToolError(f"cannot use failed {tool_name} result {result_id!r}: {found.error}")
    return found


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    registry = ctx.store
    request = ctx.resolve_request(args.get("request_id"))

    amount = args.get("amount_usd", request.annual_cost_usd)
    amount = None if amount in (None, "") else float(amount)
    data_access = str(
        args.get("data_access_level", request.data_access_level) or "unknown"
    ).strip().lower()
    integrations = args.get("requested_integrations")
    if not isinstance(integrations, list):
        integrations = list(request.requested_integrations)
    category = str(args.get("category", request.category) or "")

    # ---------------------------------------------------------------- vendor
    vendor_name = str(args.get("vendor_name", request.vendor_name) or "").strip()
    vendor_result = _lookup(
        ctx.registry_results,
        args.get("vendor_result_id"),
        "vendor_status",
        allow_failed=True,
    )
    vendor_failed: bool = bool(vendor_result and not vendor_result.ok)
    vendor_data: dict[str, Any] = vendor_result.data if (vendor_result and vendor_result.ok) else {}
    if not vendor_name:
        vendor_name = str(vendor_data.get("vendor_name") or "")

    static_vendor = registry.vendor(vendor_name)
    is_new_vendor = bool(
        vendor_data.get("is_new_vendor")
        if vendor_data
        else (static_vendor and static_vendor.is_new)
    )
    legal_terms_status = (
        vendor_data.get("legal_terms_status")
        if vendor_data
        else (static_vendor.legal_terms_status if static_vendor else None)
    )
    if legal_terms_status is None:
        legal_terms_status = "Unknown"

    # Registry-only evidence, used when no live vendor_status result was supplied.
    registry_review_date = policy.parse_date(
        static_vendor.security_review_date if static_vendor else None
    )
    registry_review_current = policy.is_review_current(registry_review_date)

    approvers: dict[str, list[str]] = {}
    approver_clauses: dict[str, list[str]] = {}
    clauses: list[str] = []

    def require(approver: str, reason: str, clause: str) -> None:
        """Register an approver. Reasons and clauses accumulate, never overwrite.

        One approver can be required by several rules (Finance by both the spend
        tier and a budget-exception review); every reason is kept so the requester
        and the human reviewer see the complete basis.
        """
        reasons = approvers.setdefault(approver, [])
        if reason and reason not in reasons:
            reasons.append(reason)
        clause_list = approver_clauses.setdefault(approver, [])
        if clause not in clause_list:
            clause_list.append(clause)
        if clause not in clauses:
            clauses.append(clause)

    # ------------------------------------------------- section 4: financial
    tier = policy.financial_tier(amount)
    if amount is None:
        financial_note = (
            "annual cost is not stated, so no financial tier applies; the request "
            "is not ready for approval (section 1)"
        )
        clauses.append("P1-REQUIRED-INFO")
        # Section 4 cannot be applied, so Procurement owns getting the request
        # to a state where it can be tiered. Never guess a tier.
        require(
            "Procurement",
            "annual cost is unstated, so the section 4 approval tier cannot be "
            "determined; Procurement must obtain the cost before any approval can proceed",
            "P1-REQUIRED-INFO",
        )
    else:
        financial_note = f"annual amount ${amount:,.2f} falls in tier {tier}"
        for approver, clause in policy.financial_approvers(amount):
            require(approver, financial_note, clause)

    # ------------------------------------------------- section 2: budget
    budget_result = _lookup(ctx.registry_results, args.get("budget_result_id"), "budget_check")
    budget_data = budget_result.data if budget_result else {}
    budget_insufficient = budget_data.get("fits_within_available") is False
    if budget_insufficient:
        require(
            "Finance",
            f"annual cost exceeds the {budget_data.get('department')} available budget by "
            f"${budget_data.get('shortfall_usd'):,.2f}; policy section 2 routes over-budget "
            "requests for a Finance / budget exception review",
            "P2-BUDGET",
        )

    # ------------------------------------------------- section 5: security
    security_reasons: list[str] = []
    if data_access in policy.DATA_ACCESS_SECURITY:
        security_reasons.append(
            f"data access level '{data_access}' means {policy.DATA_ACCESS_SECURITY[data_access]}"
        )
    integration_trigger = policy.integration_security_trigger(integrations)
    if integration_trigger:
        security_reasons.append(f"requested integration implies {integration_trigger[0]}")
    if vendor_data:
        verdict = vendor_data.get("security_verdict")
        if verdict == "expired":
            security_reasons.append(
                f"vendor security assessment expired "
                f"(review {vendor_data.get('security_review_date_effective')}, "
                f"{vendor_data.get('days_since_review')} days ago, valid "
                f"{policy.SECURITY_REVIEW_VALIDITY_DAYS} days)"
            )
        elif verdict in {"not_completed", "unknown"}:
            security_reasons.append(
                f"vendor security assessment is {str(verdict).replace('_', ' ')}"
            )
        if vendor_data.get("material_conflict"):
            security_reasons.append(
                "internal vendor registry and the vendor-risk service disagree on security "
                "status; policy section 5 requires the conflict to be surfaced, not resolved"
            )
        if not vendor_data.get("in_registry", True):
            security_reasons.append(
                "vendor is not present in the internal vendor registry, so no security "
                "assessment can be confirmed"
            )
    elif vendor_failed:
        security_reasons.append(
            f"vendor evidence for {vendor_name!r} could not be retrieved "
            f"({vendor_result.error}); policy section 10 forbids inferring a favourable status"
        )
    elif static_vendor is None:
        # No live evidence *and* no registry row: nothing can be confirmed.
        security_reasons.append(
            f"vendor {vendor_name!r} is not in the internal registry, so its security "
            "assessment cannot be confirmed"
        )
    # else: registry row exists but no live evidence was supplied.
    elif not registry_review_current:
        security_reasons.append(
            f"registry security review for {vendor_name!r} is dated "
            f"{static_vendor.security_review_date!r}, outside the "
            f"{policy.SECURITY_REVIEW_VALIDITY_DAYS}-day validity window"
            if registry_review_date is None
            else f"registry security review for {vendor_name!r} expired on "
            f"{policy.review_expiry_date(registry_review_date)}"
        )
    if static_vendor is not None and str(static_vendor.security_status).strip().lower() in {
        "pending",
        "unknown",
    }:
        security_reasons.append(
            f"registry security_status for {vendor_name!r} is "
            f"{static_vendor.security_status!r}, so no completed assessment is on record"
        )
    if security_reasons:
        require(
            "Security",
            "; ".join(security_reasons),
            "P5-SECURITY",
        )

    # ------------------------------------------------- section 6: privacy
    privacy_reasons: list[str] = []
    if data_access in policy.DATA_ACCESS_PRIVACY:
        privacy_reasons.append(
            f"data access level '{data_access}' means {policy.DATA_ACCESS_PRIVACY[data_access]}"
        )
    if vendor_data.get("stores_data_outside_region"):
        privacy_reasons.append(
            "vendor-risk service records that data may be stored outside the operating region"
        )
    if vendor_data.get("processes_personal_data") and data_access == "unknown":
        privacy_reasons.append(
            "vendor-risk service records that the product processes personal data and the "
            "request does not state its data-access level, so PII handling cannot be ruled out"
        )
    if data_access == "unknown":
        privacy_reasons.append(
            "intended data-access level is unstated; policy section 6 cannot be ruled in or out"
        )
    if privacy_reasons:
        require("Privacy", "; ".join(privacy_reasons), "P6-PRIVACY")

    # ------------------------------------------------- section 7: legal
    legal_reasons: list[str] = []
    if is_new_vendor and amount is not None and amount >= policy.NEW_VENDOR_LEGAL_THRESHOLD_USD:
        legal_reasons.append(
            f"vendor is new and annual spend ${amount:,.2f} is at or above "
            f"${policy.NEW_VENDOR_LEGAL_THRESHOLD_USD:,}"
        )
    if str(legal_terms_status).strip().lower() != "approved":
        legal_reasons.append(
            f"legal terms status is {legal_terms_status!r}, not already approved/standard"
        )
    if vendor_data.get("stores_data_outside_region"):
        legal_reasons.append(
            "a material cross-region data-processing issue was identified "
            "(stores data outside the operating region)"
        )
    if vendor_failed:
        legal_reasons.append(
            f"vendor onboarding status for {vendor_name!r} could not be verified "
            f"({vendor_result.error}), so standard legal terms cannot be assumed"
        )
    elif static_vendor is None:
        legal_reasons.append(
            "vendor is not in the registry, so no standard legal terms can be confirmed"
        )
    if legal_reasons:
        require("Legal", "; ".join(legal_reasons), "P7-LEGAL")

    # ------------------------------------------------- approver resolution
    from tools.employee import department_head

    employee = registry.employee(request.requester_id)
    head: dict[str, Any] | None = None
    manager_name = None
    approver_unresolved = False
    if employee is not None:
        head = department_head(ctx, employee)
        manager_name = employee.manager_id and employee.name
    else:
        approver_unresolved = True

    if "Manager" in approvers and amount is not None:
        require(
            "Manager",
            "line manager approval is the minimum tier at this spend level"
            + (f" ({manager_name}, {employee.employee_id})" if employee else ""),
            "P4-FINANCIAL-THRESHOLDS",
        )
    if "Department Head" in approvers:
        if head and head.get("resolved"):
            require(
                "Department Head",
                f"{head['name']} ({head['employee_id']}) heads {employee.department}; "
                "financial approval required at this spend level",
                "P4-FINANCIAL-THRESHOLDS",
            )
        else:
            require(
                "Department Head",
                (head or {}).get("basis")
                or "department head could not be resolved from employees.csv",
                "P4-FINANCIAL-THRESHOLDS",
            )
            approver_unresolved = True
    if approver_unresolved:
        clauses.append("P4-FINANCIAL-THRESHOLDS")

    ordered = sorted(
        approvers.keys(),
        key=lambda name: APPROVER_ORDER.index(name)
        if name in APPROVER_ORDER
        else len(APPROVER_ORDER),
    )
    approver_records = [
        {
            "approver": name,
            "reason": " | ".join(approvers[name]),
            "reasons": list(approvers[name]),
            "policy_clause": approver_clauses[name][0],
            "policy_clauses": list(approver_clauses[name]),
        }
        for name in ordered
    ]

    return {
        "amount_usd": amount,
        "financial_tier": tier,
        "financial_note": financial_note,
        "required_approvals": ordered,
        "approvers": approver_records,
        "security_review_required": "Security" in approvers,
        "privacy_review_required": "Privacy" in approvers,
        "legal_review_required": "Legal" in approvers,
        "budget_insufficient": budget_insufficient,
        "security_reasons": security_reasons,
        "privacy_reasons": privacy_reasons,
        "legal_reasons": legal_reasons,
        "clauses_applied": clauses or ["P1-REQUIRED-INFO"],
        "vendor_name": vendor_name,
        "is_new_vendor": is_new_vendor,
        "legal_terms_status": legal_terms_status,
        "approver_unresolved": approver_unresolved,
        "vendor_evidence_unavailable": vendor_failed,
        "vendor_evidence_error": vendor_result.error if vendor_failed else None,
        "department": employee.department if employee else None,
        "policy_clauses_available": sorted(policy.CLAUSES),
        "as_of": policy.as_of_iso(),
    }

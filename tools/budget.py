"""``budget_check`` - deterministic. Policy section 2.

Reads ``department_budgets.csv`` only. ``committed_usd`` cannot be derived from
``purchase_history.csv`` (ISS-02), so purchase history is never used for budget
arithmetic.

If the amount is unknown the tool returns ``verdict="cannot_evaluate"`` rather
than failing: a missing number is missing information, not a broken tool.
"""

from __future__ import annotations

from typing import Any

from core import policy
from tools.registry import ToolContext, ToolError


def _to_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ToolError(f"annual_cost_usd is not a number: {value!r}") from exc


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    store = ctx.store

    department = args.get("department")
    if department:
        employee = None
    elif args.get("employee_id"):
        employee = store.employee(str(args["employee_id"]).strip())
        if employee is None:
            raise ToolError(f"unknown employee_id {args['employee_id']!r}")
        department = employee.department
    else:
        request = ctx.require_request()
        employee = store.employee(request.requester_id)
        department = employee.department if employee else None

    amount = _to_amount(args.get("amount_usd"))
    if amount is None and args.get("amount_usd") is None and ctx.request is not None:
        amount = _to_amount(ctx.request.annual_cost_usd)

    budget = store.budget(department)
    if budget is None:
        raise ToolError(
            f"no budget row for department {department!r}; cannot evaluate this request"
        )

    payload: dict[str, Any] = {
        "department": budget.department,
        "requester_department": department,
        "annual_software_budget_usd": budget.annual_software_budget_usd,
        "committed_usd": budget.committed_usd,
        "available_usd": budget.available_usd,
        "requested_annual_usd": amount,
        "source_of_truth": "data/department_budgets.csv",
        "arithmetic_check": (
            budget.annual_software_budget_usd - budget.committed_usd == budget.available_usd
        ),
        "policy_clause": "P2-BUDGET",
        "as_of": policy.as_of_iso(),
    }

    if amount is None:
        payload.update(
            {
                "verdict": "cannot_evaluate",
                "fits_within_available": None,
                "shortfall_usd": None,
                "remaining_after_usd": None,
                "utilisation_pct": None,
                "reason": "annual cost is not stated, so no budget decision is possible",
            }
        )
        return payload

    fits = amount <= budget.available_usd
    payload.update(
        {
            "verdict": "within_budget" if fits else "over_budget",
            "fits_within_available": fits,
            "shortfall_usd": 0.0 if fits else round(amount - budget.available_usd, 2),
            "remaining_after_usd": round(budget.available_usd - amount, 2),
            "utilisation_pct": round(100.0 * amount / budget.available_usd, 1)
            if budget.available_usd
            else None,
            "reason": (
                f"{policy.FLAG_BUDGET_INSUFFICIENT if not fits else 'within available budget'}"
            ),
        }
    )
    return payload

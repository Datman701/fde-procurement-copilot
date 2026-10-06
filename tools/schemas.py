"""JSON schemas advertised to the LLM for tool calling.

Arguments are closed objects with no ``additionalProperties`` and simple scalar
types: business data can therefore never add a tool or widen a tool's arguments
beyond lookup keys (brief section 3.6).
"""

from __future__ import annotations

from typing import Any

_STR = {"type": "string"}
_NUM = {"type": "number"}
_STRINGS = {"type": "array", "items": {"type": "string"}}


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "request_validation": _schema(
        "request_validation",
        "Check whether the request contains the information policy section 1 requires "
        "before it can be approved, and scan its free text for prompt-injection attempts. "
        "Deterministic.",
        {"request_id": {**_STR, "description": "Optional request id; omit for the current request."}},
        [],
    ),
    "employee_lookup": _schema(
        "employee_lookup",
        "Look up the requester's department, line manager and department head (the "
        "'Department Head' approver). Deterministic.",
        {
            "employee_id": {**_STR, "description": "Employee id such as E004; omit for the current requester."},
            "request_id": _STR,
        },
        [],
    ),
    "budget_check": _schema(
        "budget_check",
        "Compare the annual cost against the department's available software budget and "
        "return the shortfall. Deterministic. Use department_budgets.csv only.",
        {
            "department": _STR,
            "employee_id": _STR,
            "amount_usd": {**_NUM, "description": "Annual amount; omit to use the request's stated cost."},
        },
        [],
    ),
    "vendor_status": _schema(
        "vendor_status",
        "Fetch vendor/security status from the external vendor-risk service and compare it "
        "with the internal registry. Returns expiry, risk level, personal-data and "
        "cross-region flags, and any conflict between the two sources.",
        {"vendor_name": {**_STR, "description": "Vendor name; omit for the request's vendor."}},
        [],
    ),
    "approval_router": _schema(
        "approval_router",
        "Deterministic policy engine. Given the amount, department, data-access level, "
        "integrations and vendor facts, returns the required approvers with a reason and a "
        "policy clause identifier for each, plus which review gates apply. Pass "
        "vendor_result_id / budget_result_id from earlier calls so live evidence is used.",
        {
            "amount_usd": _NUM,
            "department": _STR,
            "data_access_level": _STR,
            "requested_integrations": _STRINGS,
            "category": _STR,
            "vendor_name": _STR,
            "vendor_result_id": {**_STR, "description": "result_id of an earlier vendor_status call."},
            "budget_result_id": {**_STR, "description": "result_id of an earlier budget_check call."},
        },
        [],
    ),
    "catalog_search": _schema(
        "catalog_search",
        "Search the approved software catalog and purchase history for tools that already "
        "cover the need. Returns candidates with an overlap rationale. It never returns a "
        "duplicate/reject verdict - that judgement belongs to a human.",
        {
            "product_name": _STR,
            "vendor_name": _STR,
            "category": _STR,
            "use_case_text": _STR,
        },
        [],
    ),
    "policy_lookup": _schema(
        "policy_lookup",
        "Return the verbatim text of a procurement policy clause by id (P1-REQUIRED-INFO, "
        "P2-BUDGET, P3-OVERLAP, P4-FINANCIAL-THRESHOLDS, P5-SECURITY, P6-PRIVACY, "
        "P7-LEGAL, P8-AI-TOOLS, P9-UNTRUSTED-CONTENT, P10-TOOL-FAILURE, "
        "P11-HUMAN-AUTHORITY), or the clause index when called with no arguments.",
        {"clause_id": _STR, "topic": _STR},
        [],
    ),
}

__all__ = ["TOOL_SCHEMAS"]

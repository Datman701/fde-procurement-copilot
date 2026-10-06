"""``request_validation`` - deterministic check of policy section 1.

Required information: requester, product/vendor, annual cost, number of
users/licenses, business purpose, intended data-access level, required
integrations (an empty list means *none required*, see ISS-07).
"""

from __future__ import annotations

from typing import Any

from core import policy
from tools.registry import ToolContext

FIELD_LABELS = {
    "requester_id": "requester",
    "product_name": "product",
    "vendor_name": "vendor",
    "annual_cost_usd": "annual cost",
    "user_count": "number of users / licenses",
    "business_justification": "business purpose",
    "data_access_level": "intended data-access level",
}


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    request = ctx.resolve_request(args.get("request_id"))

    missing_fields = request.missing_fields()
    human_missing = [
        FIELD_LABELS.get(field, field) for field in missing_fields
    ]
    missing_information = [
        f"{label} is missing or unstated - policy section 1 requires it before approval"
        for label in human_missing
    ]

    injection_techniques: list[str] = []
    from core.data import detect_injection, untrusted_fields

    for _field, value in untrusted_fields(request).items():
        injection_techniques.extend(detect_injection(value))
    injection_techniques = sorted(set(injection_techniques))

    return {
        "request_id": request.request_id,
        "complete": not missing_fields,
        "missing_fields": missing_fields,
        "missing_information": missing_information,
        "integrations_required": len(request.requested_integrations),
        "policy_clause": "P1-REQUIRED-INFO",
        "prompt_injection_detected": bool(injection_techniques),
        "prompt_injection_techniques": injection_techniques,
        "urgency": request.urgency,
        "as_of": policy.as_of_iso(),
    }

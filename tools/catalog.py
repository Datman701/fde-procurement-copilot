"""``catalog_search`` - does an approved tool already solve the need?

Returns **candidates with an overlap rationale**, never a verdict. The brief is
explicit: never auto-conclude "duplicate" in code when the match is fuzzy, and
policy section 3 says overlap is not an automatic rejection.

Each candidate carries:
* ``match_type`` - ``same_vendor_and_product`` / ``same_vendor`` / ``same_category`` / ``same_scope``
* ``overlap_rationale`` - a sentence the agent may quote as evidence
* ``expansion_signal`` - whether the request text itself states an expansion /
  renewal / additional-seat reason (deterministic keyword rule)
* ``no_purchase_record`` - listed in the catalog but no purchase order exists (ISS-03)

All catalog ``notes`` fields are free text and therefore untrusted data.
"""

from __future__ import annotations

import re
from typing import Any

from core import policy
from tools.registry import ToolContext

#: Phrases that mean "more of the thing we already buy", not a new purchase.
#: Deterministic keyword rule - the human still judges whether it is credible.
EXPANSION_PATTERNS = (
    r"\bexpand(?:ing|s|ed)?\b",
    r"\bexpan\w+\s+to\b",
    r"\brenew(?:al|ing|s)?\b",
    r"\badditional\s+\w+",
    r"\bmore\s+(?:seat|user|licen|capacity)",
    r"\badd(?:ing)?\s+(?:seat|user|licen|squad|team)",
    r"\bextend(?:ing|s)?\b",
    r"\bsecond\s+(?:squad|team|wave|phase)",
    r"\bupgrade\b",
    r"\broll(?:ed)?\s+out\b",
)

_STOPWORDS = {
    "and", "for", "with", "the", "a", "an", "of", "to", "pro", "add", "add",
    "on", "pack", "suite", "team", "enterprise", "business", "tool",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _expansion_signal(text: str) -> tuple[bool, str | None]:
    lowered = (text or "").lower()
    for pattern in EXPANSION_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return True, f"request text states an expansion/renewal reason: '{match.group(0)}'"
    return False, None


def _overlap_rationale(item, request, match_type: str, expansion: bool, expansion_note) -> str:
    if match_type == "same_vendor_and_product":
        base = (
            f"{item.product_name} ({item.software_id}) from {item.vendor_name} is already the "
            f"approved product for this request's vendor and category "
            f"({item.category}, {item.status}, {item.licensed_seats} seats, scope {item.scope})"
        )
        if expansion:
            return base + f". " + (expansion_note or "") + " - treat as an expansion, not a duplicate."
        return base + ". The request does not state an expansion or capacity reason."
    if match_type == "same_vendor":
        base = (
            f"{item.product_name} ({item.software_id}) from {item.vendor_name} is an approved "
            f"product from the same vendor, in a different category ({item.category})"
        )
        return base + "; check whether the existing product covers the stated use case."
    if match_type == "same_category":
        base = (
            f"{item.product_name} ({item.software_id}, {item.vendor_name}) is approved in the "
            f"same category '{item.category}' with {item.licensed_seats} seats "
            f"(scope {item.scope}, notes: {item.notes or 'none'})"
        )
        return base + ". Policy section 3: overlap is not an automatic rejection - the requester must state a credible gap."
    return (
        f"{item.product_name} ({item.software_id}) is a company-scope approved product "
        f"({item.category}) that may satisfy a similar need"
    )


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    store = ctx.store
    request = ctx.resolve_request(args.get("request_id"))

    product_name = str(args.get("product_name", request.product_name) or "")
    vendor_name = str(args.get("vendor_name", request.vendor_name) or "")
    category = str(args.get("category", request.category) or "")
    use_case_text = str(
        args.get("use_case_text", request.business_justification) or ""
    )

    expansion, expansion_note = _expansion_signal(use_case_text)
    request_tokens = _tokens(product_name) | _tokens(use_case_text)
    purchase_by_vendor: dict[str, list] = {}
    for purchase in store.purchases:
        purchase_by_vendor.setdefault(purchase.vendor_name.lower(), []).append(purchase)

    candidates: list[dict[str, Any]] = []
    for item in store.catalog:
        same_product = item.product_name.lower() == product_name.lower()
        same_vendor = item.vendor_name.lower() == vendor_name.lower()
        same_category = bool(category) and item.category.lower() == category.lower()
        token_overlap = (
            len(request_tokens & _tokens(item.product_name) | (request_tokens & _tokens(item.notes or "")))
            if item.notes
            else len(request_tokens & _tokens(item.product_name))
        )

        if same_product:
            match_type = "same_vendor_and_product"
        elif same_vendor and same_category:
            # Same vendor *and* same category: the request is for that vendor's
            # product in this space, e.g. "TaskFlow Pro" against catalog "TaskFlow".
            # Treated as a strong overlap, not a separate product.
            match_type = "same_vendor_and_product"
        elif same_vendor:
            match_type = "same_vendor"
        elif same_category:
            match_type = "same_category"
        elif item.scope.lower() == "company-wide" and token_overlap > 0:
            match_type = "same_scope"
        else:
            continue

        purchases = purchase_by_vendor.get(item.vendor_name.lower(), [])
        candidates.append(
            {
                "software_id": item.software_id,
                "product_name": item.product_name,
                "category": item.category,
                "vendor_name": item.vendor_name,
                "status": item.status,
                "base_status": item.base_status,
                "restricted_use": item.restricted_use,
                "annual_cost_usd": item.annual_cost_usd,
                "licensed_seats": item.licensed_seats,
                "scope": item.scope,
                "notes": item.notes,
                "match_type": match_type,
                "match_strength": "strong" if match_type != "same_scope" else "weak",
                "token_overlap": int(token_overlap),
                "overlap_rationale": _overlap_rationale(
                    item, request, match_type, expansion, expansion_note
                ),
                "purchase_orders": [p.purchase_id for p in purchases],
                "no_purchase_record": not purchases,
                "procurement_evidence": "purchase_order" if purchases else "catalog_only",
            }
        )

    rank = {
        "same_vendor_and_product": 0,
        "same_vendor": 1,
        "same_category": 2,
        "same_scope": 3,
    }
    candidates.sort(key=lambda c: rank[c["match_type"]])

    substitution = [c for c in candidates if c["match_type"] in {"same_category", "same_scope"}]
    same_product = [c for c in candidates if c["match_type"] == "same_vendor_and_product"]

    return {
        "request_id": request.request_id,
        "query": {
            "product_name": product_name,
            "vendor_name": vendor_name,
            "category": category,
            "use_case_text": use_case_text,
        },
        "search_performed": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "strongest_match_type": candidates[0]["match_type"] if candidates else None,
        "has_substitution_candidate": bool(substitution),
        "has_same_product_candidate": bool(same_product),
        "expansion_signal": expansion,
        "expansion_note": expansion_note,
        "verdict": None,
        "verdict_reason": (
            "code never concludes 'duplicate' from a fuzzy match; policy section 3 "
            "requires the existing option to be surfaced and the gap argued by a human"
        ),
        "policy_clause": "P3-OVERLAP",
        "as_of": policy.as_of_iso(),
    }

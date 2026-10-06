"""``policy_lookup`` - returns the verbatim policy text for a clause.

Used by the agent so it can cite the real clause rather than paraphrase policy
from memory, and by ``policy_lookup`` unit tests to prove the clause identifiers
in :mod:`core.policy` still resolve to real sections of
``data/procurement_policy.md`.
"""

from __future__ import annotations

from typing import Any

from core import policy
from tools.registry import ToolContext, ToolError

_TOPICS = {
    "budget": "P2-BUDGET",
    "overlap": "P3-OVERLAP",
    "duplicate": "P3-OVERLAP",
    "existing": "P3-OVERLAP",
    "threshold": "P4-FINANCIAL-THRESHOLDS",
    "approval": "P4-FINANCIAL-THRESHOLDS",
    "security": "P5-SECURITY",
    "privacy": "P6-PRIVACY",
    "legal": "P7-LEGAL",
    "ai": "P8-AI-TOOLS",
    "injection": "P9-UNTRUSTED-CONTENT",
    "untrusted": "P9-UNTRUSTED-CONTENT",
    "failure": "P10-TOOL-FAILURE",
    "outage": "P10-TOOL-FAILURE",
    "human": "P11-HUMAN-AUTHORITY",
    "missing": "P1-REQUIRED-INFO",
    "required information": "P1-REQUIRED-INFO",
}


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    clause_id = args.get("clause_id")
    topic = args.get("topic")

    if not clause_id and topic:
        clause_id = _TOPICS.get(str(topic).strip().lower())

    if not clause_id:
        # No argument: return the compact clause index, not the whole policy.
        return {
            "clause_index": [
                {"clause_id": cid, **meta} for cid, meta in sorted(policy.CLAUSES.items())
            ],
            "financial_thresholds": {
                "up_to_1000": ["Manager"],
                "1000_01_to_10000": ["Department Head", "Procurement"],
                "10000_01_to_25000": ["Department Head", "Finance", "Procurement"],
                "above_25000": ["Department Head", "Finance", "CFO", "Procurement"],
            },
            "security_review_validity_days": policy.SECURITY_REVIEW_VALIDITY_DAYS,
            "policy_version": policy.POLICY_VERSION,
            "reference_date": policy.REFERENCE_DATE.isoformat(),
            "as_of": policy.as_of_iso(),
        }

    clause_id = str(clause_id).strip().upper()
    if clause_id not in policy.CLAUSES:
        raise ToolError(
            f"unknown clause_id {clause_id!r}; valid ids: {sorted(policy.CLAUSES)}"
        )
    text = policy.clause_text(clause_id)
    if text is None:
        raise ToolError(f"clause {clause_id} has no text in the policy file")
    return {
        "clause_id": clause_id,
        **policy.CLAUSES[clause_id],
        "text": text,
        "policy_version": policy.POLICY_VERSION,
        "as_of": policy.as_of_iso(),
    }

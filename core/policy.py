"""Deterministic policy engine.

Every threshold, boundary and clause identifier used anywhere in the product is
defined here, transcribed from ``data/procurement_policy.md`` (version 2026.09).
The LLM never reads this module's decisions as advice - it reads the *results*
and explains them.

Reference date is fixed by the policy (2026-09-30) and is deliberately **not**
the wall clock (brief: "Use the data snapshot / policy reference date ... do not
depend on the computer's current date").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "data" / "procurement_policy.md"

# --------------------------------------------------------------------------- #
# Fixed reference date
# --------------------------------------------------------------------------- #
POLICY_REFERENCE_DATE = date(2026, 9, 30)
POLICY_VERSION = "2026.09"

SECURITY_REVIEW_VALIDITY_DAYS = 365
NEW_VENDOR_LEGAL_THRESHOLD_USD = 10_000


def _parse_reference_date() -> date:
    """Read the reference date out of the policy file so it cannot drift."""
    text = POLICY_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"Data snapshot / evaluation reference date:\*\*\s*(\d{4}-\d{2}-\d{2})", text
    )
    if not match:
        return POLICY_REFERENCE_DATE
    return date.fromisoformat(match.group(1))


REFERENCE_DATE = _parse_reference_date()


# --------------------------------------------------------------------------- #
# Clauses
# --------------------------------------------------------------------------- #
CLAUSES: dict[str, dict[str, str]] = {
    "P1-REQUIRED-INFO": {"section": "1", "title": "Required request information"},
    "P2-BUDGET": {"section": "2", "title": "Budget check"},
    "P3-OVERLAP": {"section": "3", "title": "Existing software / overlap"},
    "P4-FINANCIAL-THRESHOLDS": {"section": "4", "title": "Financial approval thresholds"},
    "P5-SECURITY": {"section": "5", "title": "Security review"},
    "P6-PRIVACY": {"section": "6", "title": "Privacy review"},
    "P7-LEGAL": {"section": "7", "title": "Legal review"},
    "P8-AI-TOOLS": {"section": "8", "title": "AI tools"},
    "P9-UNTRUSTED-CONTENT": {"section": "9", "title": "Untrusted content and prompt injection"},
    "P10-TOOL-FAILURE": {"section": "10", "title": "Tool and evidence failures"},
    "P11-HUMAN-AUTHORITY": {"section": "11", "title": "Human authority"},
}


def policy_sections() -> dict[str, str]:
    """Split procurement_policy.md into its numbered sections (verbatim text)."""
    text = POLICY_PATH.read_text(encoding="utf-8")
    parts: dict[str, str] = {}
    pattern = re.compile(r"^##\s+(\d+)\.\s+(.*?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        parts[match.group(1)] = text[match.start() : end].strip()
    return parts


def clause_text(clause_id: str) -> str | None:
    section = CLAUSES.get(clause_id, {}).get("section")
    if section is None:
        return None
    return policy_sections().get(section)


# --------------------------------------------------------------------------- #
# Financial approval thresholds  (policy section 4, verbatim)
# --------------------------------------------------------------------------- #
#   Up to $1,000            -> Manager
#   $1,000.01 - $10,000     -> Department Head + Procurement
#   $10,000.01 - $25,000    -> Department Head + Finance + Procurement
#   Above $25,000           -> Department Head + Finance + CFO + Procurement
TIER_MANAGER_MAX_USD = 1_000
TIER_DEPT_PROC_MAX_USD = 10_000
TIER_DEPT_FIN_PROC_MAX_USD = 25_000


def financial_approvers(amount_usd: float | None) -> list[tuple[str, str]]:
    """Return ``(approver, clause)`` pairs for the financial tier of ``amount_usd``.

    Boundaries are inclusive-upper, exactly as the policy table states:
    ``<=1000`` is Manager, ``<=10000`` is Dept Head + Procurement,
    ``<=25000`` adds Finance, above that adds CFO.
    """
    if amount_usd is None:
        return []
    if amount_usd <= TIER_MANAGER_MAX_USD:
        return [("Manager", "P4-FINANCIAL-THRESHOLDS")]
    if amount_usd <= TIER_DEPT_PROC_MAX_USD:
        return [
            ("Department Head", "P4-FINANCIAL-THRESHOLDS"),
            ("Procurement", "P4-FINANCIAL-THRESHOLDS"),
        ]
    if amount_usd <= TIER_DEPT_FIN_PROC_MAX_USD:
        return [
            ("Department Head", "P4-FINANCIAL-THRESHOLDS"),
            ("Finance", "P4-FINANCIAL-THRESHOLDS"),
            ("Procurement", "P4-FINANCIAL-THRESHOLDS"),
        ]
    return [
        ("Department Head", "P4-FINANCIAL-THRESHOLDS"),
        ("Finance", "P4-FINANCIAL-THRESHOLDS"),
        ("CFO", "P4-FINANCIAL-THRESHOLDS"),
        ("Procurement", "P4-FINANCIAL-THRESHOLDS"),
    ]


def financial_tier(amount_usd: float | None) -> str:
    if amount_usd is None:
        return "undetermined"
    if amount_usd <= TIER_MANAGER_MAX_USD:
        return "up_to_1000"
    if amount_usd <= TIER_DEPT_PROC_MAX_USD:
        return "1000_01_to_10000"
    if amount_usd <= TIER_DEPT_FIN_PROC_MAX_USD:
        return "10000_01_to_25000"
    return "above_25000"


# --------------------------------------------------------------------------- #
# Data-access sensitivity (derived from policy sections 5 and 6)
# --------------------------------------------------------------------------- #
# Triggers taken from the literal trigger list in section 5/6 of the policy.
DATA_ACCESS_SECURITY: dict[str, str] = {
    "source_code": "source code access (section 5)",
    "production_telemetry": "production or cloud-account integration (section 5)",
    "confidential_documents": "confidential documents (section 5)",
    "customer_pii": "customer PII (section 5)",
    "employee_pii": "employee PII (section 5)",
    "unknown": "data access level not stated, sensitive exposure cannot be ruled out (section 5)",
}

DATA_ACCESS_PRIVACY: dict[str, str] = {
    "customer_pii": "processes customer PII (section 6)",
    "employee_pii": "processes employee PII (section 6)",
}

#: Data-access levels that carry no security/privacy trigger.
DATA_ACCESS_NONE: frozenset[str] = frozenset(
    {"none", "internal_documents", "internal_marketing", "public", "anonymous"}
)

#: Integration keywords that satisfy "production or cloud-account integration",
#: "source code access", "credentials/secrets" etc. (section 5).
#: Ordered longest-phrase-first so the most specific match is reported
#: ("cloud account" before the bare "cloud").
INTEGRATION_SECURITY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("cloud account", "cloud-account integration (section 5)"),
    ("cloud", "cloud-account integration (section 5)"),
    ("git repo", "source code access (section 5)"),
    ("source code", "source code access (section 5)"),
    ("repository", "confidential documents / source code access (section 5)"),
    ("document repo", "confidential documents (section 5)"),
    ("production", "production integration (section 5)"),
    ("credential", "credentials/secrets (section 5)"),
    ("api key", "credentials/secrets (section 5)"),
    ("secret", "credentials/secrets (section 5)"),
)


def integration_security_trigger(integrations: list[str] | None) -> tuple[str, str] | None:
    """First integration that triggers security review, as ``(trigger, clause)``."""
    for item in integrations or []:
        lowered = str(item).lower()
        for keyword, trigger in INTEGRATION_SECURITY_KEYWORDS:
            if keyword in lowered:
                return trigger, "P5-SECURITY"
    return None


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def parse_date(value: str | None) -> date | None:
    """Parse an ISO date. Empty string, ``None`` and malformed input all -> None.

    An absent review date is *missing evidence*, never "still valid" (ISS-04).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None


def review_expiry_date(review_date: date | None) -> date | None:
    if review_date is None:
        return None
    return review_date + timedelta(days=SECURITY_REVIEW_VALIDITY_DAYS)


def is_review_current(review_date: date | None, as_of: date | None = None) -> bool:
    """A security assessment is current for 365 days from its review date.

    ``None`` review date -> **not** current (policy section 5 lists "missing"
    as a trigger).
    """
    as_of = as_of or REFERENCE_DATE
    expiry = review_expiry_date(review_date)
    if expiry is None:
        return False
    return as_of <= expiry


def days_since(review_date: date | None, as_of: date | None = None) -> int | None:
    if review_date is None:
        return None
    return ((as_of or REFERENCE_DATE) - review_date).days


# --------------------------------------------------------------------------- #
# Risk-flag vocabulary (policy "suggested risk flags" + section 1/3 additions)
# --------------------------------------------------------------------------- #
FLAG_BUDGET_INSUFFICIENT = "budget_insufficient"
FLAG_EXISTING_OVERLAP = "existing_tool_overlap"
FLAG_EXISTING_EXPANSION = "existing_tool_expansion"
FLAG_SECURITY_REQUIRED = "security_review_required"
FLAG_VENDOR_REVIEW_EXPIRED = "vendor_review_expired"
FLAG_CONFLICTING_VENDOR = "conflicting_vendor_evidence"
FLAG_PRIVACY_REQUIRED = "privacy_review_required"
FLAG_LEGAL_REQUIRED = "legal_review_required"
FLAG_PROMPT_INJECTION = "prompt_injection_detected"
FLAG_MISSING_INFORMATION = "missing_information"
FLAG_VENDOR_UNAVAILABLE = "vendor_risk_unavailable"
FLAG_VENDOR_UNKNOWN = "vendor_not_in_registry"
FLAG_APPROVER_UNRESOLVED = "approver_unresolved"
FLAG_MODEL_UNAVAILABLE = "model_unavailable"
FLAG_TOOL_FAILURE = "tool_failure"
FLAG_NEW_VENDOR = "new_vendor"

#: Flags that by themselves force at least NEEDS_APPROVAL (brief section 3.3.4).
ESCALATING_FLAGS: frozenset[str] = frozenset(
    {
        FLAG_SECURITY_REQUIRED,
        FLAG_VENDOR_REVIEW_EXPIRED,
        FLAG_CONFLICTING_VENDOR,
        FLAG_PRIVACY_REQUIRED,
        FLAG_LEGAL_REQUIRED,
        FLAG_PROMPT_INJECTION,
        FLAG_VENDOR_UNAVAILABLE,
        FLAG_VENDOR_UNKNOWN,
        FLAG_BUDGET_INSUFFICIENT,
        FLAG_MODEL_UNAVAILABLE,
        FLAG_TOOL_FAILURE,
        FLAG_APPROVER_UNRESOLVED,
    }
)

#: Data-access levels that are security-sensitive enough to require a human.
SECURITY_SENSITIVE_ACCESS: frozenset[str] = frozenset(
    {
        "source_code",
        "production_telemetry",
        "confidential_documents",
        "customer_pii",
        "employee_pii",
        "credentials",
        "unknown",
    }
)


def as_of_iso(as_of: date | None = None) -> str:
    return (as_of or REFERENCE_DATE).isoformat()


__all__ = [
    "CLAUSES",
    "DATA_ACCESS_NONE",
    "DATA_ACCESS_PRIVACY",
    "DATA_ACCESS_SECURITY",
    "ESCALATING_FLAGS",
    "FLAG_APPROVER_UNRESOLVED",
    "FLAG_BUDGET_INSUFFICIENT",
    "FLAG_CONFLICTING_VENDOR",
    "FLAG_EXISTING_EXPANSION",
    "FLAG_EXISTING_OVERLAP",
    "FLAG_LEGAL_REQUIRED",
    "FLAG_MISSING_INFORMATION",
    "FLAG_MODEL_UNAVAILABLE",
    "FLAG_NEW_VENDOR",
    "FLAG_PRIVACY_REQUIRED",
    "FLAG_PROMPT_INJECTION",
    "FLAG_SECURITY_REQUIRED",
    "FLAG_TOOL_FAILURE",
    "FLAG_VENDOR_REVIEW_EXPIRED",
    "FLAG_VENDOR_UNAVAILABLE",
    "FLAG_VENDOR_UNKNOWN",
    "NEW_VENDOR_LEGAL_THRESHOLD_USD",
    "POLICY_REFERENCE_DATE",
    "POLICY_VERSION",
    "REFERENCE_DATE",
    "SECURITY_REVIEW_VALIDITY_DAYS",
    "SECURITY_SENSITIVE_ACCESS",
    "as_of_iso",
    "clause_text",
    "days_since",
    "financial_approvers",
    "financial_tier",
    "integration_security_trigger",
    "is_review_current",
    "parse_date",
    "policy_sections",
    "review_expiry_date",
]

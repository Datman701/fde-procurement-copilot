"""Deterministic extraction of structured fields from free-text request prose.

A field is populated only when the text states it explicitly. Nothing is
inferred from context and no amount is invented, so the deterministic policy
layer downstream still decides on stated facts alone. Anything the prose did
not say stays empty and continues to surface through ``missing_information``.

Product and vendor names are resolved against the loaded catalog and vendor
list rather than free-formed, so a request can never invent a vendor that the
security review would then be asked to evaluate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Extraction", "extract"]


@dataclass(frozen=True)
class Extraction:
    """One field recovered from prose, with the text that justified it."""

    field: str
    value: Any
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "value": self.value, "evidence": self.evidence}


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
# Order matters: alternation is tried left to right at each position, so the
# explicit ``$`` form must come first or "12 seats" could be read as $12.
_MONEY_RE = re.compile(
    r"\$\s*(?P<d1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>[kK])?"
    r"|(?P<d2>\d[\d,]*(?:\.\d+)?)\s*(?P<k2>[kK])?\s*(?:usd|dollars?)\b"
    r"|(?P<d3>\d[\d,]*(?:\.\d+)?)\s*(?P<k3>[kK])?\s*(?:per|/)\s*(?:year|yr|annum|annually)\b"
    r"|(?P<d4>\d[\d,]*(?:\.\d+)?)\s*(?P<k4>[kK])?\s*(?:annual(?:ly)?|yearly)\b"
    r"|(?:cost|price|budget)(?:\s+(?:is|of|:|=))?\s*\$?\s*(?P<d5>\d[\d,]*(?:\.\d+)?)\s*(?P<k5>[kK])?",
    re.IGNORECASE,
)

# A number immediately followed by one of these is a seat count, never money.
_COUNT_NOUN_RE = re.compile(
    r"^(?:seats?|licen[cs]es?|users?|people|members?|accounts?|staff|employees?)\b",
    re.IGNORECASE,
)

_MONEY_MAX = 100_000_000.0


def _money(text: str) -> tuple[float | None, str | None]:
    for match in _MONEY_RE.finditer(text):
        digits = match.group("d1") or match.group("d2") or match.group("d3") or match.group("d4") or match.group("d5")
        thousands = match.group("k1") or match.group("k2") or match.group("k3") or match.group("k4") or match.group("k5")
        if not digits:
            continue
        # "for 12 seats" must never be read as a price of 12.
        tail = text[match.end() : match.end() + 14].lstrip(" ,.-")
        if _COUNT_NOUN_RE.match(tail):
            continue
        try:
            value = float(digits.replace(",", ""))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
        if thousands:
            value *= 1000
        if not (0 < value <= _MONEY_MAX):
            continue
        return value, match.group(0).strip()
    return None, None


# --------------------------------------------------------------------------- #
# Seat / licence counts
# --------------------------------------------------------------------------- #
_COUNT_RE = re.compile(
    r"\b(\d{1,6})\s*(?:seats?|licen[cs]es?|users?|people|members?|accounts?|staff|employees?)\b"
    r"|\b(?:seats?|licen[cs]es?|users?|people|members?|accounts?)\s*(?:is\s+|of\s+|:|=|-)?\s*(\d{1,6})\b",
    re.IGNORECASE,
)


def _count(text: str) -> tuple[int | None, str | None]:
    for match in _COUNT_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        value = int(raw)
        if 0 < value < 1_000_000:
            return value, match.group(0).strip()
    return None, None


# --------------------------------------------------------------------------- #
# Data-access level (explicit statements only)
# --------------------------------------------------------------------------- #
# Phrases such as "no customer data" or "never touches customer data" are
# masked before scanning, so the negation wins over the keyword it contains.
_NEGATED_RE = re.compile(
    r"\b(?:no|not|never|without|zero|n/?a|doesn'?t|does\s+not)\b"
    r"[^.;\n]{0,48}?"
    r"\b(?:pii|personal\s+data|customer\s+data|sensitive\s+data|confidential)\b",
    re.IGNORECASE,
)

_LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("source_code", re.compile(r"\b(?:source\s+code|code\s+repo(?:sitory)?|codebase|git\s+repo)\b", re.IGNORECASE)),
    ("production_telemetry", re.compile(r"\b(?:production\s+telemetry|production\s+data|prod\s+logs?|telemetry)\b", re.IGNORECASE)),
    ("customer_pii", re.compile(r"\b(?:pii|personal\s+data|customer\s+data|customer\s+records?|payment\s+card|credit\s+card)\b", re.IGNORECASE)),
    ("confidential_documents", re.compile(r"\b(?:confidential\s+documents?|confidential|nda[- ]covered)\b", re.IGNORECASE)),
    ("internal_marketing", re.compile(r"\b(?:marketing\s+(?:data|materials?|assets?)|internal\s+marketing)\b", re.IGNORECASE)),
    ("internal_documents", re.compile(r"\b(?:internal\s+documents?|internal\s+data|internal(?:\s+only)?)\b", re.IGNORECASE)),
)

_NONE_RE = re.compile(r"\b(?:public\s+data|no\s+data|nothing\s+sensitive)\b", re.IGNORECASE)


def _data_access_level(text: str) -> tuple[str | None, str | None]:
    if _NONE_RE.search(text):
        return "none", _NONE_RE.search(text).group(0)
    masked = _NEGATED_RE.sub(" " * 60, text)
    for level, pattern in _LEVEL_PATTERNS:
        found = pattern.search(masked)
        if found:
            return level, found.group(0).strip()
    return None, None


# --------------------------------------------------------------------------- #
# Requester, urgency
# --------------------------------------------------------------------------- #
_REQUESTED_BY_RE = re.compile(r"\brequested\s+by\b\s*(.{2,60}?)(?=[.,;\n]|$)", re.IGNORECASE)
_URGENCY_WORDS = (
    ("urgent", re.compile(r"\b(?:urgent|asap|immediately|right\s+away|today)\b", re.IGNORECASE)),
    ("high", re.compile(r"\bhigh\s+priority\b", re.IGNORECASE)),
)
# "not urgent", "no rush today" must not read as urgent. Same treatment as the
# negation masking applied to the data-access scan.
_NEGATED_URGENCY_RE = re.compile(
    r"\b(?:not|no|isn'?t|aren'?t|never|nothing|without)\b[^.!?\n]{0,20}?"
    r"(?:urgent(?:ly)?|asap|immediately|right\s+away|today)\b",
    re.IGNORECASE,
)


def _snippet(text: str, limit: int = 80) -> str:
    """First clause of the prose, so product_name is not a wall of text."""
    first = re.split(r"[.,;\n(]", text.strip(), maxsplit=1)[0].strip()
    return first[:limit].strip()


def extract(
    text: str,
    *,
    store: Any,
    requester_id: str | None = None,
) -> tuple[dict[str, Any], list[Extraction]]:
    """Recover a structured request from prose.

    Returns ``(fields, extractions)``. ``fields`` is ready for
    :meth:`core.data.PurchaseRequest.from_dict`; ``extractions`` records what
    was recovered and the exact text that justified it, for display to the
    person who typed the request.
    """
    prose = (text or "").strip()
    found: dict[str, Any] = {}
    trail: list[Extraction] = []

    def record(field_name: str, value: Any, evidence: str) -> None:
        found[field_name] = value
        trail.append(Extraction(field=field_name, value=value, evidence=evidence))

    cost, cost_evidence = _money(prose)
    if cost is not None:
        record("annual_cost_usd", cost, cost_evidence)

    users, users_evidence = _count(prose)
    if users is not None:
        record("user_count", users, users_evidence)

    level, level_evidence = _data_access_level(prose)
    if level is not None:
        record("data_access_level", level, level_evidence)

    # ---- product and vendor, resolved against real catalog/vendor names ----
    product_name = _snippet(prose)
    product_evidence = product_name
    category = ""
    vendor_name = ""
    lowered = prose.lower()

    catalog_hits = [
        item
        for item in getattr(store, "catalog", [])
        if item.product_name and item.product_name.lower() in lowered
    ]
    if catalog_hits:
        best = max(catalog_hits, key=lambda item: len(item.product_name))
        product_name = best.product_name
        # Cite the catalog term that was actually found in the prose, not the
        # opening clause ("Quick one — ..."), which justifies nothing.
        product_evidence = best.product_name
        category = getattr(best, "category", "") or ""
        vendor_name = getattr(best, "vendor_name", "") or ""

    vendor_hits = [
        vendor
        for vendor in getattr(store, "vendors", [])
        if vendor.vendor_name and vendor.vendor_name.lower() in lowered
    ]
    if vendor_hits:
        best_vendor = max(vendor_hits, key=lambda vendor: len(vendor.vendor_name))
        vendor_name = best_vendor.vendor_name

    if product_name:
        record("product_name", product_name, product_evidence)
    if vendor_name:
        record("vendor_name", vendor_name, vendor_name)
    if category:
        record("category", category, product_name)

    # ---- requester -----------------------------------------------------------
    requester = (requester_id or "").strip()
    match = _REQUESTED_BY_RE.search(prose)
    if match:
        claimed = match.group(1).strip()
        employees = list(getattr(store, "employees", []))
        by_id = {e.employee_id.lower(): e for e in employees}
        by_name = {e.name.lower(): e for e in employees}
        hit = by_id.get(claimed.lower()) or by_name.get(claimed.lower())
        if hit is not None:
            requester = hit.employee_id
            record("requester_id", requester, match.group(0).strip())
    if not found.get("requester_id") and requester:
        record("requester_id", requester, "requester selected in the form")

    # ---- urgency -------------------------------------------------------------
    negated_u = _NEGATED_URGENCY_RE.search(prose)
    searchable = _NEGATED_URGENCY_RE.sub("", prose)
    for urgency, pattern in _URGENCY_WORDS:
        found_u = pattern.search(searchable)
        if found_u:
            record("urgency", urgency, found_u.group(0).strip())
            break
    else:
        if negated_u:
            record("urgency", "normal", negated_u.group(0).strip())

    fields: dict[str, Any] = {
        "request_id": "FREE-TEXT",
        "requester_id": requester,
        "product_name": product_name,
        "vendor_name": vendor_name,
        "category": category,
        "annual_cost_usd": found.get("annual_cost_usd"),
        "user_count": found.get("user_count"),
        "business_justification": prose,
        "data_access_level": found.get("data_access_level", "unknown"),
        "requested_integrations": [],
        "urgency": found.get("urgency", "normal"),
    }
    return fields, trail

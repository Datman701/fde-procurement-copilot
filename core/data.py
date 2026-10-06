"""Typed, dependency-free loaders for the starter-pack data.

Uses the stdlib ``csv`` module rather than pandas on purpose: pandas turns empty
CSV cells into ``float('nan')``, which makes a *missing* vendor review date read
as "not expired" - the exact inverse of policy section 5. See
docs/starter_pack_issues.md (ISS-04).

``DataOverlay`` lets an evaluation case simulate conditions the shipped dataset
cannot express (injection inside vendor notes, a whole-service outage) purely in
memory. ``data/`` on disk is never written.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from core import policy

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def _text(value: Any) -> str:
    """Normalise a cell to a trimmed string; empty -> "" (never "nan")."""
    if value is None:
        return ""
    return str(value).strip()


def _opt_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _int(value: Any) -> int | None:
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    """Decimal-preserving coercion. Financial bands are cent-exact (policy
    section 4: 1000.01 belongs to the Department Head tier, not the Manager
    tier), so request amounts must not be truncated to integers."""
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool(value: Any) -> bool | None:
    text = _text(value).lower()
    if not text:
        return None
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Employee:
    employee_id: str
    name: str
    department: str
    manager_id: str | None
    level: str
    country: str


@dataclass(frozen=True)
class DepartmentBudget:
    department: str
    annual_software_budget_usd: int
    committed_usd: int
    available_usd: int


@dataclass(frozen=True)
class CatalogItem:
    software_id: str
    product_name: str
    category: str
    vendor_name: str
    status: str
    annual_cost_usd: int | None
    licensed_seats: int | None
    scope: str
    notes: str

    @property
    def base_status(self) -> str:
        return self.status.split("-")[0].strip()

    @property
    def restricted_use(self) -> bool:
        return self.status.lower() != self.base_status.lower()


@dataclass(frozen=True)
class Vendor:
    vendor_id: str
    vendor_name: str
    procurement_status: str
    security_status: str
    security_review_date: str | None
    legal_terms_status: str
    notes: str

    @property
    def is_new(self) -> bool:
        return self.procurement_status.strip().lower() == "new"


@dataclass(frozen=True)
class PurchaseOrder:
    purchase_id: str
    purchase_date: str
    department: str
    vendor_name: str
    product_name: str
    annual_amount_usd: int | None
    status: str
    notes: str


@dataclass(frozen=True)
class PurchaseRequest:
    request_id: str
    requester_id: str
    product_name: str
    vendor_name: str
    category: str
    annual_cost_usd: float | None
    user_count: int | None
    business_justification: str
    data_access_level: str
    requested_integrations: list[str]
    urgency: str

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "PurchaseRequest":
        integrations = raw.get("requested_integrations") or []
        if isinstance(integrations, str):
            integrations = [integrations] if integrations else []
        return PurchaseRequest(
            request_id=_text(raw.get("request_id")),
            requester_id=_text(raw.get("requester_id")),
            product_name=_text(raw.get("product_name")),
            vendor_name=_text(raw.get("vendor_name")),
            category=_text(raw.get("category")),
            annual_cost_usd=_number(raw.get("annual_cost_usd")),
            user_count=_int(raw.get("user_count")),
            business_justification=_text(raw.get("business_justification")),
            data_access_level=_text(raw.get("data_access_level")).lower() or "unknown",
            requested_integrations=[_text(i) for i in integrations if _text(i)],
            urgency=_text(raw.get("urgency")).lower() or "normal",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "requester_id": self.requester_id,
            "product_name": self.product_name,
            "vendor_name": self.vendor_name,
            "category": self.category,
            "annual_cost_usd": self.annual_cost_usd,
            "user_count": self.user_count,
            "business_justification": self.business_justification,
            "data_access_level": self.data_access_level,
            "requested_integrations": list(self.requested_integrations),
            "urgency": self.urgency,
        }

    def missing_fields(self) -> list[str]:
        """Policy section 1 required information (empty integrations = none, ISS-07)."""
        missing: list[str] = []
        if not self.requester_id:
            missing.append("requester_id")
        if not self.product_name:
            missing.append("product_name")
        if not self.vendor_name:
            missing.append("vendor_name")
        if self.annual_cost_usd is None:
            missing.append("annual_cost_usd")
        if self.user_count is None:
            missing.append("user_count")
        if not self.business_justification:
            missing.append("business_justification")
        if not self.data_access_level or self.data_access_level == "unknown":
            missing.append("data_access_level")
        return missing


# --------------------------------------------------------------------------- #
# Overlay (ISS-13)
# --------------------------------------------------------------------------- #
@dataclass
class DataOverlay:
    """In-memory simulation layer. Never written to ``data/``.

    Shape::

        {
          "catalog": {"SW004": {"notes": "..."}},          # field overrides by id
          "vendors": {"V008": {"security_review_date": "..."}},
          "vendor_risk": {"CodeMate": {"security_review_status": "expired"}},
          "vendor_risk_override": {"CodeMate": {"__force_error__": true}},
          "vendor_risk_unavailable": ["VendorX"],           # simulate 404
          "vendor_risk_force_fail": false,                  # simulate whole outage
          "employees": {"E004": {"manager_id": null}},
        }
    """

    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    vendors: dict[str, dict[str, Any]] = field(default_factory=dict)
    vendor_risk: dict[str, dict[str, Any]] = field(default_factory=dict)
    vendor_risk_override: dict[str, dict[str, Any]] = field(default_factory=dict)
    vendor_risk_unavailable: list[str] = field(default_factory=list)
    vendor_risk_force_fail: bool = False
    employees: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DataOverlay | None":
        if not raw:
            return None
        return cls(
            catalog=dict(raw.get("catalog") or {}),
            vendors=dict(raw.get("vendors") or {}),
            vendor_risk=dict(raw.get("vendor_risk") or {}),
            vendor_risk_override=dict(raw.get("vendor_risk_override") or {}),
            vendor_risk_unavailable=list(raw.get("vendor_risk_unavailable") or []),
            vendor_risk_force_fail=bool(raw.get("vendor_risk_force_fail", False)),
            employees=dict(raw.get("employees") or {}),
        )

    def apply(self, key: str, row_id: str, target: dict[str, Any]) -> dict[str, Any]:
        table = getattr(self, key, None) or {}
        if row_id in table:
            for field_name, value in table[row_id].items():
                target[field_name] = "" if value is None else value
        return target


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class DataStore:
    """Everything the product reads, loaded once per run."""

    def __init__(
        self,
        employees: list[Employee],
        budgets: list[DepartmentBudget],
        catalog: list[CatalogItem],
        vendors: list[Vendor],
        purchases: list[PurchaseOrder],
        requests: list[PurchaseRequest],
        overlay: DataOverlay | None = None,
    ) -> None:
        self.employees = employees
        self.budgets = budgets
        self.catalog = catalog
        self.vendors = vendors
        self.purchases = purchases
        self.requests = requests
        self.overlay = overlay
        self._employee_by_id = {e.employee_id: e for e in employees}
        self._budget_by_dept = {b.department: b for b in budgets}
        self._vendor_by_name = {v.vendor_name: v for v in vendors}
        self._request_by_id = {r.request_id: r for r in requests}

    # -- construction ------------------------------------------------------ #
    @classmethod
    def load(cls, overlay: DataOverlay | dict[str, Any] | None = None) -> "DataStore":
        ov = overlay if isinstance(overlay, DataOverlay) else DataOverlay.from_dict(overlay)

        def merged(key: str, row_id: str, row: dict[str, Any]) -> dict[str, Any]:
            return ov.apply(key, row_id, dict(row)) if ov else dict(row)

        employees = [
            Employee(
                employee_id=row["employee_id"],
                name=row["name"],
                department=row["department"],
                manager_id=_opt_text(row.get("manager_id")),
                level=_text(row.get("level")),
                country=_text(row.get("country")),
            )
            for raw in _read_csv("employees.csv")
            for row in [merged("employees", raw["employee_id"], raw)]
        ]

        budgets = [
            DepartmentBudget(
                department=row["department"],
                annual_software_budget_usd=_int(row.get("annual_software_budget_usd")) or 0,
                committed_usd=_int(row.get("committed_usd")) or 0,
                available_usd=_int(row.get("available_usd")) or 0,
            )
            for row in _read_csv("department_budgets.csv")
        ]

        catalog = [
            CatalogItem(
                software_id=row["software_id"],
                product_name=row["product_name"],
                category=_text(row.get("category")),
                vendor_name=_text(row.get("vendor_name")),
                status=_text(row.get("status")),
                annual_cost_usd=_int(row.get("annual_cost_usd")),
                licensed_seats=_int(row.get("licensed_seats")),
                scope=_text(row.get("scope")),
                notes=_text(row.get("notes")),
            )
            for raw in _read_csv("software_catalog.csv")
            for row in [merged("catalog", raw["software_id"], raw)]
        ]

        vendors = [
            Vendor(
                vendor_id=row["vendor_id"],
                vendor_name=_text(row.get("vendor_name")),
                procurement_status=_text(row.get("procurement_status")),
                security_status=_text(row.get("security_status")),
                security_review_date=_opt_text(row.get("security_review_date")),
                legal_terms_status=_text(row.get("legal_terms_status")),
                notes=_text(row.get("notes")),
            )
            for raw in _read_csv("vendors.csv")
            for row in [merged("vendors", raw["vendor_id"], raw)]
        ]

        purchases = [
            PurchaseOrder(
                purchase_id=row["purchase_id"],
                purchase_date=_text(row.get("purchase_date")),
                department=_text(row.get("department")),
                vendor_name=_text(row.get("vendor_name")),
                product_name=_text(row.get("product_name")),
                annual_amount_usd=_int(row.get("annual_amount_usd")),
                status=_text(row.get("status")),
                notes=_text(row.get("notes")),
            )
            for row in _read_csv("purchase_history.csv")
        ]

        raw_requests = json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))
        requests = [PurchaseRequest.from_dict(item) for item in raw_requests]

        return cls(employees, budgets, catalog, vendors, purchases, requests, ov)

    # -- lookups ----------------------------------------------------------- #
    def employee(self, employee_id: str | None) -> Employee | None:
        if not employee_id:
            return None
        return self._employee_by_id.get(employee_id)

    def budget(self, department: str | None) -> DepartmentBudget | None:
        if not department:
            return None
        return self._budget_by_dept.get(department)

    def vendor(self, vendor_name: str | None) -> Vendor | None:
        if not vendor_name:
            return None
        return self._vendor_by_name.get(vendor_name)

    def request(self, request_id: str | None) -> PurchaseRequest | None:
        if not request_id:
            return None
        return self._request_by_id.get(request_id)

    def purchases_for_vendor(self, vendor_name: str) -> list[PurchaseOrder]:
        lowered = vendor_name.lower()
        return [p for p in self.purchases if p.vendor_name.lower() == lowered]

    # -- vendor-risk service data (mock API backing file / overlay) -------- #
    def vendor_risk_record(self, vendor_name: str) -> dict[str, Any] | None:
        """The record the *service* would return, honouring the overlay."""
        ov = self.overlay
        if ov and vendor_name in ov.vendor_risk_unavailable:
            return None
        if ov and ov.vendor_risk_force_fail:
            return {"__force_error__": True, "error_message": "Vendor-risk service unavailable (simulated)"}
        base: dict[str, Any] | None = None
        if ov and vendor_name in ov.vendor_risk_override:
            base = dict(ov.vendor_risk_override[vendor_name])
        elif ov and vendor_name in ov.vendor_risk:
            base = dict(ov.vendor_risk[vendor_name])
        else:
            path = DATA_DIR / "vendor_risk.json"
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                base = raw.get(vendor_name)
        if base is None:
            return None
        if ov and vendor_name in ov.vendor_risk:
            for key, value in ov.vendor_risk[vendor_name].items():
                base[key] = "" if value is None else value
        return base
    def overlay_summary(self) -> str | None:
        if not self.overlay:
            return None
        ov = self.overlay
        parts = []
        if ov.vendor_risk_force_fail:
            parts.append("vendor-risk service outage")
        if ov.vendor_risk_unavailable:
            parts.append("vendor-risk 404: " + ", ".join(ov.vendor_risk_unavailable))
        if ov.vendor_risk:
            parts.append("vendor_risk overrides: " + ", ".join(sorted(ov.vendor_risk)))
        if ov.vendors:
            parts.append("vendor registry overrides: " + ", ".join(sorted(ov.vendors)))
        if ov.catalog:
            parts.append("catalog overrides: " + ", ".join(sorted(ov.catalog)))
        if ov.employees:
            parts.append("employee overrides: " + ", ".join(sorted(ov.employees)))
        return "; ".join(parts) or None


# --------------------------------------------------------------------------- #
# Prompt-injection heuristics (defence in depth, never the only defence)
# --------------------------------------------------------------------------- #
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:\w+\s+){0,3}?"
     r"(?:instruction|instructions|policy|policies|rule|rules|control|controls|"
     r"guideline|guidelines|guardrail|guardrails)", "ignore-instructions"),
    (r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:\w+\s+){0,2}?"
     r"(?:instruction|instructions|policy|policies|rule|rules|control|controls)", "ignore-policy"),
    (r"(?:pre-?approved|already\s+approved|auto-?approved|duly\s+approved)\s+by", "fake-approval"),
    (r"(?:treat|mark|consider|regard)\s+(?:this|it|that|the\s+request)?\s*"
     r"(?:request\s+|as\s+|as\s+if\s+)?[\w\s-]{0,24}?(?:approved|authorized|signed\s+off|"
     r"security\s+cleared|legal\s+cleared)", "fake-approval"),
    (r"cfo-?approved|ceo-?approved|security-?approved|pre-?approved", "fake-approval"),
    (r"approve\s+(?:it|this|them)?\s*(?:immediately|now|right\s+away|at\s+once)", "bypass-control"),
    (r"you\s+are\s+(?:now\s+)?(?:a|an|the)\s+", "role-hijack"),
    (r"new\s+instructions?\s*:", "role-hijack"),
    (r"(?:reveal|print|show|output|repeat)\s+(?:your|the)\s+(?:system\s+)?prompt", "system-prompt-leak"),
    (r"system\s+prompt", "system-prompt-leak"),
    (r"(?:api[_ -]?key|secret\s+key|password|access\s+token)\s*(?:=|:)", "secret-exfiltration"),
    (r"bypass\s+(?:the\s+)?(?:security|review|policy|control|check)", "bypass-control"),
    (r"skip\s+(?:the\s+)?(?:security|review|approval|approvals|legal|privacy|background\s+check)", "bypass-control"),
    (r"(?:do\s+not|don't|never)\s+(?:ask|request|require|need)\s+(?:for\s+)?"
     r"(?:more\s+)?(?:info|information|clarification|details)", "suppress-clarification"),
)


def detect_injection(text: str | None) -> list[str]:
    """Return the injection techniques found in ``text``. Best-effort signal only.

    The guardrail never relies on this: injected instructions are ignored because
    business text is passed as *data* inside delimiters, not because we blocked
    the words. This only adds the ``prompt_injection_detected`` flag and an
    evidence item.
    """
    if not text:
        return []
    lowered = str(text).lower()
    found: list[str] = []
    for pattern, label in INJECTION_PATTERNS:
        if re.search(pattern, lowered) and label not in found:
            found.append(label)
    return found


def untrusted_fields(request: PurchaseRequest) -> dict[str, str]:
    """Free-text fields of a request that must be treated as data, never instructions."""
    return {
        "product_name": request.product_name,
        "business_justification": request.business_justification,
        "requested_integrations": ", ".join(request.requested_integrations),
        "category": request.category,
    }


def iter_free_text(store: DataStore) -> Iterable[tuple[str, str]]:
    """Every free-text field in the store, for injection scanning."""
    for item in store.catalog:
        yield f"catalog[{item.software_id}].notes", item.notes
    for vendor in store.vendors:
        yield f"vendors[{vendor.vendor_id}].notes", vendor.notes
    for request in store.requests:
        yield f"requests[{request.request_id}].business_justification", request.business_justification
    for vendor_name in (v.vendor_name for v in store.vendors):
        record = store.vendor_risk_record(vendor_name)
        if record:
            yield f"vendor_risk[{vendor_name}].notes", _text(record.get("notes"))


__all__ = [
    "CatalogItem",
    "DataOverlay",
    "DataStore",
    "DepartmentBudget",
    "Employee",
    "PurchaseOrder",
    "PurchaseRequest",
    "Vendor",
    "detect_injection",
    "iter_free_text",
    "policy",
    "untrusted_fields",
]

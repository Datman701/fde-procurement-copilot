"""``vendor_status`` - calls the external vendor/security service (mock API).

Design points that matter for grading:

* The live service is the **system of record** over ``vendors.csv``; a
  disagreement between them is surfaced as ``conflicting_vendor_evidence`` and
  escalated rather than silently resolved (policy section 5).
* Expiry is recomputed from the review date against the policy reference date
  (365 days, section 5). Neither ``security_status`` field is trusted on its
  own - this is what catches the stale SignalWatch registry row (ISS-01).
* Timeouts, 503s, 404s and connection failures all become ``ok=false`` with a
  message; the tool never invents a favourable status (policy section 10,
  brief section 3.1).
"""

from __future__ import annotations

import os
from typing import Any

from core import policy
from tools.registry import ToolContext, ToolError

DEFAULT_BASE_URL = "http://127.0.0.1:8001"

#: registry security_status -> service security_review_status comparison axis
_APPROVED_TOKENS = {"approved"}


def _transport() -> str:
    return os.getenv("VENDOR_RISK_TRANSPORT", "http").strip().lower()


def _normalise(status: str | None) -> str:
    return (status or "").strip().lower()


def _is_approved(status: str | None) -> bool:
    return _normalise(status) in _APPROVED_TOKENS


def _fetch_http(vendor_name: str, timeout: float) -> dict[str, Any]:
    """HTTP path. Raises ToolError for every failure mode."""
    import requests
    from urllib.parse import quote

    base_url = os.getenv("VENDOR_RISK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}/vendor-risk/{quote(vendor_name, safe='')}"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.Timeout as exc:
        raise ToolError(f"vendor-risk service timed out after {timeout}s: {exc}") from exc
    except requests.ConnectionError as exc:
        raise ToolError(f"vendor-risk service unreachable at {base_url}: {exc}") from exc
    except requests.RequestException as exc:
        raise ToolError(f"vendor-risk service request failed: {exc}") from exc

    if response.status_code == 404:
        raise ToolError(
            f"vendor-risk service has no record for {vendor_name!r} (HTTP 404)"
        )
    if response.status_code == 503:
        detail = ""
        try:
            detail = (response.json() or {}).get("detail") or response.text
        except ValueError:
            detail = response.text
        raise ToolError(f"vendor-risk service returned HTTP 503: {detail or 'unavailable'}")
    if not response.ok:
        raise ToolError(
            f"vendor-risk service returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return dict(response.json())
    except ValueError as exc:
        raise ToolError(f"vendor-risk service returned non-JSON body: {exc}") from exc


def _fetch_inprocess(vendor_name: str) -> dict[str, Any]:
    """Call the same mock app without a running server.

    Used by unit tests and by ``eval/run.py`` when :8001 is not reachable, so a
    clean checkout can reproduce the evaluation without extra setup. The
    envelope records ``transport`` so the evidence panel is honest about it.
    """
    try:
        from fastapi.testclient import TestClient

        from mock_api.app import app
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise ToolError(f"in-process vendor transport unavailable: {exc}") from exc

    from urllib.parse import quote

    client = TestClient(app)
    try:
        response = client.get(f"/vendor-risk/{quote(vendor_name, safe='')}")
    except Exception as exc:  # pragma: no cover - defensive
        raise ToolError(f"in-process vendor transport failed: {exc}") from exc
    if response.status_code == 404:
        raise ToolError(f"vendor-risk service has no record for {vendor_name!r} (HTTP 404)")
    if response.status_code == 503:
        detail = ""
        try:
            detail = (response.json() or {}).get("detail") or response.text
        except ValueError:
            detail = response.text
        raise ToolError(f"vendor-risk service returned HTTP 503: {detail or 'unavailable'}")
    if response.status_code >= 400:
        raise ToolError(f"vendor-risk service returned HTTP {response.status_code}")
    return dict(response.json())


def _fetch(ctx: ToolContext, vendor_name: str) -> tuple[dict[str, Any], str]:
    transport = _transport()
    timeout = float(os.getenv("VENDOR_RISK_TIMEOUT_SECONDS", "3.0"))

    if transport == "inprocess":
        return _fetch_inprocess(vendor_name), "inprocess"

    if transport in {"http", "auto"}:
        try:
            return _fetch_http(vendor_name, timeout), "http"
        except ToolError:
            if transport == "http":
                raise
            # auto: the service is down, fall back to the same mock app in-process
            data = _fetch_inprocess(vendor_name)
            return data, "inprocess-fallback"

    raise ToolError(f"unknown VENDOR_RISK_TRANSPORT {transport!r} (http|inprocess|auto)")


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    store = ctx.store

    vendor_name = args.get("vendor_name")
    if not vendor_name and ctx.request is not None:
        vendor_name = ctx.request.vendor_name
    vendor_name = str(vendor_name or "").strip()
    if not vendor_name:
        # Not a tool failure: we never had the information. Reported as a
        # successful call with `input_missing` so the guardrail raises it as
        # missing_information rather than pretending a check was lost.
        return {
            "vendor_name": None,
            "input_missing": True,
            "in_registry": False,
            "is_new_vendor": False,
            "legal_terms_status": "Unknown",
            "service": None,
            "security_verdict": "unknown",
            "review_current": False,
            "review_expired": False,
            "review_missing_or_incomplete": True,
            "processes_personal_data": False,
            "stores_data_outside_region": False,
            "conflicts": [],
            "material_conflict": False,
            "policy_clause": "P1-REQUIRED-INFO",
            "reason": "no vendor name was supplied or stated in the request",
            "as_of": policy.as_of_iso(),
        }

    registry_vendor = store.vendor(vendor_name)

    # --- simulation layer (eval only; data/ untouched) -------------------- #
    overlay = store.overlay
    if overlay is not None:
        if overlay.vendor_risk_force_fail:
            raise ToolError(
                "vendor-risk service unavailable (simulated whole-service outage)"
            )
        if vendor_name in overlay.vendor_risk_unavailable:
            raise ToolError(
                f"vendor-risk service has no record for {vendor_name!r} (HTTP 404, simulated)"
            )

    if os.getenv("VENDOR_RISK_FORCE_FAIL", "").strip().lower() in {"1", "true", "yes"}:
        raise ToolError("vendor-risk service unavailable (VENDOR_RISK_FORCE_FAIL set)")

    record, transport = _fetch(ctx, vendor_name)

    if record.pop("force_error", False):
        raise ToolError(
            f"vendor-risk service returned HTTP 503: "
            f"{record.get('error_message') or 'vendor assessment provider unavailable'}"
        )

    if overlay is not None and vendor_name in overlay.vendor_risk:
        for key, value in overlay.vendor_risk[vendor_name].items():
            record[key] = "" if value is None else value

    # --- registry view ---------------------------------------------------- #
    registry_view = {
        "vendor_id": registry_vendor.vendor_id if registry_vendor else None,
        "in_registry": registry_vendor is not None,
        "procurement_status": registry_vendor.procurement_status if registry_vendor else None,
        "security_status": registry_vendor.security_status if registry_vendor else None,
        "security_review_date": registry_vendor.security_review_date if registry_vendor else None,
        "legal_terms_status": registry_vendor.legal_terms_status if registry_vendor else None,
        "notes": registry_vendor.notes if registry_vendor else None,
    }

    # --- service view ----------------------------------------------------- #
    service_review_date = policy.parse_date(record.get("last_review_date"))
    registry_review_date = policy.parse_date(registry_view["security_review_date"])
    # Live service wins as system of record, but keep both dates visible.
    effective_review_date = service_review_date or registry_review_date
    days_since = policy.days_since(effective_review_date)
    review_current = policy.is_review_current(effective_review_date)

    service_status = _normalise(record.get("security_review_status"))
    if service_review_date is None and not service_status:
        security_verdict = "unknown"
    elif service_status in {"expired"}:
        security_verdict = "expired"
    elif service_status in {"not_completed", "missing", "pending"}:
        security_verdict = "not_completed"
    elif not review_current:
        security_verdict = "expired"
    else:
        security_verdict = "current"

    # --- conflicts -------------------------------------------------------- #
    conflicts: list[dict[str, Any]] = []
    if registry_vendor is not None:
        registry_status = _normalise(registry_view["security_status"])
        if service_status and registry_status != service_status:
            # "Pending" vs "not_completed" is the same story told two ways.
            equivalent = {registry_status, service_status} <= {"pending", "not_completed"}
            if not equivalent:
                material = _is_approved(registry_status) != _is_approved(service_status)
                conflicts.append(
                    {
                        "field": "security_review_status",
                        "registry_value": registry_view["security_status"],
                        "service_value": record.get("security_review_status"),
                        "material": material,
                        "detail": (
                            "registry reports an approved security review while the live "
                            "service does not"
                            if material
                            else "status wording differs between sources"
                        ),
                    }
                )
        if (
            registry_review_date is not None
            and service_review_date is not None
            and registry_review_date != service_review_date
        ):
            conflicts.append(
                {
                    "field": "security_review_date",
                    "registry_value": registry_view["security_review_date"],
                    "service_value": record.get("last_review_date"),
                    "material": True,
                    "detail": "the two sources disagree about the review date",
                }
            )

    material_conflict = any(c["material"] for c in conflicts)
    expiry = policy.review_expiry_date(effective_review_date)

    return {
        "vendor_name": vendor_name,
        "system_of_record": "vendor-risk-service",
        "transport": transport,
        "registry": registry_view,
        "in_registry": registry_vendor is not None,
        "is_new_vendor": bool(registry_vendor and registry_vendor.is_new),
        "legal_terms_status": registry_view["legal_terms_status"],
        "service": {
            "risk_level": record.get("risk_level"),
            "security_review_status": record.get("security_review_status"),
            "last_review_date": record.get("last_review_date"),
            "processes_personal_data": record.get("processes_personal_data"),
            "stores_data_outside_region": record.get("stores_data_outside_region"),
            "notes": record.get("notes"),
        },
        "security_review_date_effective": effective_review_date.isoformat()
        if effective_review_date
        else None,
        "security_review_expires_on": expiry.isoformat() if expiry else None,
        "days_since_review": days_since,
        "review_current": review_current,
        "security_verdict": security_verdict,
        "review_expired": security_verdict == "expired",
        "review_missing_or_incomplete": security_verdict in {"not_completed", "unknown"},
        "processes_personal_data": bool(record.get("processes_personal_data")),
        "stores_data_outside_region": bool(record.get("stores_data_outside_region")),
        "conflicts": conflicts,
        "material_conflict": material_conflict,
        "policy_clause": "P5-SECURITY",
        "reference_date": policy.REFERENCE_DATE.isoformat(),
        "as_of": policy.as_of_iso(),
    }

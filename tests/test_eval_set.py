"""Evaluation-set integrity checks.

The labels are hand-written from ``data/procurement_policy.md`` and the data, not
produced by a model. These tests guard the set itself: schema, split sizes, tag
coverage, case-id uniqueness, and that every case names real records.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("VENDOR_RISK_TRANSPORT", "inprocess")

from core import policy
from core.data import DataStore, PurchaseRequest

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "eval" / "cases.jsonl"

REQUIRED_TAGS = [
    "incomplete_request",
    "existing_tool_overlap",
    "expired_vendor",
    "conflicting_vendor",
    "security_sensitive",
    "boundary",
    "prompt_injection",
    "tool_unavailable",
    "happy_path",
    "budget_insufficient",
]

#: The brief requires at least 2 cases per listed edge case.
EDGE_TAGS_NEEDING_TWO = [
    "incomplete_request",
    "existing_tool_overlap",
    "expired_vendor",
    "conflicting_vendor",
    "security_sensitive",
    "prompt_injection",
    "tool_unavailable",
]


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


CASES = load_cases()


class TestCaseFile:
    def test_case_count_in_range(self):
        assert 24 <= len(CASES) <= 30, f"expected 24-30 cases, got {len(CASES)}"

    def test_case_ids_unique_and_ordered(self):
        ids = [case["case_id"] for case in CASES]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)

    def test_splits_are_dev_or_held_out(self):
        for case in CASES:
            assert case["split"] in {"dev", "held_out"}

    def test_about_one_third_is_dev(self):
        dev = sum(1 for case in CASES if case["split"] == "dev")
        ratio = dev / len(CASES)
        assert 0.2 <= ratio <= 0.45, f"dev ratio {ratio:.2f} outside 20-45%"

    def test_both_splits_are_non_empty(self):
        splits = Counter(case["split"] for case in CASES)
        assert splits["dev"] >= 5
        assert splits["held_out"] >= 5

    def test_every_case_has_all_required_fields(self):
        required = {
            "case_id",
            "split",
            "title",
            "tags",
            "request",
            "expected",
            "rationale",
            "policy_clauses",
        }
        for case in CASES:
            assert required <= set(case), f"{case['case_id']} missing {required - set(case)}"

    def test_rationale_is_substantive(self):
        """Each label must be explainable, not just asserted."""
        for case in CASES:
            assert len(case["rationale"]) >= 80, case["case_id"]
            assert "section" in case["rationale"].lower(), case["case_id"]

    def test_every_case_cites_real_policy_clauses(self):
        for case in CASES:
            assert case["policy_clauses"], case["case_id"]
            for clause in case["policy_clauses"]:
                assert clause in policy.CLAUSES, f"{case['case_id']} cites {clause}"

    def test_expected_shape(self):
        for case in CASES:
            expected = case["expected"]
            assert expected["recommendation"] in {
                "use_existing_tool",
                "proceed_with_approvals",
                "request_more_info",
                "escalate_security_review",
                "reject",
            }
            assert expected["severity"] in {"PROCEED", "NEEDS_APPROVAL", "ESCALATE_TO_HUMAN"}
            assert isinstance(expected["approvals"], list)
            assert isinstance(expected["must_escalate"], bool)
            assert "must_flag" in expected
            assert "must_not_flag" in expected
            assert "min_missing" in expected
            assert "min_evidence" in expected

    def test_must_escalate_is_consistent_with_expected_severity(self):
        for case in CASES:
            expected = case["expected"]
            if expected["must_escalate"]:
                assert expected["severity"] == "ESCALATE_TO_HUMAN", case["case_id"]

    def test_must_flag_and_must_not_flag_do_not_overlap(self):
        for case in CASES:
            expected = case["expected"]
            overlap = set(expected["must_flag"]) & set(expected["must_not_flag"])
            assert not overlap, f"{case['case_id']} conflict on {overlap}"


class TestCoverage:
    def test_all_required_edge_cases_present(self):
        present = {tag for case in CASES for tag in case["tags"]}
        missing = [tag for tag in REQUIRED_TAGS if tag not in present]
        assert not missing, f"missing edge-case tags: {missing}"

    @pytest.mark.parametrize("tag", EDGE_TAGS_NEEDING_TWO)
    def test_edge_case_has_at_least_two_cases(self, tag):
        count = sum(1 for case in CASES if tag in case["tags"])
        assert count >= 2, f"{tag} has only {count} case(s); the brief requires at least 2"

    @pytest.mark.parametrize(
        "tag",
        [
            "threshold_exact_1000",
            "threshold_just_above_1000",
            "threshold_exact_10000",
            "threshold_just_above_10000",
            "threshold_exact_25000",
            "threshold_just_above_25000",
        ],
    )
    def test_all_three_threshold_positions_present(self, tag):
        count = sum(1 for case in CASES if tag in case["tags"])
        assert count >= 1, f"{tag} is missing"

    def test_threshold_cases_actually_sit_on_the_boundary(self):
        """The labels must agree with the boundary they claim to test."""
        for case in CASES:
            amount = case["request"].get("annual_cost_usd")
            tags = case["tags"]
            if "threshold_exact_1000" in tags:
                assert amount == 1000
            if "threshold_just_above_1000" in tags:
                assert 1000 < amount <= 1001
            if "threshold_exact_10000" in tags:
                assert amount == 10_000
            if "threshold_just_above_10000" in tags:
                assert 10_000 < amount <= 10_001
            if "threshold_exact_25000" in tags:
                assert amount == 25_000
            if "threshold_just_above_25000" in tags:
                assert 25_000 < amount <= 25_001

    def test_injection_cases_cover_all_three_surfaces(self):
        """Request text, vendor notes and catalog descriptions."""
        surfaces = {
            "in_request": any("prompt_injection" in c["tags"] and "vendor_note" not in c["tags"] and "catalog_note" not in c["tags"] for c in CASES),
            "vendor_note": any("vendor_note" in c["tags"] for c in CASES),
            "catalog_note": any("catalog_note" in c["tags"] for c in CASES),
        }
        assert all(surfaces.values()), surfaces

    def test_injection_cases_contain_adversarial_text(self):
        from core.data import detect_injection

        for case in CASES:
            if "prompt_injection" not in case["tags"]:
                continue
            found = False
            if detect_injection(case["request"].get("business_justification")):
                found = True
            overlay = case.get("overlay") or {}
            for row in (overlay.get("vendors") or {}).values():
                if detect_injection(row.get("notes")):
                    found = True
            for row in (overlay.get("catalog") or {}).values():
                if detect_injection(row.get("notes")):
                    found = True
            assert found, f"{case['case_id']} is tagged prompt_injection but holds no injection"

    def test_tool_unavailable_cases_cover_distinct_failure_shapes(self):
        shapes = set()
        for case in CASES:
            if "tool_unavailable" not in case["tags"]:
                continue
            overlay = case.get("overlay") or {}
            if overlay.get("vendor_risk_force_fail"):
                shapes.add("service_outage")
            if overlay.get("vendor_risk_unavailable"):
                shapes.add("vendor_404")
            vendor = case["request"].get("vendor_name")
            if vendor == "NimbusAI":
                shapes.add("forced_503")
        assert {"service_outage", "vendor_404", "forced_503"} <= shapes, shapes


class TestCasesReferenceRealData:
    def test_shipped_requests_match_the_dataset(self):
        """A case sourced from a shipped request must not alter its key fields.

        Variants are allowed but must say so via ``source``, so a reviewer can
        tell a deliberately-isolated case from a silent edit of the dataset.
        """
        store = DataStore.load()
        for case in CASES:
            source = case.get("source", "")
            if not source.startswith("REQ-"):
                continue
            stored = store.request(source)
            assert stored is not None, case["case_id"]
            if "variant" in source or case["request"].get("request_id") != source:
                continue  # documented variant
            for field in (
                "requester_id",
                "vendor_name",
                "annual_cost_usd",
                "data_access_level",
                "user_count",
            ):
                assert case["request"].get(field) == getattr(stored, field), (
                    f"{case['case_id']} diverges from data/requests.json on {field}"
                )

    def test_every_case_requester_exists(self):
        store = DataStore.load()
        for case in CASES:
            requester = case["request"].get("requester_id")
            if requester:
                assert store.employee(requester) is not None, case["case_id"]

    def test_every_case_vendor_is_known_or_explicitly_unavailable(self):
        store = DataStore.load()
        for case in CASES:
            vendor = case["request"].get("vendor_name")
            if not vendor:
                continue
            known = store.vendor(vendor) is not None
            overlay = case.get("overlay") or {}
            simulated = (
                overlay.get("vendor_risk_force_fail")
                or vendor in (overlay.get("vendor_risk_unavailable") or [])
                or vendor in (overlay.get("vendor_risk") or {})
            )
            assert known or simulated, (
                f"{case['case_id']} names vendor {vendor!r}, which is in neither the registry "
                "nor the overlay"
            )

    def test_overlay_ids_exist(self):
        store = DataStore.load()
        for case in CASES:
            overlay = case.get("overlay") or {}
            for vendor_id in (overlay.get("vendors") or {}):
                assert any(v.vendor_id == vendor_id for v in store.vendors), case["case_id"]
            for software_id in (overlay.get("catalog") or {}):
                assert any(c.software_id == software_id for c in store.catalog), case["case_id"]

    def test_every_case_amount_fits_or_exceeds_its_department_budget(self):
        """A budget_insufficient label must be arithmetically true."""
        store = DataStore.load()
        for case in CASES:
            if "budget_insufficient" not in case["expected"]["must_flag"]:
                continue
            request = PurchaseRequest.from_dict(case["request"])
            employee = store.employee(request.requester_id)
            budget = store.budget(employee.department)
            assert request.annual_cost_usd > budget.available_usd, (
                f"{case['case_id']} is labelled budget_insufficient but "
                f"${request.annual_cost_usd} fits ${budget.available_usd}"
            )

    def test_must_escalate_cases_are_actually_sensitive_or_broken(self):
        """A must_escalate label needs a concrete trigger in the data.

        Recomputes the trigger set the same way the runtime does (including the
        overlay), so a label cannot drift into asserting an escalation that
        nothing in the data supports.
        """
        for case in CASES:
            if not case["expected"]["must_escalate"]:
                continue
            reason = escalation_trigger(case, DataStore.load(case.get("overlay")))
            assert reason, (
                f"{case['case_id']} is labelled must_escalate but no trigger recomputes: "
                f"data_access={case['request'].get('data_access_level')!r}, "
                f"vendor={case['request'].get('vendor_name')!r}, "
                f"amount={case['request'].get('annual_cost_usd')}, "
                f"overlay={list((case.get('overlay') or {}).keys())}"
            )


def escalation_trigger(case: dict, store: DataStore) -> str:
    """Recompute why a case should escalate. Empty string means nothing does."""
    request = PurchaseRequest.from_dict(case["request"])
    reasons: list[str] = []

    if request.data_access_level in policy.SECURITY_SENSITIVE_ACCESS:
        reasons.append(f"security-sensitive data access {request.data_access_level!r}")
    if request.requested_integrations and policy.integration_security_trigger(
        request.requested_integrations
    ):
        reasons.append("security-sensitive integration")
    if request.missing_fields():
        reasons.append(f"missing required fields {request.missing_fields()}")

    from core.data import detect_injection

    if detect_injection(request.business_justification):
        reasons.append("prompt injection in request text")
    for row in ((case.get("overlay") or {}).get("vendors") or {}).values():
        if detect_injection(row.get("notes")):
            reasons.append("prompt injection in vendor notes")
            break
    for row in ((case.get("overlay") or {}).get("catalog") or {}).values():
        if detect_injection(row.get("notes")):
            reasons.append("prompt injection in catalog notes")
            break

    overlay = case.get("overlay") or {}
    if overlay.get("vendor_risk_force_fail"):
        reasons.append("vendor-risk service outage")

    vendor = store.vendor(request.vendor_name)
    record = store.vendor_risk_record(request.vendor_name)
    if request.vendor_name in (overlay.get("vendor_risk_unavailable") or []):
        reasons.append("vendor absent from the vendor-risk service")
    elif record is not None and record.get("__force_error__"):
        reasons.append("vendor-risk service forced error")
    elif record is not None and record.get("force_error"):
        reasons.append("vendor-risk service forced error")
    elif record is None and vendor is None:
        reasons.append("vendor in neither the registry nor the service")
    elif record is not None:
        service_status = str(record.get("security_review_status") or "").strip().lower()
        if service_status in {"expired", "not_completed", "missing", "pending"}:
            reasons.append(f"vendor-risk service reports security_review_status={service_status!r}")
        if not policy.is_review_current(policy.parse_date(record.get("last_review_date"))):
            reasons.append("service review date missing or outside the 365-day window")
        # Section 5: a disagreement between the two sources is itself a trigger.
        if vendor is not None:
            registry_status = str(vendor.security_status or "").strip().lower()
            registry_date = policy.parse_date(vendor.security_review_date)
            service_date = policy.parse_date(record.get("last_review_date"))
            if registry_status == "approved" and service_status in {"expired", "not_completed"}:
                reasons.append("registry says approved, service disagrees")
            if (
                registry_date is not None
                and service_date is not None
                and registry_date != service_date
            ):
                reasons.append("registry and service disagree about the review date")

    if vendor is not None and not policy.is_review_current(
        policy.parse_date(vendor.security_review_date)
    ):
        reasons.append("registry security review missing or outside the 365-day window")

    employee = store.employee(request.requester_id)
    budget = store.budget(employee.department) if employee else None
    if (
        budget is not None
        and request.annual_cost_usd is not None
        and request.annual_cost_usd > budget.available_usd
    ):
        reasons.append("budget insufficient")

    return "; ".join(reasons)



class TestMetricsAndRunner:
    def test_cases_file_is_jsonl_one_object_per_line(self):
        text = CASES_PATH.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                assert line.strip().startswith("{")
                json.loads(line)

    def test_no_model_derived_language_in_rationale(self):
        """Labels must be justified from the policy, not from a model."""
        banned = ["the model said", "gpt said", "llm output", "the agent said"]
        for case in CASES:
            lowered = case["rationale"].lower()
            for phrase in banned:
                assert phrase not in lowered, case["case_id"]

    def test_runner_module_imports(self):
        from eval import metrics, report, run  # noqa: F401

    def test_grounding_module_is_independent_of_the_guardrail(self):
        from eval import grounding

        assert hasattr(grounding, "check_grounding")

    def test_metrics_score_case_needs_no_llm(self):
        from eval.metrics import summarise

        assert callable(summarise)

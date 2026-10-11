from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api import app


class ApiValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_unknown_request_id_is_404_not_500(self):
        # DataStore.request returns None for an id it does not hold, so this
        # used to reach analyze() and surface as a TypeError.
        r = self.client.post(
            "/api/analyze", json={"source": "catalog", "request_id": "REQ-0001"}
        )
        self.assertEqual(r.status_code, 404)
        self.assertIn("REQ-0001", r.json()["detail"])

    def test_missing_request_id_is_404(self):
        r = self.client.post("/api/analyze", json={"source": "catalog"})
        self.assertEqual(r.status_code, 404)

    def test_empty_free_text_is_400(self):
        r = self.client.post("/api/analyze", json={"source": "free_text", "free_text": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertIn("empty", r.json()["detail"])

    def test_missing_source_is_422(self):
        r = self.client.post("/api/analyze", json={"free_text": "anything"})
        self.assertEqual(r.status_code, 422)

    def test_employees_carry_department_and_head_context(self):
        """The requester picker needs department, level and the resolved
        department head — the same rule tools/employee.department_head() uses."""
        employees = {e["employee_id"]: e for e in self.client.get("/api/requests").json()["employees"]}

        sarah = employees["E001"]
        self.assertEqual(sarah["department"], "Marketing")
        self.assertEqual(sarah["department_head"], {"employee_id": "E009", "name": "David Chen"})
        self.assertEqual(sarah["budget"]["available_usd"], 15000)

        david = employees["E009"]
        self.assertTrue(david["is_department_head"])
        self.assertEqual(david["department"], "Marketing")

        # A VP is not a Director, so they never resolve as a department head.
        lisa = employees["E010"]
        self.assertFalse(lisa["is_department_head"])
        self.assertIsNone(lisa["department_head"])


class ReferenceDataTests(unittest.TestCase):
    """/api/reference mirrors core/policy.py so a reviewer can cross-check a
    verdict against the source the guardrail actually read."""

    @classmethod
    def setUpClass(cls):
        cls.data = TestClient(app).get("/api/reference").json()

    def test_policy_header_is_exposed(self):
        self.assertEqual(self.data["policy_version"], "2026.09")
        self.assertEqual(self.data["reference_date"], "2026-09-30")
        self.assertEqual(self.data["security_review_validity_days"], 365)

    def test_thresholds_match_the_policy_table(self):
        tiers = self.data["thresholds"]
        self.assertEqual(len(tiers), 4)
        self.assertEqual(tiers[0]["approvers"], ["Manager"])
        self.assertEqual(tiers[1]["approvers"], ["Department Head", "Procurement"])
        self.assertEqual(
            tiers[2]["approvers"], ["Department Head", "Finance", "Procurement"]
        )
        self.assertEqual(
            tiers[3]["approvers"],
            ["Department Head", "Finance", "CFO", "Procurement"],
        )
        self.assertIsNone(tiers[3]["max_usd"])

    def test_expired_and_current_reviews_are_distinguished(self):
        by_name = {v["vendor_name"]: v for v in self.data["vendors"]}

        # Reviewed 2025-07-01 -> expired 365 days later, before the reference date.
        signalwatch = by_name["SignalWatch"]
        self.assertFalse(signalwatch["security_review_current"])
        self.assertEqual(signalwatch["security_review_date"], "2025-07-01")

        creative = by_name["CreativeSuite Labs"]
        self.assertTrue(creative["security_review_current"])

        # A new vendor with no review on record is not "still current".
        brandboard = by_name["BrandBoard"]
        self.assertTrue(brandboard["is_new"])
        self.assertIsNone(brandboard["security_review_date"])
        self.assertFalse(brandboard["security_review_current"])

    def test_department_budgets_are_exposed(self):
        budgets = {b["department"]: b for b in self.data["budgets"]}
        self.assertEqual(budgets["Marketing"]["available_usd"], 15000)
        self.assertEqual(budgets["Engineering"]["available_usd"], 26000)


if __name__ == "__main__":
    unittest.main()

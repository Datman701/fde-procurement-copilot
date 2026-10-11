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


if __name__ == "__main__":
    unittest.main()

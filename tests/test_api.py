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


if __name__ == "__main__":
    unittest.main()

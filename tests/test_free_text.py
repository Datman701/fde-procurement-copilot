"""Free-text extraction tests.

The extractor must be conservative: it recovers only what the prose states
explicitly, resolves vendor/product against real data, and never reads a seat
count as a price.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDOR_RISK_TRANSPORT", "inprocess")

from app.analyze import analyze, _coerce_request
from core.data import DataStore, PurchaseRequest
from core.free_text import extract

PROSE = (
    "I need PixelCraft Pro for the design team. Requested by Sarah Lee. "
    "Annual cost is $32,000 for 65 seats. This handles customer data for "
    "marketing campaigns. High priority, please process ASAP."
)

_STORE: DataStore | None = None


def store() -> DataStore:
    """One load per module; DataStore.load() reads every data file."""
    global _STORE
    if _STORE is None:
        _STORE = DataStore.load()
    return _STORE


class TestMoney:
    def test_dollar_amount_is_a_price(self):
        fields, _ = extract("Costs $9,000 a year", store=store())
        assert fields["annual_cost_usd"] == 9000.0

    def test_thousands_suffix(self):
        fields, _ = extract("The price is $12k", store=store())
        assert fields["annual_cost_usd"] == 12000.0

    def test_unit_words(self):
        for prose in ("9000 USD", "9000 per year", "cost is 9000"):
            fields, _ = extract(prose, store=store())
            assert fields["annual_cost_usd"] == 9000.0, prose

    def test_seat_count_is_never_read_as_a_price(self):
        fields, _ = extract("Please buy 12 seats for the team", store=store())
        assert fields["annual_cost_usd"] is None
        assert fields["user_count"] == 12

    def test_absent_cost_stays_absent(self):
        fields, _ = extract("We need this tool for the team", store=store())
        assert fields["annual_cost_usd"] is None


class TestCounts:
    def test_seats(self):
        fields, _ = extract("65 seats please", store=store())
        assert fields["user_count"] == 65

    def test_labelled_form(self):
        fields, _ = extract("users: 3", store=store())
        assert fields["user_count"] == 3


class TestCatalogResolution:
    def test_product_and_vendor_resolve_against_real_data(self):
        fields, trail = extract(PROSE, store=store())
        assert fields["product_name"] == "PixelCraft Pro"
        assert fields["vendor_name"] == "PixelCraft"
        assert fields["category"] == "Design & Creative"
        assert "vendor_name" in {item.field for item in trail}

    def test_unknown_product_falls_back_to_a_short_snippet(self):
        fields, _ = extract(
            "A tool nobody has heard of, because our workflow needs it.",
            store=store(),
        )
        assert len(fields["product_name"]) <= 80

    def test_a_free_text_request_cannot_invent_a_vendor(self):
        fields, _ = extract("For VendorNotInRegistry Inc, $100", store=store())
        assert fields["vendor_name"] == ""


class TestDataAccessLevel:
    def test_explicit_customer_data(self):
        fields, _ = extract("It will process customer data", store=store())
        assert fields["data_access_level"] == "customer_pii"

    def test_negation_wins_over_the_keyword(self):
        fields, _ = extract(
            "It never touches customer data, marketing assets only.",
            store=store(),
        )
        assert fields["data_access_level"] != "customer_pii"

    def test_source_code(self):
        fields, _ = extract("Needs read access to our source code", store=store())
        assert fields["data_access_level"] == "source_code"

    def test_unstated_stays_unknown(self):
        fields, _ = extract("Just a nice to have, $500", store=store())
        assert fields["data_access_level"] == "unknown"


class TestRequesterAndUrgency:
    def test_explicit_named_requester_resolves_to_an_employee_id(self):
        fields, _ = extract(PROSE, store=store())
        assert fields["requester_id"] == "E001"

    def test_requester_from_the_form_is_used_when_not_named(self):
        fields, _ = extract("Need the tool, $100", store=store(), requester_id="E007")
        assert fields["requester_id"] == "E007"

    def test_explicit_urgency(self):
        fields, _ = extract(PROSE, store=store())
        assert fields["urgency"] == "urgent"

    def test_urgency_defaults_to_normal(self):
        fields, _ = extract("Need the tool, $100", store=store())
        assert fields["urgency"] == "normal"


class TestCoercion:
    def test_known_request_id_is_not_reparsed(self):
        request, trail = _coerce_request("REQ-1001", store())
        assert isinstance(request, PurchaseRequest)
        assert request.request_id == "REQ-1001"
        assert trail == []

    def test_free_text_becomes_a_complete_request(self):
        request, trail = _coerce_request(PROSE, store())
        assert request.request_id == "FREE-TEXT"
        assert request.annual_cost_usd == 32000.0
        assert request.missing_fields() == []
        assert trail


class TestAnalyzeIntegration:
    def _client(self, draft):
        from core.llm_client import DiskCache, LLMClient, LLMConfig, LLMResponse, ToolCall

        class Scripted:
            def __init__(self):
                self.responses = [draft]

            def __call__(self, messages, tools, response_schema, temperature):
                return self.responses.pop(0) if self.responses else LLMResponse(text="{}")

        config = LLMConfig(
            provider="gemini",
            model="gemini-2.5-flash",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            requests_per_minute=100_000,
            max_calls_per_run=20,
        )
        client = LLMClient(config, cache=DiskCache(enabled=False), use_cache=False, transport="scripted")
        client.provider_impl = Scripted()
        return client, ToolCall

    def test_free_text_run_reports_what_it_read(self):
        from core.llm_client import LLMResponse, ToolCall

        draft = LLMResponse(
            text="",
            tool_calls=[
                ToolCall("submit_decision", {
                    "recommendation": "proceed_with_approvals",
                    "severity": "PROCEED",
                    "evidence": [{
                        "claim": "PixelCraft Pro costs $32,000",
                        "result_id": "t03-budget_check",
                        "quote_or_value": "32000",
                    }],
                    "missing_information": [],
                    "next_step": "Send to the manager for approval.",
                    "extra_flags": [],
                    "extra_approvers": [],
                })
            ],
        )
        client, _ = self._client(draft)
        captured: list = []
        decision = analyze(
            PROSE,
            architecture="A",
            client=client,
            vendor_transport="inprocess",
            extraction_sink=captured.extend,
        )
        assert decision.request_id == "FREE-TEXT"
        assert captured, "free-text runs must report extracted fields"
        fields = {item["field"] for item in captured}
        assert {"annual_cost_usd", "user_count", "vendor_name", "product_name"} <= fields

    def test_catalog_run_reports_nothing_extracted(self):
        from core.llm_client import LLMResponse, ToolCall

        draft = LLMResponse(
            text="",
            tool_calls=[
                ToolCall("submit_decision", {
                    "recommendation": "proceed_with_approvals",
                    "severity": "PROCEED",
                    "evidence": [{
                        "claim": "Sales has $18,000 available",
                        "result_id": "t03-budget_check",
                        "quote_or_value": "18000",
                    }],
                    "missing_information": [],
                    "next_step": "Send to the manager for approval.",
                    "extra_flags": [],
                    "extra_approvers": [],
                })
            ],
        )
        client, _ = self._client(draft)
        captured: list = []
        analyze(
            "REQ-1001",
            architecture="A",
            client=client,
            vendor_transport="inprocess",
            extraction_sink=captured.extend,
        )
        assert captured == []

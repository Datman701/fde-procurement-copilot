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

    def test_unknown_product_is_left_missing_not_invented_from_prose(self):
        fields, trail = extract(
            "A tool nobody has heard of, because our workflow needs it.",
            store=store(),
        )
        assert not fields["product_name"]
        assert "product_name" not in {item.field for item in trail}

    def test_a_long_opening_clause_does_not_become_the_product_name(self):
        # An 85-character first sentence used to be copied verbatim into
        # product_name, which is a wall of text rather than a product.
        fields, trail = extract(
            "We need a tool for our team to store internal documents and "
            "PDFs and share them with external vendors. Not urgent.",
            store=store(),
        )
        assert not fields["product_name"]
        assert "product_name" not in {item.field for item in trail}

    def test_product_evidence_cites_the_matched_term_not_the_opening_clause(self):
        # "Quick one — the contracts team needs SignFlow again" used to be
        # cited as "Quick on", which justifies nothing.
        _, trail = extract(
            "Quick one — the contracts team needs SignFlow again for Q4, "
            "$780 for the year, 5 seats.",
            store=store(),
        )
        evidence = [e.evidence for e in trail if e.field == "product_name"]
        assert evidence == ["SignFlow"]

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

    def test_nothing_that_touches_customer_data_is_not_customer_pii(self):
        # A real request said "nothing that touches customer data" and was
        # filed as customer_pii, because \bno\b does not match inside
        # "nothing". That false positive triggered a security and privacy
        # escalation on a request that had explicitly ruled them out.
        fields, _ = extract(
            "DocSpace for legal, $24,000 for 200 seats. All the files are our "
            "own contracts — internal documents only, nothing that touches "
            "customer data.",
            store=store(),
        )
        assert fields["data_access_level"] == "internal_documents"

    def test_other_negation_forms_are_masked(self):
        for prose in (
            "This isn't customer data.",
            "This is not customer data.",
            "None of this is customer data.",
            "It won't hold customer data.",
            "No customer data at all.",
        ):
            fields, _ = extract(prose, store=store())
            assert fields["data_access_level"] != "customer_pii", prose

    def test_a_negation_does_not_mask_a_later_separate_mention(self):
        # "no" earlier in the sentence must not silence a genuine later use.
        fields, _ = extract(
            "There is no budget line for this yet. It will hold customer data "
            "from the billing system.",
            store=store(),
        )
        assert fields["data_access_level"] == "customer_pii"

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

    def test_explicit_form_requester_beats_the_requester_named_in_prose(self):
        # PROSE says "Requested by Sarah Lee" (E001). An explicit form choice
        # must win, otherwise a typed requester silently changes department.
        fields, trail = extract(PROSE, store=store(), requester_id="E007")
        assert fields["requester_id"] == "E007"
        evidence = [e.evidence for e in trail if e.field == "requester_id"]
        assert evidence == ["requester selected in the form"]

    def test_a_conflicting_prose_requester_is_surfaced_not_silently_dropped(self):
        _, trail = extract(PROSE, store=store(), requester_id="E007")
        conflicts = [e for e in trail if e.field == "requester_conflict"]
        assert len(conflicts) == 1
        assert conflicts[0].value == "E001"
        assert "Sarah Lee" in conflicts[0].evidence
        assert "E007" in conflicts[0].evidence

    def test_no_conflict_recorded_when_prose_and_form_agree(self):
        _, trail = extract(PROSE, store=store(), requester_id="E001")
        assert [e for e in trail if e.field == "requester_conflict"] == []

    def test_explicit_urgency(self):
        fields, _ = extract(PROSE, store=store())
        assert fields["urgency"] == "urgent"

    def test_urgency_defaults_to_normal(self):
        fields, _ = extract("Need the tool, $100", store=store())
        assert fields["urgency"] == "normal"

    def test_negated_urgency_is_not_urgent(self):
        # A real request that said "Not urgent, would just be good to have
        # before the January renewals" was being filed as urgent.
        fields, trail = extract(
            "Need SignFlow, $780 for the year, 5 seats. "
            "Not urgent, would just be good to have before the January renewals.",
            store=store(),
        )
        assert fields["urgency"] == "normal"

    def test_negated_urgency_is_recorded_so_the_writer_can_see_it_was_read(self):
        _, trail = extract(
            "Need SignFlow, $780. Not urgent, next quarter is fine.",
            store=store(),
        )
        evidence = [e.evidence for e in trail if e.field == "urgency"]
        assert evidence == ["Not urgent"]

    def test_other_negations_of_urgency(self):
        for prose in (
            "Need SignFlow, $780. No rush, whenever.",
            "Need SignFlow, $780. Not ASAP, just whenever.",
            "Need SignFlow, $780. Nothing urgent about this.",
        ):
            fields, _ = extract(prose, store=store())
            assert fields["urgency"] == "normal", prose

    def test_a_negation_earlier_in_the_sentence_does_not_mask_a_later_urgency(self):
        fields, _ = extract(
            "Need SignFlow, $780. Not next month, I need it ASAP.",
            store=store(),
        )
        assert fields["urgency"] == "urgent"


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

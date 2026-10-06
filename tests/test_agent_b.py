"""Architecture B tests: analyst loop + tighten-only reviewer enforcement."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("VENDOR_RISK_TRANSPORT", "inprocess")

from agents.agent_b import AgentB, ReviewOutcome, apply_reviewer
from app.analyze import analyze
from core.llm_client import LLMResponse, ToolCall
from core.schema import Trace
from tests.test_agent_a import GOOD_DRAFT, Scripted, TOOL_ROUND_1, TOOL_ROUND_2, client_for


def review(severity="PROCEED", flags=None, missing=None, note=""):
    return LLMResponse(
        text="",
        tool_calls=[
            ToolCall(
                "submit_review",
                {
                    "severity": severity,
                    "reason": "reviewed",
                    "added_flags": flags or [],
                    "added_missing_information": missing or [],
                    "reviewer_note": note,
                },
            )
        ],
    )


ANALYST_ROUND_1 = LLMResponse(
    text="",
    tool_calls=[
        ToolCall("request_validation", {"request_id": "REQ-1005"}),
        ToolCall("employee_lookup", {"request_id": "REQ-1005"}),
        ToolCall("budget_check", {"request_id": "REQ-1005"}),
        ToolCall("vendor_status", {"request_id": "REQ-1005"}),
        ToolCall("catalog_search", {"request_id": "REQ-1005"}),
    ],
)
ANALYST_ROUND_2 = LLMResponse(
    text="",
    tool_calls=[
        ToolCall(
            "approval_router",
            {
                "request_id": "REQ-1005",
                "vendor_result_id": "t04-vendor_status",
                "budget_result_id": "t03-budget_check",
            },
        )
    ],
)


class TestReviewerTightenOnly:
    def _draft(self, severity="PROCEED"):
        return {
            "recommendation": "proceed_with_approvals",
            "severity": severity,
            "evidence": [],
            "missing_information": [],
            "next_step": "Proceed.",
            "extra_flags": [],
        }

    def test_reviewer_can_raise_severity(self):
        trace = Trace(architecture="B")
        merged = apply_reviewer(
            self._draft("PROCEED"), ReviewOutcome("ESCALATE_TO_HUMAN", [], [], ""), trace
        )
        assert merged["severity"] == "ESCALATE_TO_HUMAN"

    def test_reviewer_cannot_lower_severity(self):
        trace = Trace(architecture="B")
        merged = apply_reviewer(
            self._draft("ESCALATE_TO_HUMAN"), ReviewOutcome("PROCEED", [], [], ""), trace
        )
        assert merged["severity"] == "ESCALATE_TO_HUMAN"
        assert any("would loosen" in note for note in trace.reviewer_tightened)

    @pytest.mark.parametrize(
        ("draft_sev", "review_sev", "expected"),
        [
            ("PROCEED", "PROCEED", "PROCEED"),
            ("PROCEED", "NEEDS_APPROVAL", "NEEDS_APPROVAL"),
            ("PROCEED", "ESCALATE_TO_HUMAN", "ESCALATE_TO_HUMAN"),
            ("NEEDS_APPROVAL", "PROCEED", "NEEDS_APPROVAL"),
            ("NEEDS_APPROVAL", "NEEDS_APPROVAL", "NEEDS_APPROVAL"),
            ("NEEDS_APPROVAL", "ESCALATE_TO_HUMAN", "ESCALATE_TO_HUMAN"),
            ("ESCALATE_TO_HUMAN", "PROCEED", "ESCALATE_TO_HUMAN"),
            ("ESCALATE_TO_HUMAN", "NEEDS_APPROVAL", "ESCALATE_TO_HUMAN"),
            ("ESCALATE_TO_HUMAN", "ESCALATE_TO_HUMAN", "ESCALATE_TO_HUMAN"),
        ],
    )
    def test_severity_is_always_the_maximum(self, draft_sev, review_sev, expected):
        trace = Trace(architecture="B")
        merged = apply_reviewer(self._draft(draft_sev), ReviewOutcome(review_sev, [], [], ""), trace)
        assert merged["severity"] == expected

    def test_reviewer_flags_are_unioned_not_replaced(self):
        trace = Trace(architecture="B")
        draft = self._draft()
        draft["extra_flags"] = ["analyst_flag"]
        merged = apply_reviewer(
            draft, ReviewOutcome("NEEDS_APPROVAL", ["reviewer_flag"], [], ""), trace
        )
        assert merged["extra_flags"] == ["analyst_flag", "reviewer_flag"]

    def test_reviewer_cannot_remove_draft_flags(self):
        trace = Trace(architecture="B")
        draft = self._draft()
        draft["extra_flags"] = ["analyst_flag"]
        merged = apply_reviewer(
            draft, ReviewOutcome("NEEDS_APPROVAL", [], [], ""), trace
        )
        assert "analyst_flag" in merged["extra_flags"]

    def test_missing_information_is_unioned(self):
        trace = Trace(architecture="B")
        merged = apply_reviewer(
            self._draft(), ReviewOutcome("NEEDS_APPROVAL", [], ["retention policy"], ""), trace
        )
        assert "retention policy" in merged["missing_information"]

    def test_reviewer_adds_no_evidence(self):
        """The reviewer has no tools, so it cannot cite a result."""
        trace = Trace(architecture="B")
        draft = self._draft()
        draft["evidence"] = [{"claim": "c", "result_id": "t01-x", "quote_or_value": "1"}]
        merged = apply_reviewer(
            draft, ReviewOutcome("NEEDS_APPROVAL", ["new_flag"], ["new_missing"], ""), trace
        )
        assert merged["evidence"] == draft["evidence"]

    def test_unknown_reviewer_severity_is_ignored(self):
        trace = Trace(architecture="B")
        merged = apply_reviewer(
            self._draft("NEEDS_APPROVAL"), ReviewOutcome("SUPER_URGENT", [], [], ""), trace
        )
        assert merged["severity"] == "NEEDS_APPROVAL"


def _clean_case_responses(draft: LLMResponse) -> list[LLMResponse]:
    tools = LLMResponse(
        text="",
        tool_calls=[
            ToolCall("request_validation", {"request_id": "REQ-1001"}),
            ToolCall("employee_lookup", {"request_id": "REQ-1001"}),
            ToolCall("budget_check", {"request_id": "REQ-1001"}),
            ToolCall("vendor_status", {"request_id": "REQ-1001"}),
            ToolCall("catalog_search", {"request_id": "REQ-1001"}),
            ToolCall("approval_router", {"request_id": "REQ-1001"}),
        ],
    )
    return [tools, draft]


class TestAgentBLoop:
    def test_analyst_then_reviewer_produces_a_decision(self):
        script = Scripted([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT, review("PROCEED")])
        decision = analyze("REQ-1005", architecture="B", client=client_for(script))
        assert decision.trace.architecture == "B"
        assert decision.severity == "ESCALATE_TO_HUMAN"
        assert decision.recommendation == "escalate_security_review"

    def test_reviewer_adds_one_extra_llm_call(self):
        script = Scripted([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT, review("PROCEED")])
        decision = analyze("REQ-1005", architecture="B", client=client_for(script))
        assert decision.trace.llm_calls == 4

    def test_reviewer_can_raise_severity_on_a_clean_request(self):
        """The reviewer's only real power on a low-risk request is to tighten."""
        draft = LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    "submit_decision",
                    {
                        "recommendation": "proceed_with_approvals",
                        "severity": "PROCEED",
                        "evidence": [],
                        "missing_information": [],
                        "next_step": "Send to the line manager.",
                    },
                )
            ],
        )
        escalate = review(
            "ESCALATE_TO_HUMAN",
            flags=["unusual_vendor_terms"],
            missing=["data retention policy"],
            note="no retention policy on file",
        )
        script = Scripted(_clean_case_responses(draft) + [escalate])
        decision = analyze("REQ-1001", architecture="B", client=client_for(script))
        assert decision.severity == "ESCALATE_TO_HUMAN"
        assert "unusual_vendor_terms" in decision.flag_names()
        assert "data retention policy" in decision.missing_information
        assert decision.needs_human_review is True

    def test_reviewer_cannot_loosen_an_escalated_draft(self):
        draft = LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    "submit_decision",
                    {
                        "recommendation": "escalate_security_review",
                        "severity": "ESCALATE_TO_HUMAN",
                        "evidence": [],
                        "missing_information": [],
                        "next_step": "Route to Security.",
                        "extra_flags": ["analyst_worry"],
                    },
                )
            ],
        )
        loosen = review("PROCEED", flags=[], missing=[], note="looks fine to me")
        script = Scripted(_clean_case_responses(draft) + [loosen])
        decision = analyze("REQ-1001", architecture="B", client=client_for(script))
        assert decision.severity == "ESCALATE_TO_HUMAN"
        assert "analyst_worry" in decision.flag_names()

    def test_reviewer_failure_does_not_crash_the_run(self):
        class Dead:
            def __init__(self):
                self.n = 0
                self.script = list([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT])

            def __call__(self, messages, tools=None, response_schema=None, temperature=0):
                self.n += 1
                if self.n == 4:
                    from core.llm_client import _RetryableError

                    raise _RetryableError("429")
                return self.script.pop(0)

        client = client_for(Scripted([]))
        client.provider_impl = Dead()
        decision = analyze("REQ-1005", architecture="B", client=client)
        # Stage 1 succeeded, so we still get a real decision from the guardrail.
        assert decision.severity in {"NEEDS_APPROVAL", "ESCALATE_TO_HUMAN"}
        assert decision.request_id == "REQ-1005"

    def test_reviewer_sees_the_raw_tool_results(self):
        script = Scripted([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT, review("PROCEED")])
        analyze("REQ-1005", architecture="B", client=client_for(script))
        reviewer_prompt = str(script.calls[-1]["messages"])
        assert "Evidence pack" in reviewer_prompt
        assert "tool_results" in reviewer_prompt
        assert "t04-vendor_status" in reviewer_prompt

    def test_reviewer_is_told_it_has_no_tools(self):
        """The reviewer call must not expose the business tool layer."""
        script = Scripted([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT, review("PROCEED")])
        analyze("REQ-1005", architecture="B", client=client_for(script))
        tools = script.calls[-1]["tools"]
        assert tools is not None and len(tools) == 1
        assert tools[0]["function"]["name"] == "submit_review"
        system_prompt = str(script.calls[-1]["messages"][0])
        assert "no tools" in system_prompt.lower()

    def test_both_architectures_share_the_guardrail(self):
        script_a = Scripted([TOOL_ROUND_1, TOOL_ROUND_2, GOOD_DRAFT])
        decision_a = analyze("REQ-1005", architecture="A", client=client_for(script_a))
        script_b = Scripted([ANALYST_ROUND_1, ANALYST_ROUND_2, GOOD_DRAFT, review("PROCEED")])
        decision_b = analyze("REQ-1005", architecture="B", client=client_for(script_b))
        assert decision_a.recommendation == decision_b.recommendation
        assert decision_a.severity == decision_b.severity
        assert decision_a.approver_names() == decision_b.approver_names()
        assert decision_a.flag_names() == decision_b.flag_names()



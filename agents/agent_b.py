"""Architecture B - lightweight staged / two-agent variant.

    Stage 1  Procurement Analyst   has the tools; produces an evidence pack and a draft Decision
    Stage 2  Policy/Risk Reviewer  has NO tools; sees the evidence pack and may only tighten

The reviewer's tighten-only power is enforced **in code**, not by the prompt:
:func:`apply_reviewer` takes ``max(draft_severity, reviewer_severity)`` and the
union of flags, approvals and missing information, so no reviewer output can
loosen anything even if it tries.

Both stages pass through the same guardrail as Architecture A.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agents import prompts
from core import guardrail
from core.data import PurchaseRequest
from core.llm_client import LLMClient, LLMError
from core.schema import Decision, ToolResult, Trace, max_severity
from tools.registry import ToolRegistry

MAX_TOOL_ROUNDS = 4
SEVERITY_VALUES = ("PROCEED", "NEEDS_APPROVAL", "ESCALATE_TO_HUMAN")


def _wire_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """The OpenAI ``tools`` shape, so tests can see exactly what is offered."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {}),
        },
    }


@dataclass
class AgentOutcome:
    draft: dict[str, Any] | None
    error: str | None
    schema_attempts: int


@dataclass
class ReviewOutcome:
    severity: str
    added_flags: list[str]
    added_missing: list[str]
    note: str
    error: str | None = None


class AgentB:
    """Two agents, one shared tool layer, one shared guardrail."""

    architecture = "B"

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.client = client
        self.registry = registry
        self.max_rounds = max_rounds

    # -- main entry -------------------------------------------------------- #
    def run(self, request: PurchaseRequest) -> Decision:
        started = time.perf_counter()
        trace = Trace(architecture="B")

        try:
            draft, outcome = self._analyst(request, trace)
        except LLMError as exc:
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            trace.llm_calls = self.client.llm_calls
            return guardrail.fallback(
                request, self.registry.all(), trace, f"LLM unavailable: {exc}"
            )

        trace.schema_retries = outcome.schema_attempts
        if outcome.error:
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            trace.llm_calls = self.client.llm_calls
            return guardrail.fallback(request, self.registry.all(), trace, outcome.error)

        try:
            validated = guardrail.validate_draft(outcome.draft)
        except guardrail.GuardrailRejected as exc:
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            trace.llm_calls = self.client.llm_calls
            return guardrail.fallback(request, self.registry.all(), trace, str(exc))

        # ---- stage 2: reviewer, tighten-only ------------------------------ #
        review = self._review(request, draft=outcome.draft or {}, trace=trace)
        validated = apply_reviewer(validated, review, trace)

        trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        trace.llm_calls = self.client.llm_calls
        trace.llm_cached_calls = self.client.cached_calls

        return guardrail.apply(validated, self.registry.all(), request, trace)

    # -- stage 1: analyst -------------------------------------------------- #
    def _analyst(
        self, request: PurchaseRequest, trace: Trace
    ) -> tuple[dict[str, Any] | None, AgentOutcome]:
        request_block = prompts.render_request(request)
        messages = prompts.analyst_b_initial(request_block)
        transcript: list[dict[str, Any]] = []
        schema_attempts = 0
        tools = prompts.all_tool_schemas() + [prompts.DECISION_TOOL]

        for round_index in range(self.max_rounds + 1):
            response = self.client.chat(messages, tools=tools)

            submitted = self._submitted(response)
            if submitted is not None:
                return submitted, AgentOutcome(submitted, None, schema_attempts)

            if response.tool_calls:
                transcript.extend(self._execute_tool_calls(response, trace))
                messages = prompts.analyst_b_after_tools(request_block, transcript)
                continue

            draft = self._coerce(response)
            if draft is not None:
                return draft, AgentOutcome(draft, None, schema_attempts)

            schema_attempts += 1
            if schema_attempts > 1:
                return None, AgentOutcome(
                    None,
                    "analyst did not return a valid decision object after one repair retry",
                    schema_attempts,
                )
            if round_index >= self.max_rounds:
                messages = list(messages) + prompts.agent_a_final_reminder()
            else:
                messages = list(messages) + [
                    {"role": "assistant", "content": response.text[:2000]},
                    {
                        "role": "user",
                        "content": (
                            "Reply with the single JSON decision object only. No prose, no "
                            "markdown fence. Required keys: recommendation, severity, "
                            "evidence, missing_information, next_step."
                        ),
                    },
                ]
            transcript = []

        messages = list(messages) + prompts.agent_a_final_reminder()
        response = self.client.chat(messages, tools=None)
        draft = self._coerce(response)
        if draft is not None:
            return draft, AgentOutcome(draft, None, schema_attempts)
        return None, AgentOutcome(
            None,
            f"tool round cap ({self.max_rounds}) reached without a valid decision",
            schema_attempts,
        )

    # -- stage 2: reviewer ------------------------------------------------- #
    def _review(
        self, request: PurchaseRequest, draft: dict[str, Any], trace: Trace
    ) -> ReviewOutcome:
        """Run the reviewer. A reviewer failure is never fatal - it just means
        no tightening was applied, and the shared guardrail still applies."""
        pack = prompts.evidence_pack(request, self.registry.all())
        messages = prompts.reviewer_initial(pack)
        try:
            response = self.client.chat(
                messages, tools=[_wire_tool(prompts.REVIEWER_TOOL)]
            )
        except LLMError as exc:
            trace.guardrail_notes.append(f"reviewer unavailable, continuing without it: {exc}")
            return ReviewOutcome(
                severity=draft.get("severity", "NEEDS_APPROVAL"),
                added_flags=[],
                added_missing=[],
                note=f"reviewer unavailable: {exc}",
                error=str(exc),
            )

        payload = self._coerce_review(response)
        if payload is None:
            trace.guardrail_notes.append(
                "reviewer returned unusable output; continuing with the analyst draft only"
            )
            return ReviewOutcome(
                severity=draft.get("severity", "NEEDS_APPROVAL"),
                added_flags=[],
                added_missing=[],
                note="reviewer output unusable",
                error="reviewer output unusable",
            )

        severity = str(payload.get("severity", "")).strip().upper()
        if severity not in SEVERITY_VALUES:
            severity = draft.get("severity", "NEEDS_APPROVAL")

        return ReviewOutcome(
            severity=severity,
            added_flags=[str(f) for f in (payload.get("added_flags") or []) if str(f).strip()],
            added_missing=[
                str(m)
                for m in (payload.get("added_missing_information") or [])
                if str(m).strip()
            ],
            note=str(payload.get("reviewer_note") or payload.get("reason") or "").strip(),
        )

    @staticmethod
    def _coerce_review(response: Any) -> dict[str, Any] | None:
        from core.llm_client import extract_json

        for call in response.tool_calls:
            if call.name == "submit_review" and call.arguments:
                return call.arguments
        if response.parsed and "severity" in response.parsed:
            return response.parsed
        try:
            parsed = extract_json(response.text)
        except LLMError:
            return None
        return parsed if isinstance(parsed, dict) and "severity" in parsed else None

    # -- shared helpers ---------------------------------------------------- #
    @staticmethod
    def _submitted(response: Any) -> dict[str, Any] | None:
        for call in response.tool_calls:
            if call.name == "submit_decision" and call.arguments:
                return call.arguments
        return None

    @staticmethod
    def _coerce(response: Any) -> dict[str, Any] | None:
        from core.llm_client import extract_json

        submitted = AgentB._submitted(response)
        if submitted is not None:
            return submitted
        if response.parsed and "recommendation" in response.parsed:
            return response.parsed
        try:
            parsed = extract_json(response.text)
        except LLMError:
            return None
        return parsed if isinstance(parsed, dict) and "recommendation" in parsed else None

    def _execute_tool_calls(self, response: Any, trace: Trace) -> list[dict[str, Any]]:
        from agents.agent_a import _sanitise_args

        transcript: list[dict[str, Any]] = []
        results: list[ToolResult] = []

        for call in response.tool_calls:
            if call.name == "submit_decision":
                continue
            if call.name not in self.registry.names:
                trace.guardrail_notes.append(
                    f"rejected tool call {call.name!r}: not available to the analyst"
                )
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{len(results)}",
                        "name": call.name,
                        "content": (
                            f"ERROR: tool {call.name!r} is not available. Available tools: "
                            f"{self.registry.names}. No action was taken."
                        ),
                    }
                )
                continue
            results.append(self.registry.call(call.name, _sanitise_args(call.arguments)))

        callable_requests = [c for c in response.tool_calls if c.name != "submit_decision"]
        if callable_requests:
            transcript.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for index, call in enumerate(callable_requests)
                    ],
                }
            )
        for index, result in enumerate(results):
            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{index}",
                    "name": result.tool,
                    "content": result.brief(),
                }
            )
        return transcript


# --------------------------------------------------------------------------- #
# Tighten-only merge (code, not prompt)
# --------------------------------------------------------------------------- #
def apply_reviewer(
    draft: dict[str, Any], review: ReviewOutcome, trace: Trace
) -> dict[str, Any]:
    """Merge the reviewer's verdict into the draft. Cannot loosen anything.

    * severity  -> ``max(draft, reviewer)``
    * flags     -> union
    * missing   -> union
    * evidence  -> untouched (the reviewer has no tools, so it can add none)
    """
    merged = dict(draft)

    before = str(merged.get("severity", "")).upper()
    after = max_severity(before, review.severity)
    merged["severity"] = after
    if after != before:
        trace.reviewer_tightened.append(f"severity {before} -> {after}")
    if review.severity and after == before:
        trace.reviewer_tightened.append(
            f"reviewer proposed {review.severity}; not applied because it would loosen"
        )

    flags = list(merged.get("extra_flags") or [])
    for flag in review.added_flags:
        if flag not in flags:
            flags.append(flag)
            trace.reviewer_tightened.append(f"added risk flag {flag}")
    merged["extra_flags"] = flags

    missing = list(merged.get("missing_information") or [])
    for item in review.added_missing:
        if item not in missing:
            missing.append(item)
            trace.reviewer_tightened.append(f"added missing information: {item}")
    merged["missing_information"] = missing

    if review.note:
        trace.guardrail_notes.append(f"reviewer: {review.note}")
    return merged


__all__ = ["MAX_TOOL_ROUNDS", "AgentB", "ReviewOutcome", "apply_reviewer"]

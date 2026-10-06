"""Architecture A - single-agent baseline.

One agent, tool calling, a capped number of tool rounds (default 4), structured
final output, temperature 0.

Loop:

    plan  -> call tools (bounded) -> read results -> structured decision
                     ^                                        |
                     +------------- if more evidence needed ----+

The agent decides *which* tools to call and in what order. It never computes a
threshold, a budget or a required approval. The shared guardrail then applies
afterwards, exactly as for Architecture B.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agents import prompts
from core import guardrail
from core.data import PurchaseRequest
from core.llm_client import LLMClient, LLMError
from core.schema import Decision, ToolResult, Trace
from tools.registry import ToolRegistry

MAX_TOOL_ROUNDS = 4


@dataclass
class AgentOutcome:
    draft: dict[str, Any] | None
    error: str | None
    schema_attempts: int


class AgentA:
    """Single-agent baseline."""

    architecture = "A"

    def __init__(self, client: LLMClient, registry: ToolRegistry, max_rounds: int = MAX_TOOL_ROUNDS) -> None:
        self.client = client
        self.registry = registry
        self.max_rounds = max_rounds

    # -- main entry -------------------------------------------------------- #
    def run(self, request: PurchaseRequest) -> Decision:
        started = time.perf_counter()
        trace = Trace(architecture="A")

        try:
            draft, outcome = self._gather_and_decide(request, trace)
        except LLMError as exc:
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            trace.llm_calls = self.client.llm_calls
            trace.schema_retries = 0
            return guardrail.fallback(
                request, self.registry.all(), trace, f"LLM unavailable: {exc}"
            )

        trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        trace.llm_calls = self.client.llm_calls
        trace.llm_cached_calls = self.client.cached_calls

        trace.schema_retries = outcome.schema_attempts

        # Brief section 3.3.1: validate, retry once, then fall back.
        if outcome.error:
            trace.llm_calls = self.client.llm_calls
            return guardrail.fallback(request, self.registry.all(), trace, outcome.error)

        try:
            validated = guardrail.validate_draft(outcome.draft)
        except guardrail.GuardrailRejected as exc:
            trace.schema_retries = 1
            trace.llm_calls = self.client.llm_calls
            return guardrail.fallback(request, self.registry.all(), trace, str(exc))

        return guardrail.apply(validated, self.registry.all(), request, trace)

    # -- tool loop --------------------------------------------------------- #
    def _gather_and_decide(
        self, request: PurchaseRequest, trace: Trace
    ) -> tuple[dict[str, Any] | None, AgentOutcome]:
        request_block = prompts.render_request(request)
        messages = prompts.agent_a_initial(request_block)
        transcript: list[dict[str, Any]] = []
        schema_attempts = 0
        tools = prompts.all_tool_schemas() + [prompts.DECISION_TOOL]

        for round_index in range(self.max_rounds + 1):
            response = self.client.chat(messages, tools=tools)

            # A submit_decision call is the final answer, not a tool request.
            submitted = self._submitted_decision(response)
            if submitted is not None:
                return submitted, AgentOutcome(submitted, None, schema_attempts)

            if response.tool_calls:
                transcript.extend(self._execute_tool_calls(response, trace))
                messages = prompts.agent_a_after_tools(request_block, transcript)
                continue

            # No tool call: the model is trying to answer.
            draft = self._coerce_decision(response)
            if draft is not None:
                return draft, AgentOutcome(draft, None, schema_attempts)

            # Unparseable: one repair retry (brief section 4).
            schema_attempts += 1
            if schema_attempts > 1:
                return None, AgentOutcome(
                    None,
                    "model did not return a valid decision object after one repair retry",
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

        # Round cap reached without a usable answer.
        messages = list(messages) + prompts.agent_a_final_reminder()
        response = self.client.chat(messages, tools=None)
        draft = self._coerce_decision(response)
        if draft is not None:
            return draft, AgentOutcome(draft, None, schema_attempts)
        return None, AgentOutcome(
            None,
            f"tool round cap ({self.max_rounds}) reached without a valid decision",
            schema_attempts,
        )

    # -- helpers ----------------------------------------------------------- #
    def _execute_tool_calls(
        self, response: Any, trace: Trace
    ) -> list[dict[str, Any]]:
        """Run each requested tool. Unknown names are rejected, never executed."""
        transcript: list[dict[str, Any]] = []
        results: list[ToolResult] = []

        for call in response.tool_calls:
            if call.name == "submit_decision":
                # Not a tool request; handled by the caller.
                continue
            if call.name not in self.registry.names:
                trace.guardrail_notes.append(
                    f"rejected tool call {call.name!r}: not available; this agent may only "
                    f"call {self.registry.names}"
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
            result = self.registry.call(call.name, _sanitise_args(call.arguments))
            results.append(result)

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

    @staticmethod
    def _submitted_decision(response: Any) -> dict[str, Any] | None:
        """The decision tool call, if this response is the final answer."""
        for call in response.tool_calls:
            if call.name == "submit_decision" and call.arguments:
                return call.arguments
        return None

    @staticmethod
    def _coerce_decision(response: Any) -> dict[str, Any] | None:
        from core.llm_client import extract_json

        submitted = AgentA._submitted_decision(response)
        if submitted is not None:
            return submitted
        if response.parsed and "recommendation" in response.parsed:
            return response.parsed
        try:
            parsed = extract_json(response.text)
        except LLMError:
            return None
        return parsed if isinstance(parsed, dict) and "recommendation" in parsed else None


def _sanitise_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Only structured scalar arguments reach a tool.

    Anything that is not a string/number/bool/list-of-strings is dropped, so a
    model cannot smuggle a structure into an argument.
    """
    clean: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if not isinstance(key, str):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, list):
            clean[key] = [v for v in value if isinstance(v, (str, int, float))]
    return clean


__all__ = ["MAX_TOOL_ROUNDS", "AgentA"]

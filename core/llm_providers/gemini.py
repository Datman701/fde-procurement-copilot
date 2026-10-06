"""Gemini adapter using Google's official ``google-genai`` SDK.

The application keeps a provider-neutral chat interface. This adapter maps it
to Google GenAI models.generate_content, including native function-call output.
"""

from __future__ import annotations

import json
from typing import Any

from core.llm_client import LLMConfig, LLMError, LLMResponse, ToolCall, _RetryableError


class GeminiProvider:
    """Synchronous official-SDK transport for Google GenAI models."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise LLMError("Gemini requires google-genai; install requirements.txt") from exc
        self._client = genai.Client(api_key=config.api_key)

    def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_schema: dict[str, Any] | None,
        temperature: float,
    ) -> LLMResponse:
        return self.complete(messages, tools, response_schema, temperature)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_schema: dict[str, Any] | None,
        temperature: float,
    ) -> LLMResponse:
        from google.genai import types

        system_instruction, input_text = _render_messages(messages)

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if tools:
            decls = [_as_gemini_declaration(tool) for tool in tools]
            config_kwargs["tools"] = [types.Tool(function_declarations=decls)]
        elif response_schema:
            config_kwargs["response_mime_type"] = "application/json"

        call_config = types.GenerateContentConfig(**config_kwargs)

        try:
            response = self._client.models.generate_content(
                model=self.config.model,
                contents=input_text,
                config=call_config,
            )
        except Exception as exc:
            self._raise_as_llm_error(exc)
            raise AssertionError("unreachable")  # pragma: no cover
        return _parse_response(response)

    @staticmethod
    def _raise_as_llm_error(exc: Exception) -> None:
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        detail = str(exc)
        if status in {408, 409, 425, 429, 500, 502, 503, 504}:
            raise _RetryableError(f"Gemini API {status}: {detail[:500]}") from exc
        if isinstance(exc, (TimeoutError, ConnectionError)):
            raise _RetryableError(f"Gemini transport error: {detail[:500]}") from exc
        raise LLMError(f"Gemini SDK request failed: {detail[:500]}") from exc


def _render_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Serialise the provider-neutral transcript for a stateless model turn."""
    systems: list[str] = []
    turns: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content")
        if role == "system":
            if content:
                systems.append(str(content))
        elif role == "tool":
            turns.append(
                "[TOOL RESULT " + str(message.get("name", "unknown")) + "]\n" + str(content or "")
            )
        elif message.get("tool_calls"):
            turns.append("[MODEL TOOL CALLS]\n" + json.dumps(message["tool_calls"], default=str))
        elif content is not None:
            label = "MODEL" if role == "assistant" else "USER"
            turns.append(f"[{label}]\n{content}")
    return "\n\n".join(systems), "\n\n".join(turns)


def _clean_schema(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _clean_schema(v)
            for k, v in obj.items()
            if k not in ("additionalProperties", "additional_properties")
        }
    if isinstance(obj, list):
        return [_clean_schema(item) for item in obj]
    return obj


def _as_gemini_declaration(tool: dict[str, Any]) -> Any:
    from google.genai import types

    function = tool.get("function", tool)
    params = _clean_schema(function.get("parameters"))
    return types.FunctionDeclaration(
        name=function["name"],
        description=function.get("description", ""),
        parameters=params,
    )


def _parse_response(response: Any) -> LLMResponse:
    text = (getattr(response, "text", None) or "").strip()
    tool_calls: list[ToolCall] = []

    for call in getattr(response, "function_calls", None) or []:
        name = getattr(call, "name", None)
        if name:
            args = getattr(call, "args", {}) or {}
            tool_calls.append(
                ToolCall(
                    name=str(name),
                    arguments=dict(args),
                )
            )

    parsed: dict[str, Any] | None = None
    if text and not tool_calls:
        from core.llm_client import extract_json

        try:
            parsed = extract_json(text)
        except LLMError:
            pass
    if not text and not tool_calls:
        raise LLMError(f"Gemini response returned no text or function call: {response!r}")
    raw = {"text": text, "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in tool_calls]}
    return LLMResponse(text=text, tool_calls=tool_calls, parsed=parsed, raw=raw)


def make_provider(config: LLMConfig) -> GeminiProvider:
    return GeminiProvider(config)


__all__ = ["GeminiProvider", "make_provider"]

"""OpenAI-compatible chat adapter.

One file, no provider SDK: it speaks the OpenAI ``/chat/completions`` wire
format over ``requests``. That covers OpenAI itself, Gemini's OpenAI-compatible
endpoint, OpenRouter, Groq, Together, Ollama and most local gateways.

To add a provider that is *not* OpenAI-compatible, copy this file, change
:meth:`OpenAICompatProvider.complete`, and register it in ``core/llm_providers/
__init__.py``. Nothing else in the codebase changes.
"""

from __future__ import annotations

import json
import time
from typing import Any

from core.llm_client import (
    LLMConfig,
    LLMError,
    LLMResponse,
    ToolCall,
    _RetryableError,
    normalise_tool_arguments,
)

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class OpenAICompatProvider:
    """Minimal, dependency-light OpenAI-compatible chat provider."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    # -- public ------------------------------------------------------------ #
    __call__ = complete = None  # placeholder for typing

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_schema: dict[str, Any] | None,
        temperature: float,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        if response_schema:
            # Best effort: providers that do not support this either ignore it
            # or return a 400, which we degrade to a JSON-in-text request.
            payload["response_format"] = {
                "type": "json_object",
                "schema": response_schema,
            }
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort

        data = self._post(payload)
        return self._parse(data)

    # -- transport --------------------------------------------------------- #
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            **self.config.extra_headers,
        }
        try:
            response = requests.post(
                url, headers=headers, data=json.dumps(payload), timeout=self.config.timeout
            )
        except requests.Timeout as exc:
            raise _RetryableError(f"request timed out after {self.config.timeout}s: {exc}") from exc
        except requests.ConnectionError as exc:
            raise _RetryableError(f"connection error: {exc}") from exc
        except requests.RequestException as exc:
            raise LLMError(f"request failed: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            retry_after = response.headers.get("Retry-After")
            hint = f" (Retry-After: {retry_after})" if retry_after else ""
            raise _RetryableError(
                f"HTTP {response.status_code}{hint}: {response.text[:300]}"
            )
        if not response.ok:
            body = response.text[:400]
            # Some endpoints reject `response_format`; retry once without it.
            if response.status_code == 400 and "response_format" in body:
                payload = dict(payload)
                payload.pop("response_format", None)
                return self._post(payload)
            raise LLMError(f"HTTP {response.status_code}: {body}")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise LLMError(f"non-JSON response: {exc}") from exc

    # -- parsing ----------------------------------------------------------- #
    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"response contained no choices: {json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=normalise_tool_arguments(function.get("arguments")),
                )
            )

        parsed: dict[str, Any] | None = None
        if not tool_calls:
            from core.llm_client import extract_json

            try:
                parsed = extract_json(text)
            except LLMError:
                parsed = None

        return LLMResponse(text=text, tool_calls=tool_calls, parsed=parsed, raw=data)


def make_provider(config: LLMConfig) -> OpenAICompatProvider:
    return OpenAICompatProvider(config)


__all__ = ["OpenAICompatProvider", "make_provider"]

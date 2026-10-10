"""Provider-agnostic LLM client.

Design constraints from the brief:

* **No provider is hardcoded.** ``LLM_PROVIDER``, ``LLM_MODEL``, ``LLM_API_KEY``
  and optional ``LLM_BASE_URL`` come from the environment.
* **One interface**: :meth:`LLMClient.chat(messages, tools=None,
  response_schema=None) -> LLMResponse`. Adding a provider is one file in
  ``core/llm_providers/`` plus one entry in :data:`PROVIDERS`.
* **Rate-limit safety**: exponential backoff with jitter on 429/5xx, a
  configurable requests-per-minute throttle, and a hard per-run call budget.
* **Disk cache** keyed by a hash of (provider, model, messages, tools, schema,
  temperature). Cached responses still count toward the reported call count, so
  the numbers in the README mean the same thing with or without a cache.
* **Never crash**: after retries are exhausted, :attr:`LLMClient.unavailable`
  is set and the agent stage returns a safe escalate-to-human fallback.

Implemented for the free Gemini tier, which is reachable through its
OpenAI-compatible endpoint, so the OpenAI-compatible adapter covers Gemini,
OpenRouter, Groq and any local OpenAI-compatible server.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_CACHE_PATH = "eval/cache/llm_cache.json"


class LLMError(RuntimeError):
    """Raised when a call cannot be completed and no fallback is acceptable."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    parsed: dict[str, Any] | None = None
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


# --------------------------------------------------------------------------- #
# JSON-in-text extraction (fallback for weak structured-output support)
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict[str, Any]:
    """Parse the first complete JSON object in ``text``.

    Deliberately strict: no eval, no partial recovery. Used as the fallback when
    a provider cannot do native tool calling or response schemas.
    """
    if not text or not text.strip():
        raise LLMError("empty model output")

    # Strip markdown fences.
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]
        stripped = stripped.strip()
        for prefix in ("json", "JSON"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        # A JSON *array* is not a decision; reject it rather than handing the
        # caller something that is not an object.
        if isinstance(parsed, dict):
            return parsed
        raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")

    # Fall back to the first balanced {...} span, ignoring braces inside strings.
    start = stripped.find("{")
    if start == -1:
        raise LLMError(f"no JSON object found in output: {text[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"malformed JSON in output: {exc}") from exc
    raise LLMError(f"unbalanced JSON object in output: {text[:200]!r}")


def normalise_tool_arguments(raw: Any) -> dict[str, Any]:
    """Providers return tool arguments as a JSON string; parse them."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"tool arguments are not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"tool arguments must be an object, got {type(parsed).__name__}")
        return parsed
    return {}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    temperature: float = 0.0
    timeout: float = 60.0
    max_retries: int = 4
    requests_per_minute: float = 10.0
    max_calls_per_run: int = 12
    reasoning_effort: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        from core.llm_providers import resolve

        return resolve(os.environ)

    def fingerprint(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
        }


# --------------------------------------------------------------------------- #
# Disk cache
# --------------------------------------------------------------------------- #
class DiskCache:
    """JSON-file LLM cache. Committed to the repo so results reproduce.

    Keyed by a sha256 of (provider, model, base_url, temperature, messages,
    tools, response_schema). Entries are immutable: a cached response is
    returned verbatim.
    """

    def __init__(
        self,
        path: str | None = None,
        enabled: bool = True,
        *,
        bypass_read: bool = False,
        read_only: bool = False,
    ) -> None:
        self.path = path or os.getenv("LLM_CACHE_PATH", DEFAULT_CACHE_PATH)
        self.enabled = enabled
        #: Skip lookups but still write: re-run against the live API while
        #: keeping the committed cache up to date.
        self.bypass_read = bypass_read
        #: Load and serve the cache but never modify it (CI / read-only runs).
        self.read_only = read_only
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self.writes = 0
        if self.enabled and os.path.isfile(self.path):
            try:
                self._data = json.loads(open(self.path, encoding="utf-8").read())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    @staticmethod
    def make_key(fingerprint: dict[str, Any], payload: dict[str, Any]) -> str:
        blob = json.dumps(
            {"fingerprint": fingerprint, "payload": payload},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        if self.bypass_read:
            with self._lock:
                self.misses += 1  # deliberately re-fetched from the provider
            return None
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return entry

    def set(self, key: str, entry: dict[str, Any]) -> None:
        if not self.enabled or self.read_only:
            return
        with self._lock:
            self._data[key] = entry
            self.writes += 1
        self.flush()

    def flush(self) -> None:
        if not self.enabled or self.read_only:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            # default=str: a provider response that carries a bytes value must
            # never abort the run at cache-write time.
            json.dump(self._data, handle, indent=1, sort_keys=True, default=str)
        os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._data)


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
class Throttle:
    """Minimum interval between calls + a per-run hard budget."""

    def __init__(self, requests_per_minute: float, max_calls_per_run: int) -> None:
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self.max_calls = max_calls_per_run
        self.calls = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def check_budget(self) -> None:
        if self.calls >= self.max_calls:
            raise LLMError(
                f"per-run LLM call budget exhausted ({self.max_calls} calls); "
                "stopping before the next request"
            )

    def wait(self) -> float:
        slept = 0.0
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
                slept = gap
                now = time.monotonic()
            self._last = now
        return slept


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LLMClient:
    """Thin, synchronous, provider-agnostic chat client.

    Owns all counting, timing, caching, throttling and retry so callers only
    deal with messages and tool schemas.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        cache: DiskCache | None = None,
        use_cache: bool = True,
        cache_writes: bool | None = None,
        transport: str | None = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        if cache is not None:
            self.cache = cache
        else:
            writes = use_cache if cache_writes is None else cache_writes
            self.cache = DiskCache(
                enabled=use_cache or writes,
                bypass_read=not use_cache,
                read_only=not writes,
            )
        self.throttle = Throttle(
            self.config.requests_per_minute, self.config.max_calls_per_run
        )
        self.transport = transport or self._default_transport()
        self.provider_impl = self._load_provider()

        # Per-run counters (reset by ``new_run``).
        self.llm_calls = 0
        self.cached_calls = 0
        self.retries = 0
        self.call_log: list[dict[str, Any]] = []
        self.unavailable = False
        self.unavailable_reason: str | None = None
        self.error_log: list[str] = []

    # -- lifecycle --------------------------------------------------------- #
    def new_run(self) -> None:
        """Reset per-run counters. The cache is deliberately *not* cleared."""
        self.llm_calls = 0
        self.cached_calls = 0
        self.retries = 0
        self.unavailable = False
        self.unavailable_reason = None
        self.error_log = []
        self.throttle.calls = 0

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key and self.config.model)

    # -- provider ---------------------------------------------------------- #
    def _default_transport(self) -> str:
        from core.llm_providers import PROVIDERS

        return PROVIDERS.get(self.config.provider, {}).get("transport", "openai_compat")

    def _load_provider(self):
        from core.llm_providers import PROVIDERS

        spec = PROVIDERS.get(self.config.provider)
        if spec is None:
            raise LLMError(
                f"unknown LLM_PROVIDER {self.config.provider!r}; "
                f"known: {sorted(PROVIDERS)}"
            )
        import importlib

        module = importlib.import_module(spec["module"])
        return getattr(module, spec["function"])(self.config)

    # -- main entry point -------------------------------------------------- #
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """One chat completion. Counts, times, caches and retries."""
        if not self.configured:
            self.unavailable = True
            self.unavailable_reason = "LLM_API_KEY / LLM_MODEL not configured"
            raise LLMError(self.unavailable_reason)

        payload = {
            "messages": messages,
            "tools": tools,
            "response_schema": response_schema,
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
        }
        key = DiskCache.make_key(self.config.fingerprint(), payload)

        # A cached response still counts as a call: the reported numbers must
        # mean the same thing whether or not the cache was warm.
        self.throttle.check_budget()
        self.throttle.calls += 1
        self.llm_calls += 1
        self.call_log.append(
            {
                "cached": True,
                "tool_calls_requested": [t.get("name") for t in tools or []],
            }
        )

        entry = self.cache.get(key)
        if entry is not None:
            self.cached_calls += 1
            if self.call_log:
                self.call_log[-1]["cached"] = True
                self.call_log[-1]["latency_ms"] = entry.get("latency_ms")
            return LLMResponse(
                text=entry.get("text", ""),
                tool_calls=[
                    ToolCall(name=tc["name"], arguments=tc.get("arguments", {}))
                    for tc in entry.get("tool_calls", [])
                ],
                parsed=entry.get("parsed"),
                cached=True,
                raw=entry.get("raw", {}),
            )

        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                self.throttle.wait()
                result = self.provider_impl(
                    messages=messages,
                    tools=tools,
                    response_schema=response_schema,
                    temperature=payload["temperature"],
                )
            except _RetryableError as exc:
                last_error = exc
                self.retries += 1
                if attempt >= self.config.max_retries:
                    break
                time.sleep(backoff_delay(attempt))
                continue
            except LLMError:
                raise
            except Exception as exc:  # unexpected: do not retry blindly forever
                last_error = exc
                break

            elapsed_ms = (time.perf_counter() - started) * 1000
            if self.call_log:
                self.call_log[-1]["latency_ms"] = round(elapsed_ms, 1)
                self.call_log[-1]["text_length"] = len(result.text)
                self.call_log[-1]["tool_calls_returned"] = [tc.name for tc in result.tool_calls]
            self.cache.set(
                key,
                {
                    "text": result.text,
                    "tool_calls": [
                        {"name": tc.name, "arguments": tc.arguments} for tc in result.tool_calls
                    ],
                    "parsed": result.parsed,
                    "raw": result.raw,
                    "latency_ms": round(elapsed_ms, 1),
                    "model": self.config.model,
                },
            )
            return result

        self.unavailable = True
        self.unavailable_reason = f"{type(last_error).__name__}: {last_error}"
        self.error_log.append(self.unavailable_reason)
        raise LLMError(self.unavailable_reason)

    # -- structured output with one repair retry --------------------------- #
    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        tool: dict[str, Any] | None = None,
        schema_hint: dict[str, Any] | None = None,
        max_repairs: int = 1,
    ) -> tuple[dict[str, Any], LLMResponse, int]:
        """Get one JSON object out of the model.

        Strategy, in order:
        1. native structured output when the provider supports it,
        2. native tool calling (the JSON comes back as a single tool argument),
        3. JSON-in-text with strict parsing, then **one** repair retry.

        Returns ``(json, last_response, repair_count)``.
        """
        repairs = 0
        response: LLMResponse | None = None
        conversation = list(messages)

        for attempt in range(max_repairs + 1):
            response = self.chat(
                conversation,
                tools=[tool] if tool else None,
                response_schema=schema_hint,
            )
            parsed = self._extract_json(response)
            if parsed is not None:
                return parsed, response, repairs
            if attempt >= max_repairs:
                break
            repairs += 1
            conversation = conversation + [
                {"role": "assistant", "content": response.text[:2000]},
                {
                    "role": "user",
                    "content": (
                        "That was not valid JSON in the required shape. Reply with a single "
                        "JSON object and nothing else - no prose, no markdown fence. "
                        f"Required keys: {sorted((schema_hint or {}).keys())}"
                    ),
                },
            ]

        raise LLMError(f"model did not return usable JSON after {repairs} repair(s)")

    @staticmethod
    def _extract_json(response: LLMResponse) -> dict[str, Any] | None:
        if response.parsed is not None:
            return response.parsed
        for call in response.tool_calls:
            if call.arguments:
                return call.arguments
        try:
            return extract_json(response.text)
        except LLMError:
            return None

    def stats(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "cached_calls": self.cached_calls,
            "retries": self.retries,
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
            "unavailable": self.unavailable,
            "unavailable_reason": self.unavailable_reason,
            "provider": self.config.provider,
            "model": self.config.model,
        }


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
class _RetryableError(LLMError):
    """A failure worth retrying: 429, 5xx, or a transport error."""


def backoff_delay(attempt: int, base: float = 1.5, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter (brief section 4)."""
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)


def iter_json_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield line

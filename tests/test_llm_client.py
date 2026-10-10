"""Unit tests for the LLM client, cache, throttle and backoff. No network."""

from __future__ import annotations

import json
import os

import pytest

from core.llm_client import (
    DiskCache,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMResponse,
    Throttle,
    ToolCall,
    _RetryableError,
    backoff_delay,
    extract_json,
    normalise_tool_arguments,
)
from core.llm_providers import PROVIDERS, resolve


class FakeProvider:
    """Stands in for a provider adapter; records what it was asked."""

    def __init__(self, responses, fail_times=0):
        self.responses = list(responses)
        self.fail_times = fail_times
        self.calls = []

    def __call__(self, messages, tools, response_schema, temperature):
        self.calls.append(
            {"messages": messages, "tools": tools, "schema": response_schema, "temperature": temperature}
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _RetryableError("simulated 429")
        # Key responses by the prompt content so a cache-warmed run replays the
        # right response instead of consuming the list in the wrong order.
        marker = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                marker = str(message.get("content", ""))
                break
        for index, response in enumerate(self.responses):
            if getattr(response, "_marker", None) == marker:
                self.responses.pop(index)
                return response
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="{}")

    @staticmethod
    def tagged(marker: str, response: LLMResponse) -> LLMResponse:
        response._marker = marker
        return response


def make_client(tmp_path, responses=None, fail_times=0, **overrides):
    config = LLMConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        requests_per_minute=100_000,
        max_calls_per_run=overrides.pop("max_calls_per_run", 12),
        max_retries=overrides.pop("max_retries", 2),
        **overrides,
    )
    cache = DiskCache(path=str(tmp_path / "cache.json"), enabled=True)
    client = LLMClient(config, cache=cache, use_cache=True, transport="fake")
    client.provider_impl = FakeProvider(responses or [LLMResponse(text="{}")], fail_times)
    return client


def make_config() -> LLMConfig:
    """Config for tests that want the client to build its own cache."""
    return LLMConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        requests_per_minute=100_000,
        max_calls_per_run=12,
        max_retries=1,
    )


# --------------------------------------------------------------------------- #
# Provider resolution
# --------------------------------------------------------------------------- #
class TestProviderResolution:
    def test_no_provider_is_hardcoded(self):
        """The registry, not client logic, selects each provider adapter."""
        assert PROVIDERS["gemini"]["module"] == "core.llm_providers.gemini"
        for name, spec in PROVIDERS.items():
            assert spec["function"] == "make_provider", name
            assert spec["base_url"].startswith("http")

    def test_resolve_from_environment(self):
        config = resolve(
            {
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-2.5-flash",
                "LLM_API_KEY": "abc123",
            }
        )
        assert config.provider == "gemini"
        assert config.model == "gemini-2.5-flash"
        assert config.api_key == "abc123"
        assert "generativelanguage.googleapis.com" in config.base_url
        assert config.temperature == 0.0

    def test_provider_specific_env_key_fallback(self):
        config = resolve({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk_test"})
        assert config.api_key == "gsk_test"
        assert "api.groq.com" in config.base_url

    def test_explicit_base_url_wins(self):
        config = resolve(
            {"LLM_PROVIDER": "gemini", "LLM_BASE_URL": "http://localhost:1234/v1"}
        )
        assert config.base_url == "http://localhost:1234/v1"

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            resolve({"LLM_PROVIDER": "not-a-provider"})

    def test_rate_limit_settings_from_env(self):
        config = resolve(
            {
                "LLM_PROVIDER": "gemini",
                "LLM_REQUESTS_PER_MINUTE": "4",
                "LLM_MAX_CALLS_PER_RUN": "3",
                "LLM_MAX_RETRIES": "1",
            }
        )
        assert config.requests_per_minute == 4
        assert config.max_calls_per_run == 3
        assert config.max_retries == 1

    def test_default_model_per_provider(self):
        assert resolve({"LLM_PROVIDER": "gemini"}).model == "gemini-2.5-flash"


# --------------------------------------------------------------------------- #
# JSON parsing (the free-model fallback path)
# --------------------------------------------------------------------------- #
class TestJSONExtraction:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('```\n{"a": 2}\n```') == {"a": 2}

    def test_json_embedded_in_prose(self):
        text = 'Here is my analysis:\n{"recommendation": "proceed_with_approvals"}\nDone.'
        assert extract_json(text)["recommendation"] == "proceed_with_approvals"

    def test_braces_inside_strings_do_not_break_parsing(self):
        text = '{"next_step": "use the tool {budget_check} first"}'
        assert extract_json(text)["next_step"] == "use the tool {budget_check} first"

    def test_escaped_quotes(self):
        text = '{"next_step": "say \\"approved\\" then stop"}'
        assert extract_json(text)["next_step"] == 'say "approved" then stop'

    @pytest.mark.parametrize("bad", ["", "   ", "no json here", "{unbalanced", "[1,2,3]"])
    def test_invalid_output_raises(self, bad):
        with pytest.raises(LLMError):
            extract_json(bad)

    def test_tool_arguments_string_is_parsed(self):
        assert normalise_tool_arguments('{"department": "Finance"}') == {"department": "Finance"}

    def test_tool_arguments_dict_passthrough(self):
        assert normalise_tool_arguments({"a": 1}) == {"a": 1}

    def test_tool_arguments_empty(self):
        assert normalise_tool_arguments("") == {}
        assert normalise_tool_arguments(None) == {}

    def test_tool_arguments_non_object_rejected(self):
        with pytest.raises(LLMError):
            normalise_tool_arguments("[1,2]")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
class TestDiskCache:
    def test_key_is_stable_for_identical_input(self):
        payload = {"messages": [{"role": "user", "content": "hi"}], "tools": None}
        fingerprint = {"provider": "gemini", "model": "m"}
        assert DiskCache.make_key(fingerprint, payload) == DiskCache.make_key(fingerprint, payload)

    def test_key_changes_with_model(self):
        payload = {"messages": []}
        a = DiskCache.make_key({"model": "a"}, payload)
        b = DiskCache.make_key({"model": "b"}, payload)
        assert a != b

    def test_key_changes_with_messages(self):
        fingerprint = {"model": "m"}
        a = DiskCache.make_key(fingerprint, {"messages": [{"content": "x"}]})
        b = DiskCache.make_key(fingerprint, {"messages": [{"content": "y"}]})
        assert a != b

    def test_round_trip(self, tmp_path):
        cache = DiskCache(path=str(tmp_path / "c.json"))
        key = DiskCache.make_key({}, {"a": 1})
        cache.set(key, {"text": "hello"})
        cache.flush()
        reloaded = DiskCache(path=str(tmp_path / "c.json"))
        assert reloaded.get(key)["text"] == "hello"

    def test_disabled_cache_never_stores(self, tmp_path):
        cache = DiskCache(path=str(tmp_path / "c.json"), enabled=False)
        key = DiskCache.make_key({}, {"a": 1})
        cache.set(key, {"text": "x"})
        cache.flush()
        assert cache.get(key) is None
        assert not (tmp_path / "c.json").exists()

    def test_cached_response_still_counts_as_a_call(self, tmp_path):
        first = make_client(tmp_path, [LLMResponse(text='{"a":1}')])
        first.chat([{"role": "user", "content": "hello"}])
        assert first.llm_calls == 1
        assert first.cached_calls == 0

        second = make_client(tmp_path, [LLMResponse(text='{"a":1}')])
        second.cache._data = first.cache._data  # warm cache
        response = second.chat([{"role": "user", "content": "hello"}])
        assert response.cached is True
        assert second.cached_calls == 1
        # The headline number is identical with or without a warm cache.
        assert second.llm_calls == 1

    def test_no_cache_flag_bypasses_the_store(self, tmp_path):
        client = make_client(tmp_path, [LLMResponse(text="{}")])
        client.cache.enabled = False
        client.chat([{"role": "user", "content": "x"}])
        client.chat([{"role": "user", "content": "x"}])
        assert client.cached_calls == 0
        assert client.llm_calls == 2


    def test_bypass_read_ignores_an_existing_entry_but_still_writes(self, tmp_path):
        path = str(tmp_path / "c.json")
        warm = DiskCache(path=path)
        key = DiskCache.make_key({}, {"a": 1})
        warm.set(key, {"text": "warm"})

        fresh = DiskCache(path=path, bypass_read=True)
        assert fresh.get(key) is None  # forced back to the provider
        fresh.set(key, {"text": "refetched"})
        fresh.flush()

        reloaded = DiskCache(path=path)
        assert reloaded.get(key)["text"] == "refetched"
        # The fresh run merged with what was already there rather than wiping it.
        assert len(reloaded._data) == 1

    def test_read_only_serves_the_cache_but_never_writes(self, tmp_path):
        path = str(tmp_path / "c.json")
        warm = DiskCache(path=path)
        key = DiskCache.make_key({}, {"a": 1})
        warm.set(key, {"text": "warm"})
        warm.flush()

        frozen = DiskCache(path=path, read_only=True)
        assert frozen.get(key)["text"] == "warm"
        frozen.set(key, {"text": "changed"})
        frozen.flush()

        assert DiskCache(path=path).get(key)["text"] == "warm"
        assert frozen.writes == 0

    def test_client_default_cache_flags_match_use_cache(self, tmp_path):
        path = str(tmp_path / "default.json")
        os.environ["LLM_CACHE_PATH"] = path
        try:
            client = LLMClient(make_config(), transport="fake")
            assert client.cache.enabled and not client.cache.bypass_read
            assert not client.cache.read_only

            frozen = LLMClient(
                make_config(), use_cache=True, cache_writes=False, transport="fake"
            )
            assert frozen.cache.read_only

            off = LLMClient(make_config(), use_cache=False, cache_writes=False, transport="fake")
            assert not off.cache.enabled
        finally:
            os.environ.pop("LLM_CACHE_PATH", None)

    def test_fresh_run_rerequests_and_updates_the_committed_cache(self, tmp_path):
        """use_cache=False + cache_writes=True: live calls that refresh the cache."""
        path = str(tmp_path / "fresh.json")
        os.environ["LLM_CACHE_PATH"] = path
        try:
            prompt = [{"role": "user", "content": "same prompt"}]

            seed = LLMClient(make_config(), transport="fake")
            seed.provider_impl = FakeProvider([LLMResponse(text='{"v":1}')])
            seed.chat(prompt)
            assert len(seed.cache._data) == 1

            fresh = LLMClient(
                make_config(), use_cache=False, cache_writes=True, transport="fake"
            )
            fresh.provider_impl = FakeProvider([LLMResponse(text='{"v":2}')])
            response = fresh.chat(prompt)
            assert response.cached is False  # cache was deliberately bypassed
            assert fresh.cached_calls == 0
            assert fresh.llm_calls == 1
            # Old entry kept, new response written alongside it.
            assert len(fresh.cache._data) >= 1

            replay = LLMClient(make_config(), transport="fake")
            replay.provider_impl = FakeProvider([LLMResponse(text="{}")])
            replay.chat(prompt)
            assert replay.cached_calls == 1  # a later run reads the refreshed entry
        finally:
            os.environ.pop("LLM_CACHE_PATH", None)


# --------------------------------------------------------------------------- #
# Counting, budget, backoff
# --------------------------------------------------------------------------- #
class TestCountingAndBudget:
    def test_every_call_is_counted(self, tmp_path):
        client = make_client(tmp_path, [LLMResponse(text="{}")] * 5)
        for _ in range(5):
            client.chat([{"role": "user", "content": str(_)}])
        assert client.llm_calls == 5

    def test_hard_per_run_budget_is_enforced(self, tmp_path):
        client = make_client(tmp_path, [LLMResponse(text="{}")] * 10, max_calls_per_run=3)
        for _ in range(3):
            client.chat([{"role": "user", "content": str(_)}])
        with pytest.raises(LLMError, match="budget exhausted"):
            client.chat([{"role": "user", "content": "over"}])
        assert client.llm_calls == 3

    def test_new_run_resets_counters(self, tmp_path):
        client = make_client(tmp_path, [LLMResponse(text="{}")] * 4)
        client.chat([{"role": "user", "content": "a"}])
        client.new_run()
        assert client.llm_calls == 0
        client.chat([{"role": "user", "content": "b"}])
        assert client.llm_calls == 1

    def test_backoff_is_exponential_with_jitter(self):
        delays = [backoff_delay(attempt) for attempt in range(5) for _ in range(40)]
        assert all(d >= 0 for d in delays)
        # Attempt 3's ceiling (12s) must be larger than attempt 0's (1.5s).
        assert max(d for d in delays[:40]) <= 1.5
        assert max(delays[120:160]) > 3

    def test_backoff_is_capped(self):
        assert backoff_delay(20, base=1.5, cap=30.0) <= 30.0

    def test_retries_on_429_then_succeeds(self, tmp_path):
        client = make_client(
            tmp_path, [LLMResponse(text='{"ok":true}')], fail_times=2, max_retries=3
        )
        client.config.max_retries = 3
        response = client.chat([{"role": "user", "content": "x"}])
        assert response.text == '{"ok":true}'
        assert client.retries == 2
        assert client.llm_calls == 1  # one logical call, two retries

    def test_exhausted_retries_marks_unavailable(self, tmp_path):
        client = make_client(tmp_path, [], fail_times=99, max_retries=1)
        with pytest.raises(LLMError):
            client.chat([{"role": "user", "content": "x"}])
        assert client.unavailable is True
        assert "simulated 429" in (client.unavailable_reason or "")

    def test_throttle_waits_between_calls(self):
        throttle = Throttle(requests_per_minute=600, max_calls_per_run=10)  # 0.1s interval
        throttle.wait()
        import time

        start = time.monotonic()
        throttle.wait()
        assert time.monotonic() - start >= 0.05


# --------------------------------------------------------------------------- #
# chat_json: structured output + one repair retry
# --------------------------------------------------------------------------- #
class TestChatJson:
    def test_parses_plain_json_response(self, tmp_path):
        client = make_client(tmp_path, [LLMResponse(text='{"recommendation": "reject"}')])
        payload, _, repairs = client.chat_json([{"role": "user", "content": "x"}])
        assert payload["recommendation"] == "reject"
        assert repairs == 0

    def test_parses_json_from_a_tool_call(self, tmp_path):
        response = LLMResponse(text="", tool_calls=[ToolCall("emit", {"recommendation": "reject"})])
        client = make_client(tmp_path, [response])
        payload, _, _ = client.chat_json([{"role": "user", "content": "x"}])
        assert payload == {"recommendation": "reject"}

    def test_one_repair_retry_then_success(self, tmp_path):
        client = make_client(
            tmp_path,
            [LLMResponse(text="sorry, here is prose"), LLMResponse(text='{"a": 1}')],
        )
        payload, _, repairs = client.chat_json(
            [{"role": "user", "content": "x"}], schema_hint={"a": "int"}
        )
        assert payload == {"a": 1}
        assert repairs == 1
        assert len(client.provider_impl.calls) == 2
        # The repair message must ask for JSON only.
        repair = client.provider_impl.calls[1]["messages"][-1]["content"]
        assert "JSON" in repair

    def test_raises_after_one_repair(self, tmp_path):
        client = make_client(
            tmp_path, [LLMResponse(text="nope"), LLMResponse(text="still nope")]
        )
        with pytest.raises(LLMError, match="usable JSON"):
            client.chat_json([{"role": "user", "content": "x"}])

    def test_unconfigured_client_raises_and_flags(self, tmp_path):
        client = make_client(tmp_path)
        client.config.api_key = ""
        with pytest.raises(LLMError, match="not configured"):
            client.chat([{"role": "user", "content": "x"}])
        assert client.unavailable is True

    def test_stats_reports_provider_and_model(self, tmp_path):
        client = make_client(tmp_path)
        stats = client.stats()
        assert stats["provider"] == "gemini"
        assert stats["model"] == "gemini-2.5-flash"
        assert "cache_hits" in stats

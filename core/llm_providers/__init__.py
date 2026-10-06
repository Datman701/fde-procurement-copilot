"""Provider registry.

``LLM_PROVIDER`` selects one entry. Base URLs are defaults only - ``LLM_BASE_URL``
overrides anything, so a new OpenAI-compatible endpoint needs no code at all.

To add a provider:

1. create ``core/llm_providers/<name>.py`` exposing ``make_provider(config)``,
2. add an entry to :data:`PROVIDERS` below.

Nothing outside this package changes.
"""

from __future__ import annotations

import os
from typing import Any

from core.llm_client import LLMConfig

PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GEMINI_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
        "note": "Google AI Studio free tier; reached through its OpenAI-compatible endpoint.",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "env_key": "OLLAMA_API_KEY",
        "transport": "openai_compat",
        "module": "core.llm_providers.openai_compat",
        "function": "make_provider",
        "note": "Local models; any OpenAI-compatible key value works.",
    },
}

#: OpenAI requires ``max_completion_tokens``; others use ``max_tokens``.
DEFAULT_MODEL_BY_PROVIDER = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "groq": "llama-3.3-70b-versatile",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "ollama": "llama3.2",
}


def resolve(env: dict[str, str] | None = None) -> LLMConfig:
    env = env if env is not None else os.environ

    provider = (env.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider not in PROVIDERS:
        raise KeyError(
            f"unknown LLM_PROVIDER {provider!r}; known providers: {sorted(PROVIDERS)}"
        )
    spec = PROVIDERS[provider]

    api_key = (
        env.get("LLM_API_KEY")
        or env.get(spec["env_key"])
        or (env.get(spec["env_key"]) or "")
    ).strip()
    model = (env.get("LLM_MODEL") or DEFAULT_MODEL_BY_PROVIDER[provider]).strip()
    base_url = (env.get("LLM_BASE_URL") or spec["base_url"]).strip()

    extra_headers: dict[str, str] = {}
    if provider == "openrouter" and env.get("OPENROUTER_SITE_URL"):
        extra_headers["HTTP-Referer"] = env["OPENROUTER_SITE_URL"]
        if env.get("OPENROUTER_APP_NAME"):
            extra_headers["X-Title"] = env["OPENROUTER_APP_NAME"]

    reasoning = env.get("LLM_REASONING_EFFORT", "").strip() or None

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(env.get("LLM_TEMPERATURE", "0") or 0),
        timeout=float(env.get("LLM_TIMEOUT_SECONDS", "60") or 60),
        max_retries=int(env.get("LLM_MAX_RETRIES", "4") or 4),
        requests_per_minute=float(env.get("LLM_REQUESTS_PER_MINUTE", "10") or 10),
        max_calls_per_run=int(env.get("LLM_MAX_CALLS_PER_RUN", "12") or 12),
        reasoning_effort=reasoning,
        extra_headers=extra_headers,
    )


__all__ = ["DEFAULT_MODEL_BY_PROVIDER", "PROVIDERS", "resolve"]

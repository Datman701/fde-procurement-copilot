"""The single entry point: ``analyze(request, architecture)``.

Shared by the UI, the CLI and the evaluation script. Everything that is not the
agent stage - tool layer, guardrail, output schema, LLM client - is identical for
"A" and "B"; only ``agents/agent_a.py`` vs ``agents/agent_b.py`` differs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from core.data import DataOverlay, DataStore, PurchaseRequest
from core.llm_client import DiskCache, LLMClient, LLMConfig
from core.schema import Decision, ToolResult
from tools.registry import build_registry

Architecture = Literal["A", "B"]

#: Starter-pack adapter names -> canonical architecture letters (ISS-15).
ARCHITECTURE_ALIASES = {
    "single": "A",
    "staged": "B",
    "a": "A",
    "b": "B",
    "1": "A",
    "2": "B",
}


def normalise_architecture(architecture: str) -> str:
    key = str(architecture).strip().lower()
    if key not in ARCHITECTURE_ALIASES:
        raise ValueError(
            f"unknown architecture {architecture!r}; expected 'A' or 'B' "
            f"(aliases: {sorted(ARCHITECTURE_ALIASES)})"
        )
    return ARCHITECTURE_ALIASES[key]


def _coerce_request(
    request: Any,
    store: DataStore,
    requester: str | None = None,
) -> PurchaseRequest:
    """Accept a request id, a dict, an existing request, or free text."""
    if isinstance(request, PurchaseRequest):
        return request
    if isinstance(request, str):
        found = store.request(request)
        if found is not None:
            return found
        # Not a known id: treat it as free-text from the UI.
        return PurchaseRequest.from_dict(
            {
                "request_id": "FREE-TEXT",
                "requester_id": requester or "",
                "product_name": request.strip()[:120],
                "vendor_name": "",
                "category": "",
                "annual_cost_usd": None,
                "user_count": None,
                "business_justification": request,
                "data_access_level": "unknown",
                "requested_integrations": [],
                "urgency": "normal",
            }
        )
    if isinstance(request, dict):
        return PurchaseRequest.from_dict(request)

    raise TypeError(f"cannot interpret request of type {type(request).__name__}")


def analyze(
    request: Any,
    architecture: str = "A",
    *,
    overlay: DataOverlay | dict[str, Any] | None = None,
    client: LLMClient | None = None,
    use_cache: bool = True,
    store: DataStore | None = None,
    requester: str | None = None,
    vendor_transport: str | None = None,
    tool_result_sink: Callable[[list[ToolResult]], None] | None = None,
) -> Decision:
    """Analyse one purchase request and return a validated Decision.

    Parameters
    ----------
    request:
        A request id (``"REQ-1005"``), a request dict, a :class:`PurchaseRequest`
        or a free-text description.
    architecture:
        ``"A"`` (single agent) or ``"B"`` (analyst + reviewer). The starter
        aliases ``single``/``staged`` are accepted.
    overlay:
        Optional in-memory data overlay for simulation (eval only). ``data/`` is
        never written.
    client:
        Inject an :class:`LLMClient` (used by tests). One is built from the
        environment otherwise.
    tool_result_sink:
        Optional observer for the actual envelopes produced during this run.
        It does not alter the Decision contract and is intended for local UI
        evidence display.
    """
    arch = normalise_architecture(architecture)
    data_store = store if store is not None else DataStore.load(overlay)
    parsed = _coerce_request(request, data_store, requester=requester)

    registry = build_registry(data_store, parsed, vendor_transport=vendor_transport)

    llm = client if client is not None else _build_client(use_cache=use_cache)
    llm.new_run()

    started = time.perf_counter()
    if arch == "A":
        from agents.agent_a import AgentA

        decision = AgentA(llm, registry).run(parsed)
    else:
        from agents.agent_b import AgentB

        decision = AgentB(llm, registry).run(parsed)

    # Wall-clock latency always reflects the whole analyze() call.
    decision.trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    decision.trace.architecture = arch  # type: ignore[assignment]
    if not decision.trace.tool_names:
        decision.trace.tool_names = sorted(dict.fromkeys(r.tool for r in registry.all()))
    if not decision.trace.tool_result_ids:
        decision.trace.tool_result_ids = [r.result_id for r in registry.all()]
    if not decision.trace.llm_calls:
        decision.trace.llm_calls = llm.llm_calls
    decision.trace.llm_cached_calls = llm.cached_calls
    if tool_result_sink is not None:
        tool_result_sink(list(registry.all()))
    return decision


def _build_client(use_cache: bool) -> LLMClient:
    config = LLMConfig.from_env()
    cache = DiskCache(enabled=use_cache)
    return LLMClient(config, cache=cache, use_cache=use_cache)


__all__ = ["ARCHITECTURE_ALIASES", "analyze", "normalise_architecture"]

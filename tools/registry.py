"""Shared tool registry: uniform envelope, unique result_ids, result store.

Every tool is a plain function ``(ctx: ToolContext, args: dict) -> dict``. The
registry is the only place that knows how to build the envelope, mint a
``result_id`` and record the call, which is what makes grounding checkable by
the guardrail.

Tools accept **structured arguments only**. Nothing in business data can add a
tool, remove a tool, or widen a tool's arguments beyond lookup keys (a request
id, a vendor name, a ``result_id`` from an earlier call).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from core.data import DataStore, PurchaseRequest
from core.schema import ToolResult


class ToolError(Exception):
    """Raised by a tool when it cannot produce a trustworthy answer.

    The registry converts this into ``ok=False`` with ``error`` set. It never
    substitutes a guessed or favourable value (policy section 10).
    """


class ToolContext:
    """Everything a tool is allowed to read.

    ``registry_results`` lets a tool resolve a ``result_id`` produced earlier in
    the same run (a lookup key, not a data channel).
    """

    def __init__(
        self,
        store: DataStore,
        request: PurchaseRequest | None = None,
        registry_results: dict[str, ToolResult] | None = None,
    ) -> None:
        self.store = store
        self.request = request
        self.registry_results = registry_results if registry_results is not None else {}

    def require_request(self) -> PurchaseRequest:
        if self.request is None:
            raise ToolError("no request in scope for this run")
        return self.request

    def resolve_request(self, request_id: str | None) -> PurchaseRequest:
        """A ``request_id`` argument is a lookup key, never a data channel."""
        if request_id:
            found = self.store.request(request_id)
            if found is None:
                raise ToolError(f"unknown request_id {request_id!r}")
            return found
        return self.require_request()


ToolFn = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


class ToolRegistry:
    """Holds the tool implementations and the results they produced this run."""

    def __init__(self, ctx: ToolContext, tools: dict[str, ToolFn], sources: dict[str, str]) -> None:
        self.ctx = ctx
        self._tools = tools
        self._sources = sources
        self._results: dict[str, ToolResult] = ctx.registry_results
        ctx.registry_results = self._results
        self._order: list[str] = []
        self.tool_calls = 0
        self.tool_names: list[str] = []

    # -- schema for the LLM ------------------------------------------------ #
    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        from tools.schemas import TOOL_SCHEMAS

        return [TOOL_SCHEMAS[name] for name in self._tools]

    # -- invocation -------------------------------------------------------- #
    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        args = dict(args or {})
        self.tool_calls += 1
        self.tool_names.append(name)

        fn = self._tools.get(name)
        if fn is None:
            return self._finish(
                name,
                ToolResult(
                    ok=False,
                    tool=name,
                    result_id="",
                    data={},
                    source="tool_registry",
                    as_of=None,
                    error=f"unknown tool {name!r}; available: {sorted(self._tools)}",
                ),
            )

        self._order.append(name)
        index = len(self._order)
        result_id = f"t{index:02d}-{name}"
        try:
            data = fn(self.ctx, args)
            result = ToolResult(
                ok=True,
                tool=name,
                result_id=result_id,
                data=data,
                source=self._sources.get(name, "internal"),
                as_of=data.get("as_of"),
                error=None,
            )
        except ToolError as exc:
            result = ToolResult(
                ok=False,
                tool=name,
                result_id=result_id,
                data={},
                source=self._sources.get(name, "internal"),
                as_of=None,
                error=str(exc),
            )
        except Exception as exc:  # defensive: a tool crash must never fail the run
            result = ToolResult(
                ok=False,
                tool=name,
                result_id=result_id,
                data={},
                source=self._sources.get(name, "internal"),
                as_of=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        return self._finish(name, result)

    def _finish(self, name: str, result: ToolResult) -> ToolResult:
        self._results[result.result_id] = result
        return result

    # -- lookup ------------------------------------------------------------ #
    def get(self, result_id: str | None) -> ToolResult | None:
        if not result_id:
            return None
        return self._results.get(result_id)

    def all(self) -> list[ToolResult]:
        return [self._results[rid] for rid in sorted(self._results)]

    def failures(self) -> list[ToolResult]:
        return [r for r in self.all() if not r.ok]

    def result_ids(self) -> list[str]:
        return [r.result_id for r in self.all()]

    def __iter__(self) -> Iterable[ToolResult]:
        return iter(self.all())


def build_registry(
    store: DataStore,
    request: PurchaseRequest | None = None,
    vendor_transport: str | None = None,
) -> ToolRegistry:
    """Wire up the full tool layer. Identical for architectures A and B."""
    from tools import approvals, budget, catalog, employee, policy_lookup, request_validation, vendor

    ctx = ToolContext(store=store, request=request)
    transport = vendor_transport or "http"
    return ToolRegistry(
        ctx=ctx,
        tools={
            "request_validation": request_validation.run,
            "employee_lookup": employee.run,
            "budget_check": budget.run,
            "vendor_status": vendor.run,
            "approval_router": approvals.run,
            "catalog_search": catalog.run,
            "policy_lookup": policy_lookup.run,
        },
        sources={
            "request_validation": "data/requests.json",
            "employee_lookup": "data/employees.csv",
            "budget_check": "data/department_budgets.csv",
            "vendor_status": f"vendor-risk-service:{transport}",
            "approval_router": "data/procurement_policy.md + data/vendors.csv",
            "catalog_search": "data/software_catalog.csv + data/purchase_history.csv",
            "policy_lookup": "data/procurement_policy.md",
        },
    )


__all__ = ["ToolContext", "ToolError", "ToolRegistry", "build_registry"]

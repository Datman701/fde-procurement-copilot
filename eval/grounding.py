"""Evaluation-time grounding check.

Deliberately independent of :mod:`core.guardrail`: the guardrail filters
ungrounded evidence *before* the decision is returned, so scoring the
guardrailed output would measure nothing. This module re-verifies from the
decision and the raw tool results, using the same rule, so the reported
"grounded evidence" fraction is a real check of the emitted claims.
"""

from __future__ import annotations

from typing import Any


def _flatten(value: Any) -> str:
    import json

    return json.dumps(value, default=str, sort_keys=True)


def _normalise(text: str) -> str:
    return text.lower().replace(",", "").replace("$", "").replace("_", " ")


def _as_number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _numbers(value: Any) -> set[float]:
    found: set[float] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.add(float(node))
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


def item_is_grounded(item: Any, result: Any) -> bool:
    """True iff ``result`` supports ``item``'s quoted value."""
    if result is None or not getattr(result, "ok", False):
        return False
    quote = (getattr(item, "quote_or_value", "") or "").strip()
    if not quote:
        return False
    payload = _flatten(result.data)
    if quote in payload:
        return True
    if _normalise(quote) and _normalise(quote) in _normalise(payload):
        return True
    number = _as_number(quote)
    if number is not None and number in _numbers(result.data):
        return True
    return False


def check_grounding(evidence: list[Any], tool_results: list[Any]) -> tuple[int, int]:
    """Return ``(total_items, grounded_items)``."""
    by_id = {result.result_id: result for result in tool_results}
    total = len(evidence)
    grounded = sum(
        1 for item in evidence if item_is_grounded(item, by_id.get(item.result_id))
    )
    return total, grounded


__all__ = ["check_grounding", "item_is_grounded"]

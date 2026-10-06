"""``python -m app.cli`` - the command-line entry point.

Examples
--------
    python -m app.cli REQ-1005
    python -m app.cli REQ-1005 --arch B --json
    python -m app.cli --list
    python -m app.cli --all --arch A
    echo "Marketing wants a design tool" | python -m app.cli - --requester E001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analyze import analyze  # noqa: E402
from core.data import DataStore  # noqa: E402

SEVERITY_MARK = {
    "PROCEED": "[ PROCEED ]",
    "NEEDS_APPROVAL": "[NEEDS APPROVAL]",
    "ESCALATE_TO_HUMAN": "[ ESCALATE TO HUMAN ]",
}


def render(decision, show_raw: bool = False, raw_results=None) -> str:
    lines: list[str] = []
    trace = decision.trace
    lines.append("=" * 78)
    lines.append(f"{decision.request_id}   architecture {trace.architecture}")
    lines.append(SEVERITY_MARK[decision.severity])
    if decision.needs_human_review:
        lines.append("HUMAN REVIEW REQUIRED - this copilot cannot approve anything.")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"RECOMMENDATION : {decision.recommendation}")
    lines.append(f"NEXT STEP      : {decision.next_step}")

    lines.append("")
    lines.append(f"APPROVALS REQUIRED ({len(decision.approvals_required)})")
    if not decision.approvals_required:
        lines.append("  (none identified)")
    for approval in decision.approvals_required:
        lines.append(f"  - {approval.approver:<18} [{approval.policy_clause}] {approval.reason}")

    lines.append("")
    lines.append(f"MISSING INFORMATION ({len(decision.missing_information)})")
    if not decision.missing_information:
        lines.append("  (nothing outstanding)")
    for item in decision.missing_information:
        lines.append(f"  - {item}")

    lines.append("")
    lines.append(f"RISK FLAGS ({len(decision.risk_flags)})")
    if not decision.risk_flags:
        lines.append("  (none)")
    for flag in decision.risk_flags:
        source = f" [{flag.source_result_id}]" if flag.source_result_id else ""
        lines.append(f"  - {flag.flag} ({flag.severity}){source}")
        if flag.detail:
            lines.append(f"      {flag.detail}")

    lines.append("")
    lines.append(f"EVIDENCE ({len(decision.evidence)} grounded items)")
    if not decision.evidence:
        lines.append("  (none survived the grounding check)")
    for item in decision.evidence:
        lines.append(f"  - {item.claim}")
        lines.append(f"      {item.result_id}: {item.quote_or_value[:120]}")

    if show_raw and raw_results:
        lines.append("")
        lines.append("RAW TOOL RESULTS")
        for result in raw_results:
            status = "ok" if result.ok else f"FAILED: {result.error}"
            lines.append(f"  {result.result_id}  source={result.source}  {status}")
            if result.ok:
                payload = json.dumps(result.data, default=str, sort_keys=True)
                lines.append(f"      {payload[:600]}")

    lines.append("")
    lines.append("TRACE")
    lines.append(
        f"  llm_calls={trace.llm_calls} (cached {trace.llm_cached_calls})  "
        f"tool_calls={len(trace.tool_result_ids)}  latency={trace.latency_ms:.0f} ms"
    )
    lines.append(f"  tools: {', '.join(trace.tool_names)}")
    if trace.guardrail_notes:
        lines.append("  guardrail:")
        for note in trace.guardrail_notes:
            lines.append(f"    - {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Analyse a procurement request through the AI copilot.",
    )
    parser.add_argument("request", nargs="?", help="request id, or free text with '-'")
    parser.add_argument(
        "--arch", "--architecture", default="A", help="A (single agent) or B (staged)"
    )
    parser.add_argument("--requester", default=None, help="requester id, e.g. E001")
    parser.add_argument("--json", action="store_true", help="emit the full Decision as JSON")
    parser.add_argument("--raw", action="store_true", help="show raw tool results")
    parser.add_argument("--list", action="store_true", help="list available request ids")
    parser.add_argument("--all", action="store_true", help="analyse every example request")
    parser.add_argument(
        "--no-cache", action="store_true", help="bypass the on-disk LLM response cache"
    )
    parser.add_argument(
        "--force-vendor-fail",
        action="store_true",
        help="simulate a vendor-risk service outage",
    )
    args = parser.parse_args(argv)

    store = DataStore.load()
    if args.list:
        for request in store.requests:
            print(f"{request.request_id}  {request.product_name}  ({request.vendor_name})")
        return 0

    if not args.request and not args.all:
        parser.error("provide a request id, free text with '-', or use --all/--list")

    if args.force_vendor_fail:
        os.environ["VENDOR_RISK_FORCE_FAIL"] = "1"

    targets = [r.request_id for r in store.requests] if args.all else [args.request]
    free_text = args.request == "-"
    if free_text:
        text = sys.stdin.read().strip()
        targets = [text or "Unspecified request"]

    for target in targets:
        decision = analyze(
            target,
            architecture=args.arch,
            use_cache=not args.no_cache,
            store=store,
            requester=args.requester,
        )
        if args.json:
            print(json.dumps(decision.model_dump(), indent=2, default=str))
        else:
            registry_results = None
            if args.raw:
                from tools.registry import build_registry

                from app.analyze import _coerce_request

                registry = build_registry(store, _coerce_request(target, store, args.requester))
                # Reuse the results already recorded on the decision.
                registry_results = _results_for(store, decision)
            print(render(decision, show_raw=args.raw, raw_results=registry_results))

    return 0


def _results_for(store: DataStore, decision) -> list | None:
    """Raw envelopes for display; re-derived only when explicitly requested."""
    from core.data import PurchaseRequest
    from tools.registry import build_registry

    request = store.request(decision.request_id)
    if request is None:
        return None
    registry = build_registry(store, request)
    by_id = {result_id: None for result_id in decision.trace.tool_result_ids}
    for result in registry.all():
        if result.result_id in by_id:
            by_id[result.result_id] = result
    return [by_id[key] for key in by_id if by_id[key] is not None]


if __name__ == "__main__":
    raise SystemExit(main())

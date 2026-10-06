"""Markdown comparison table for an evaluation run."""

from __future__ import annotations

from typing import Any


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value * 100:.1f}%" if pct else f"{value:.3f}"
    return str(value)


def render(payload: dict[str, Any], architectures: list[str]) -> str:
    meta = payload["metadata"]
    summary = payload["summary"]
    lines: list[str] = []

    lines.append("# Evaluation results")
    lines.append("")
    lines.append(f"- **Timestamp (UTC):** {meta['timestamp_utc']}")
    lines.append(f"- **Cases:** {meta['n_cases']} (split: `{meta.get('split', 'all')}`)")
    lines.append(
        f"- **Architectures:** {', '.join(architectures)}  "
        f"(identical tool layer, guardrail, schema and LLM client)"
    )
    lines.append(f"- **Model:** `{meta['llm_model']}` via `{meta['llm_provider']}`, temperature 0")
    lines.append(f"- **API key present:** {meta['llm_api_key_present']}")
    lines.append(
        f"- **Policy version {meta['policy_version']}**, reference date "
        f"{meta['policy_reference_date']}"
    )
    lines.append(f"- **LLM cache used:** {meta['cache_used']} (`{meta['cache_path']}`)")
    lines.append(f"- **Vendor-risk transport:** {meta['vendor_risk_transport']}")
    lines.append(f"- **Git commit:** `{meta['git_commit']}` · Python {meta['python']}")
    lines.append(f"- **Wall clock:** {payload['wall_clock_seconds']}s")
    lines.append("")
    lines.append(
        "> **Prompt tuning disclosure.** Prompts were developed and tuned against the "
        "`dev` split only. The `held_out` split was run once per architecture for the "
        "final numbers reported here and was not used for iteration."
    )
    lines.append("")

    # -- headline ---------------------------------------------------------- #
    for split in ("all", "held_out", "dev"):
        lines.append(f"## Results - {split}")
        lines.append("")
        lines.append(_table(summary, architectures, split))
        lines.append("")

    # -- comparison -------------------------------------------------------- #
    if len(architectures) == 2 and payload.get("comparison"):
        a, b = architectures
        lines.append(f"## {b} minus {a}")
        lines.append("")
        lines.append("Positive = B is better for match/rate metrics and higher for cost metrics.")
        lines.append("")
        lines.append("| Metric | Delta (B - A) | Better |")
        lines.append("|---|---:|---|")
        for key, value in payload["comparison"].get("all", {}).items():
            if key in {"latency_p50_ms", "latency_p95_ms", "llm_calls_mean", "tool_calls_mean"}:
                better = "A (lower cost)" if value < 0 else ("B (lower cost)" if value > 0 else "tie")
            else:
                better = "B" if value > 0 else ("A" if value < 0 else "tie")
            lines.append(f"| `{key}` | {value:+.3f} | {better} |")
        lines.append("")

    # -- per-tag ----------------------------------------------------------- #
    lines.append("## Per-edge-case-tag breakdown")
    lines.append("")
    for architecture in architectures:
        rows = payload["by_tag"][architecture]
        lines.append(f"### {architecture}")
        lines.append("")
        lines.append(
            "| Tag | n | Rec. match | Policy | Grounded | Esc. recall | p50 ms | LLM calls | Misses |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for row in rows:
            misses = ", ".join(row["failed_cases"]) or "-"
            lines.append(
                f"| `{row['tag']}` | {row['n']} | {_fmt(row['recommendation_match'], True)} | "
                f"{_fmt(row['policy_followed'], True)} | {_fmt(row['grounded_evidence'], True)} | "
                f"{_fmt(row['escalation_recall'], True)} | {row['latency_p50_ms']:.0f} | "
                f"{row['llm_calls_mean']:.1f} | {misses} |"
            )
        lines.append("")

    # -- failures ---------------------------------------------------------- #
    for architecture in architectures:
        failed = payload["failures"][architecture]
        lines.append(f"## {architecture} - cases with findings ({len(failed)})")
        lines.append("")
        if not failed:
            lines.append("None.")
            lines.append("")
            continue
        lines.append("| Case | Split | Recommendation | Severity | Approvals | Evidence | Problems |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in failed:
            problems = "<br>".join(row["problems"][:3])
            lines.append(
                f"| {row['case_id']} | {row['split']} | {row['recommendation']} | "
                f"{row['severity']} | {row['approvals']} | {row['grounded_evidence']} | "
                f"{problems} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def _table(summary: dict[str, Any], architectures: list[str], split: str) -> str:
    rows = [
        ("Cases", "n", False),
        ("Correct recommendation / next action", "recommendation_match", True),
        ("Severity exact", "severity_match", True),
        ("Policy + deterministic rules followed", "policy_followed", True),
        ("Approvals set exact", "approvals_exact_match", True),
        ("Severity never loosened", "no_severity_loosened", True),
        ("Evidence grounded in tool results", "grounded_evidence", True),
        ("Escalation precision", "escalation_precision", True),
        ("Escalation recall", "escalation_recall", True),
        ("Escalation F1", "escalation_f1", True),
        ("Human-review correct", "human_review_match", True),
        ("Latency p50 (ms)", "latency_p50_ms", False),
        ("Latency p95 (ms)", "latency_p95_ms", False),
        ("Latency mean (ms)", "latency_mean_ms", False),
        ("LLM calls (mean)", "llm_calls_mean", False),
        ("LLM calls (total)", "llm_calls_total", False),
        ("Tool calls (mean)", "tool_calls_mean", False),
        ("Tool calls (total)", "tool_calls_total", False),
    ]
    header = f"| Metric | {split} |" + "".join(f" {a} |" for a in architectures)
    lines = [header, "|---|---:|" + "---:|" * len(architectures)]
    for label, key, is_pct in rows:
        line = f"| {label} |"
        for architecture in architectures:
            value = summary[architecture][split].get(key)
            if key == "n":
                line += f" {value} |"
            elif key == "human_review_match" and value is None:
                line += " - |"
            elif is_pct:
                line += f" {_fmt(value, True)} |"
            else:
                line += f" {_fmt(value)} |"
        lines.append(line)
    return "\n".join(lines)


__all__ = ["render"]

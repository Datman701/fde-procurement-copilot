"""Substitute the real evaluation numbers into docs/memo.md and README.md.

Usage: python -m eval.fill_report_numbers [results_json]

With no argument, the newest ``eval/results/*.json`` that covers both
architectures and all 28 cases is used. Word-count check for the memo is
included (the brief allows 500 words maximum).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMO = ROOT / "docs" / "memo.md"
README = ROOT / "README.md"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f} ms"


def _calls(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def pick_results() -> Path:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.is_file():
            raise SystemExit(f"not a file: {path}")
        return path
    candidates = sorted((ROOT / "eval" / "results").glob("*.json"))
    for path in reversed(candidates):
        data = json.loads(path.read_text())
        meta = data.get("metadata", {})
        if meta.get("architectures") == ["A", "B"] and meta.get("n_cases") == 28:
            return path
    raise SystemExit(
        "no results file with both architectures and 28 cases; run make eval first"
    )


def main() -> int:
    path = pick_results()
    data = json.loads(path.read_text())
    summary = data["summary"]
    meta = data["metadata"]

    def both(key: str, split: str = "all") -> tuple[str, str]:
        a = summary["A"][split].get(key)
        b = summary["B"][split].get(key)
        return a, b

    def esc(arch: str, split: str = "all") -> str:
        row = summary[arch][split]
        precision, recall = row.get("escalation_precision"), row.get("escalation_recall")
        return f"{_pct(precision)} / {_pct(recall)}"

    tightened = sum(
        1
        for case in data.get("cases", [])
        if case.get("architecture") == "B"
        and (case.get("raw", {}).get("trace", {}).get("reviewer_tightened") or [])
    )

    a_rec, b_rec = both("recommendation_match")
    a_pol, b_pol = both("policy_followed")
    a_grd, b_grd = both("grounded_evidence")
    a_p50, b_p50 = both("latency_p50_ms")
    a_calls, b_calls = both("llm_calls_mean")

    values = {
        "{{MODEL}}": f"{meta.get('llm_provider')}/{meta.get('llm_model')}",
        "{{RESULTS_FILE}}": path.name,
        "{{A_REC}}": _pct(a_rec),
        "{{B_REC}}": _pct(b_rec),
        "{{A_POL}}": _pct(a_pol),
        "{{B_POL}}": _pct(b_pol),
        "{{A_GRD}}": _pct(a_grd),
        "{{B_GRD}}": _pct(b_grd),
        "{{A_ESC}}": esc("A"),
        "{{B_ESC}}": esc("B"),
        "{{A_P50}}": _ms(a_p50),
        "{{B_P50}}": _ms(b_p50),
        "{{A_CALLS}}": _calls(a_calls),
        "{{B_CALLS}}": _calls(b_calls),
        "{{B_TIGHTENED}}": str(tightened),
    }

    memo = MEMO.read_text()
    for key, value in values.items():
        memo = memo.replace(key, value)
    leftovers = re.findall(r"\{\{[A-Z_]+\}\}", memo)
    if leftovers:
        raise SystemExit(f"unsubstituted placeholders in memo: {leftovers}")
    MEMO.write_text(memo)

    words = len(re.findall(r"\S+", memo))
    if words > 500:
        raise SystemExit(f"memo is {words} words (limit 500)")

    readme = README.read_text()
    new_block = (
        "**Published comparison** (28 cases, both architectures, model "
        f"{meta.get('llm_provider')}/{meta.get('llm_model')}, results file "
        f"`eval/results/{path.name}`): Architecture A matched the hand labels on "
        f"{_pct(a_rec)} of recommendation/next-action pairs vs B {_pct(b_rec)}; "
        f"deterministic-policy compliance {_pct(a_pol)} vs {_pct(b_pol)}; grounded "
        f"evidence {_pct(a_grd)} vs {_pct(b_grd)}; escalation precision/recall "
        f"{esc('A')} vs {esc('B')}; median latency {_ms(a_p50)} vs {_ms(b_p50)}; "
        f"mean LLM calls per case {_calls(a_calls)} vs {_calls(b_calls)}. "
        "See `docs/memo.md` for the ship decision and its reasoning."
    )
    readme = re.sub(
        r"\*\*Published comparison:\*\*[^\n]*", new_block, readme, count=1
    )
    README.write_text(readme)

    print(f"memo: {words} words (limit 500) — placeholders substituted from {path.name}")
    print("README published-comparison line updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

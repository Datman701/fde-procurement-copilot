"""``python -m eval.run`` - the reproducible evaluation script.

    python -m eval.run --arch A|B|both [--no-cache] [--split dev|held_out|all]

Writes ``eval/results/<timestamp>.json`` plus a ``results.md`` comparison table
and prints a summary. Temperature is 0. With the committed cache it reproduces
from a clean checkout with no API key.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS_DIR = ROOT / "eval" / "results"


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def load_cases(path: Path, split: str | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if split and split != "all" and case["split"] != split:
            continue
        cases.append(case)
    return cases


# --------------------------------------------------------------------------- #
# Vendor-risk service
# --------------------------------------------------------------------------- #
class VendorService:
    """Keeps the mock vendor-risk API reachable for the duration of a run.

    Uses ``VENDOR_RISK_TRANSPORT=inprocess`` (the same ``mock_api.app`` code) when
    port 8001 is not up, so ``make eval`` works from a clean checkout with no
    manual setup. If port 8001 *is* up, the real HTTP path is used.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.transport = "inprocess"

    def start(self) -> str:
        if os.getenv("VENDOR_RISK_TRANSPORT"):
            self.transport = os.getenv("VENDOR_RISK_TRANSPORT", "")
            return self.transport
        if self._port_is_up():
            self.transport = "http"
            os.environ["VENDOR_RISK_TRANSPORT"] = "http"
            return self.transport

        # Start the service ourselves so the HTTP tool path is exercised.
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "mock_api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8001",
                    "--log-level",
                    "warning",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if self._wait_for_health():
                self.transport = "http"
                os.environ["VENDOR_RISK_TRANSPORT"] = "http"
                return self.transport
            self._stop()
        except Exception:
            pass

        self.transport = "inprocess"
        os.environ["VENDOR_RISK_TRANSPORT"] = "inprocess"
        return self.transport

    def stop(self) -> None:
        self._stop()
        os.environ.pop("VENDOR_RISK_TRANSPORT", None)

    def _stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    @staticmethod
    def _port_is_up() -> bool:
        try:
            import requests

            base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001")
            return requests.get(f"{base.rstrip('/')}/health", timeout=0.5).ok
        except Exception:
            return False

    def _wait_for_health(self, timeout: float = 15.0) -> bool:
        try:
            import requests

            base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    return False
                try:
                    if requests.get(f"{base.rstrip('/')}/health", timeout=0.5).ok:
                        return True
                except Exception:
                    time.sleep(0.2)
            return False
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run_case(case: dict[str, Any], architecture: str, use_cache: bool) -> dict[str, Any]:
    """Run one case through ``analyze`` and score it."""
    from app.analyze import analyze
    from core.data import DataStore
    from eval.metrics import score_case
    from tools.registry import build_registry

    overlay = case.get("overlay")
    store = DataStore.load(overlay)
    request = dict(case["request"])
    parsed_request = store.request(request.get("request_id", "")) or request

    started = time.perf_counter()
    decision = analyze(
        parsed_request,
        architecture=architecture,
        overlay=overlay,
        use_cache=use_cache,
        store=store,
    )
    wall_ms = (time.perf_counter() - started) * 1000

    # Re-derive the raw tool results so grounding is checked against the actual
    # payloads this case produced, including overlay-simulated ones.
    registry = build_registry(store, parsed_request)
    results = registry.all()

    scored = score_case(case, decision, results)
    scored.latency_ms = round(wall_ms, 1)
    scored.raw = {
        "next_step": decision.next_step,
        "risk_flags": [
            {"flag": f.flag, "severity": f.severity, "source_result_id": f.source_result_id}
            for f in decision.risk_flags
        ],
        "approvals": [
            {"approver": a.approver, "policy_clause": a.policy_clause, "reason": a.reason}
            for a in decision.approvals_required
        ],
        "evidence": [
            {"claim": e.claim, "result_id": e.result_id, "quote_or_value": e.quote_or_value}
            for e in decision.evidence
        ],
        "missing_information": decision.missing_information,
        "trace": decision.trace.model_dump(),
        "guardrail_notes": decision.trace.guardrail_notes,
        "tool_results": [
            {
                "result_id": r.result_id,
                "tool": r.tool,
                "ok": r.ok,
                "source": r.source,
                "as_of": r.as_of,
                "error": r.error,
            }
            for r in results
        ],
    }
    return scored.__dict__


def run_all(
    cases: list[dict[str, Any]],
    architectures: list[str],
    use_cache: bool,
) -> dict[str, Any]:
    from eval.metrics import CaseResult, by_tag, delta, failures, summarise

    all_results: list[CaseResult] = []
    per_arch: dict[str, list[CaseResult]] = {}
    started = time.perf_counter()

    for architecture in architectures:
        rows: list[CaseResult] = []
        print(f"\n=== architecture {architecture} ===")
        for case in cases:
            try:
                raw = run_case(case, architecture, use_cache)
            except Exception as exc:  # a crash is a failure, never an abort
                raw = _crash_record(case, architecture, exc)
                print(f"  CRASH {case['case_id']}: {type(exc).__name__}: {exc}")
            result = CaseResult(**raw)
            rows.append(result)
            status = "ok  " if result.recommendation_match else "MISS"
            print(
                f"  {status} {result.case_id} [{result.split:<8}] "
                f"{result.latency_ms:>7.0f}ms llm={result.llm_calls} tools={result.tool_calls} "
                f"{result.recommendation_actual} (want {result.recommendation_expected})"
            )
            if result.notes:
                for note in result.notes[:3]:
                    print(f"         ! {note}")
        per_arch[architecture] = rows
        all_results.extend(rows)

    summaries = {
        architecture: {
            "all": summarise(rows, architecture, None),
            "dev": summarise(rows, architecture, "dev"),
            "held_out": summarise(rows, architecture, "held_out"),
        }
        for architecture, rows in per_arch.items()
    }

    payload: dict[str, Any] = {
        "metadata": _metadata(use_cache, architectures, len(cases)),
        "summary": summaries,
        "by_tag": {
            architecture: by_tag(rows, architecture) for architecture, rows in per_arch.items()
        },
        "failures": {
            architecture: failures(rows) for architecture, rows in per_arch.items()
        },
        "comparison": {},
        "cases": [r.__dict__ for r in all_results],
        "wall_clock_seconds": round(time.perf_counter() - started, 1),
    }

    if len(architectures) == 2:
        a, b = architectures
        for split in ("all", "dev", "held_out"):
            payload["comparison"][split] = delta(
                summaries[a][split], summaries[b][split]
            )

    return payload


def _crash_record(case: dict, architecture: str, exc: Exception) -> dict[str, Any]:
    expected = case["expected"]
    return {
        "case_id": case["case_id"],
        "split": case["split"],
        "title": case["title"],
        "tags": list(case.get("tags", [])),
        "architecture": architecture,
        "recommendation_expected": expected["recommendation"],
        "recommendation_actual": "CRASH",
        "recommendation_match": False,
        "severity_expected": expected["severity"],
        "severity_actual": "CRASH",
        "severity_ok": False,
        "approvals_expected": list(expected["approvals"]),
        "approvals_actual": [],
        "approvals_exact": False,
        "approvals_missing": list(expected["approvals"]),
        "approvals_extra": [],
        "severity_loosened": False,
        "policy_followed": False,
        "evidence_total": 0,
        "evidence_grounded": 0,
        "grounded_fraction": 0.0,
        "must_escalate": bool(expected["must_escalate"]),
        "escalated": False,
        "escalation_correct": not expected["must_escalate"],
        "human_review_actual": False,
        "human_review_expected": expected.get("human_review_required"),
        "human_review_correct": None,
        "latency_ms": 0.0,
        "llm_calls": 0,
        "tool_calls": 0,
        "risk_flags": [],
        "missing_count": 0,
        "must_flag_present": [],
        "must_flag_absent": list(expected.get("must_flag", [])),
        "notes": [f"CRASH: {type(exc).__name__}: {exc}"],
        "raw": {"error": f"{type(exc).__name__}: {exc}"},
    }


def _metadata(use_cache: bool, architectures: list[str], n_cases: int) -> dict[str, Any]:
    from core import policy
    from core.llm_client import LLMConfig

    try:
        config = LLMConfig.from_env()
        provider, model, key_present = config.provider, config.model, bool(config.api_key)
        base_url = config.base_url
    except Exception as exc:  # pragma: no cover
        provider, model, key_present, base_url = "unknown", "unknown", False, str(exc)

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "architectures": architectures,
        "n_cases": n_cases,
        "temperature": 0,
        "llm_provider": provider,
        "llm_model": model,
        "llm_api_key_present": key_present,
        "llm_base_url": base_url,
        "cache_used": use_cache,
        "cache_path": os.getenv("LLM_CACHE_PATH", "eval/cache/llm_cache.json"),
        "policy_version": policy.POLICY_VERSION,
        "policy_reference_date": policy.REFERENCE_DATE.isoformat(),
        "vendor_risk_transport": os.getenv("VENDOR_RISK_TRANSPORT", "unknown"),
        "python": sys.version.split()[0],
        "git_commit": _git_commit(),
        "note": (
            "Cached LLM responses still count toward llm_calls, so these numbers mean "
            "the same thing with or without a warm cache."
        ),
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def write_results(
    payload: dict[str, Any],
    architectures: list[str],
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    out_dir = Path(out_dir) if out_dir else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path = out_dir / f"{stamp}.results.md"
    md_path.write_text(render_markdown(payload, architectures), encoding="utf-8")
    return json_path, md_path


def render_markdown(payload: dict[str, Any], architectures: list[str]) -> str:
    from eval.report import render

    return render(payload, architectures)


def print_summary(payload: dict[str, Any], architectures: list[str]) -> None:
    meta = payload["metadata"]
    print("\n" + "=" * 78)
    print(f"EVALUATION SUMMARY   {meta['n_cases']} cases   model {meta['llm_model']}")
    print(f"provider={meta['llm_provider']}  temperature={meta['temperature']}  cache={meta['cache_used']}")
    print(f"policy {meta['policy_version']} reference date {meta['policy_reference_date']}")
    print("=" * 78)

    headline = [
        ("Recommendation match", "recommendation_match_count"),
        ("Policy followed", "policy_followed_count"),
        ("Approvals exact", "approvals_exact_match_count"),
        ("Grounded evidence", "grounded_evidence_count"),
        ("Escalation P", "escalation_precision"),
        ("Escalation R", "escalation_recall"),
        ("Latency p50 (ms)", "latency_p50_ms"),
        ("Latency p95 (ms)", "latency_p95_ms"),
        ("LLM calls (mean)", "llm_calls_mean"),
        ("Tool calls (mean)", "tool_calls_mean"),
    ]
    header = f"{'metric':<24}" + "".join(f"{a:>14}" for a in architectures) + (
        f"{'delta B-A':>14}" if len(architectures) == 2 else ""
    )
    print(header)
    print("-" * len(header))
    for label, key in headline:
        row = f"{label:<24}"
        for architecture in architectures:
            value = payload["summary"][architecture]["all"].get(key)
            if isinstance(value, float):
                row += f"{value:>14.3f}"
            else:
                row += f"{str(value):>14}"
        if len(architectures) == 2:
            delta_value = payload["comparison"]["all"].get(
                "escalation_recall" if "R" in label else key
            )
            row += f"{str(delta_value) if delta_value is not None else '-':>14}"
        print(row)

    if len(architectures) == 2:
        print("\nheld-out only (prompts were tuned on dev):")
        for label, key in headline[:8]:
            row = f"  {label:<22}"
            for architecture in architectures:
                value = payload["summary"][architecture]["held_out"].get(key)
                row += f"{(f'{value:.3f}' if isinstance(value, float) else str(value)):>14}"
            print(row)

    for architecture in architectures:
        failed = payload["failures"][architecture]
        print(f"\n{architecture}: {len(failed)} case(s) with findings")
        for row in failed[:12]:
            print(f"  {row['case_id']} [{row['split']}] {row['title']}")
            for problem in row["problems"][:2]:
                print(f"      - {problem}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run",
        description="Run the hand-labelled evaluation set against one or both architectures.",
    )
    parser.add_argument("--arch", default="both", help="A, B, or both")
    parser.add_argument("--split", default="all", choices=["dev", "held_out", "all"])
    parser.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    parser.add_argument("--no-cache", action="store_true", help="ignore the LLM cache")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = parser.parse_args(argv)

    architectures = (
        ["A", "B"] if args.arch.lower() == "both" else [args.arch.strip().upper()]
    )
    for architecture in architectures:
        if architecture not in {"A", "B"}:
            parser.error("--arch must be A, B or both")

    cases = load_cases(Path(args.cases), args.split)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    service = VendorService()
    transport = service.start()
    print(f"vendor-risk transport: {transport}")
    print(f"cases: {len(cases)}  split: {args.split}  cache: {not args.no_cache}")

    try:
        payload = run_all(cases, architectures, use_cache=not args.no_cache)
    finally:
        service.stop()

    payload["metadata"]["cases_file"] = str(Path(args.cases).relative_to(ROOT))
    payload["metadata"]["split"] = args.split
    json_path, md_path = write_results(payload, architectures, Path(args.out_dir))

    print_summary(payload, architectures)
    print(f"\nresults json : {json_path.relative_to(ROOT)}")
    print(f"results table: {md_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

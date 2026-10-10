# AI Procurement Request Copilot

An internal decision-support tool for software and service procurement. It
gathers budget, catalog, vendor-risk, and policy evidence before recommending a
human next step. It never purchases software, approves spend, changes budgets,
accepts terms, or overrides Security, Privacy, Legal, Finance, or Procurement.

## Status

The shared core, both agent architectures, UI, and evaluation harness are
implemented and passing (286 tests). A 28-case live comparison of both
architectures has been published; see **Evaluation** below for the measured
numbers, the results file, and the ship decision in `docs/memo.md`.

Results reproduce without an API key: every LLM response used by the published
run is committed in `eval/cache/llm_cache.json`, and `make eval` replays it.
Do not infer metrics from safe fallback behaviour — that path only triggers on
a cache miss with no key, and it is a resilience test rather than a quality
evaluation.

## Quick start

Python 3.12 is required. On Windows use the equivalent virtual-environment
executable, or set `PYTHON=python` when running `make`.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
# Set LLM_API_KEY, and optionally LLM_PROVIDER / LLM_MODEL.

make verify
make test
make run
```

`make run` starts the local vendor-risk API at `127.0.0.1:8001` and the copilot
UI at `127.0.0.1:8501` (a React app served by the FastAPI bridge in
`app/api.py`, which calls the same `analyze()` used by the CLI and evaluator).
Rebuild the UI with `make ui-build` after editing `frontend/` (requires
Node.js), or run the previous Streamlit interface with `make run-streamlit`.
Never commit `.env` or credentials.

## Commands

```bash
make run                         # local vendor-risk API + React UI
make run-streamlit               # previous Streamlit UI instead
make test                        # unit/integration suite
make verify                      # starter-pack preflight
make eval                        # both architectures, all 28 cases
make ui-build                    # rebuild frontend/ after UI edits

.venv/bin/python -m app.cli --list
.venv/bin/python -m app.cli REQ-1005 --arch A --json
.venv/bin/python -m eval.run --arch both --split dev
.venv/bin/python -m eval.run --arch both --split held_out
```

The evaluator writes timestamped JSON and Markdown reports to `eval/results/`.
It uses the response cache if present; cached LLM responses still count toward
the reported LLM-call total, and a cache hit needs no API key — so `make eval`
reproduces the published numbers from a clean checkout with no `.env` key set.
Use `--fresh` to re-query the provider while still updating the cache, which is
what honest latency figures require. Without both a key and cached responses,
the safe fallback escalates to human review. That is a resilience test, not a
valid model-quality evaluation.

## Workflow and architecture

```text
employee request -> evidence tools -> deterministic policy checks
                 -> constrained model recommendation -> human review
```

Both variants use the same `analyze(request, architecture)` entry point, tools,
Pydantic decision schema, LLM client, and guardrail. They differ only in agent
stage:

| Variant | Agent stage | Trade-off |
|---|---|---|
| A | One tool-using procurement agent, capped at four rounds | Lower orchestration/call overhead |
| B | Tool-using analyst plus tool-less policy/risk reviewer | Extra scrutiny; reviewer may only tighten |

Code—not the model—computes budgets, threshold bands, policy clauses, required
approvals, review gates, and severity floor. The shared guardrail validates the
draft; verifies evidence `result_id`s and values; recomputes deterministic
requirements; takes the maximum severity; and forces a safe path when evidence
is missing, expired, conflicting, or unavailable.

Read [`docs/architecture.md`](docs/architecture.md) for diagrams and detailed
design. [`docs/starter_pack_issues.md`](docs/starter_pack_issues.md) records
the actual data schema, policy rules, starter-pack defects, and assumptions.

## Free-text requests

Requests can also be described in prose instead of picked from the catalog.
`core/free_text.py` recovers structured fields **deterministically** — no model
call — and only from what the text states explicitly:

* money (`$32,000`, `9000 USD`, `9000 per year`, `$12k`) and seat counts
  (`65 seats`, `users: 3`). A seat count is never read as a price.
* product and vendor, resolved against the real catalog and vendor list, so a
  request cannot invent a vendor that Security would then be asked to review.
* data-access level, requester (`Requested by Sarah Lee` → the employee's id)
  and urgency, each only when named.

Nothing is inferred. Vague prose still yields `request_more_info`, and the UI
shows a *"What we read from your request"* panel listing every recovered field
with the exact wording that justified it. The published evaluation uses catalog
request ids only, so free-text extraction does not affect any reported number.

## Evidence tools

| Tool | Purpose |
|---|---|
| `request_validation` | Required information and request-text injection check |
| `employee_lookup` | Requester, manager, department, and department head |
| `budget_check` | Available-budget fit and shortfall |
| `vendor_status` | Live vendor risk, expiry, registry conflicts |
| `approval_router` | Deterministic Finance/Security/Privacy/Legal routing |
| `catalog_search` | Existing-tool candidates and overlap rationale |
| `policy_lookup` | Policy text by clause |

Every tool returns `ok`, `tool`, `result_id`, `data`, `source`, `as_of`, and
`error`. Request, catalog, and vendor prose is untrusted business data: it
cannot alter policy, prompts, or tool definitions.

## Evaluation

`eval/cases.jsonl` has 28 hand-labelled cases: 9 dev and 19 held-out. It covers
incomplete requests, catalog overlap, vendor expiry/conflict, sensitive data,
threshold boundaries, injection in all business-data surfaces, outages, happy
paths, and insufficient budget. Labels come from policy/data review, not model
output.

For each architecture, the report includes recommendation/action match,
grounded evidence, deterministic-policy/approval compliance, escalation
precision/recall/F1, human-review correctness, latency, call counts, per-tag
breakdowns, and failures. Prompts may be refined on `dev` only.

**Published comparison** (28 cases, both architectures, model
`gemini/gemini-3.5-flash-lite`, results file
`eval/results/20261010T123631Z.json`, live API run with cache reads bypassed so
the latency figures are real): Architecture A matched the hand labels on 92.9%
of recommendation/next-action pairs vs B 89.3%; deterministic-policy compliance
85.7% vs 85.7%; exact approval match 85.7% vs 85.7%; grounded evidence 100.0%
vs 100.0%; severity match 96.4% vs 89.3%; escalation precision/recall 94.4% /
100.0% vs 85.0% / 100.0%; median latency 20,424 ms vs 25,435 ms; mean LLM calls
per case 4.25 vs 5.25. See `docs/memo.md` for the ship decision and its
reasoning.

`LLM_MODEL` must stay equal to `gemini-3.5-flash-lite`, the model the committed
cache was written under; a different model changes the cache key, misses every
entry, and the comparison above will not reproduce.

## Assumptions and limitations

* Reference date is 2026-09-30; reviews are current for 365 days.
* `available_usd` is authoritative; purchase history is incomplete and not a
  budget ledger.
* Catalog matches are candidates, not automatic duplicate/reject decisions.
* An unknown department-head identity is surfaced rather than guessed.
* Live vendor-risk evidence takes precedence over the static registry; conflict
  requires human/security review.
* Provider quality, latency, availability, and cost vary. Safe fallback is
  deliberately conservative and is not a provider benchmark.

## Repository map

```text
app/       analyze(), FastAPI bridge, CLI, legacy Streamlit UI
frontend/  React UI (Vite) served by the FastAPI bridge
agents/    Architecture A/B loops and prompts
core/      schema, data, findings, guardrail, LLM client
tools/     shared deterministic/evidence tools
eval/      cases, runner, metrics, cache, results
docs/      architecture and inspection record
tests/     no-live-LLM test coverage
```

Starter compatibility remains available through `src/solution.py:handle_request`.

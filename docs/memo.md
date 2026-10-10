# Architecture decision memo

**Decision: ship Architecture A** — the single-agent baseline.

**The evidence.** Both architectures ran the same 28 hand-labelled procurement
cases (9 dev for tuning, 19 held out) with identical tools, decision schema,
guardrail and LLM client; only the agent stage differs. Model
`gemini/gemini-3.5-flash-lite`, temperature 0. Cached responses still count as
calls; latency is from a run with cache reads bypassed, so it is wall-clock
real.

| Metric (all 28 cases) | A | B |
|---|---|---|
| Recommendation / next action match | 92.9% | 89.3% |
| Policy + deterministic rules followed | 85.7% | 85.7% |
| Evidence grounded in tool results | 100.0% | 100.0% |
| Severity match | 96.4% | 89.3% |
| Escalation precision / recall | 94.4% / 100.0% | 85.0% / 100.0% |
| Latency p50 | 20,424 ms | 25,435 ms |
| Mean LLM calls per case | 4.25 | 5.25 |

Per-tag breakdowns and failure lists: `eval/results/20261010T123631Z.json`.

**Why A.** A matches or beats B on label agreement and policy compliance while
using 19% fewer LLM calls and 20% less median latency. The simpler system does
as well or better, so the second agent is unnecessary orchestration.

**What B buys.** The reviewer raised severity or added flags in 27 of 28 cases
and never loosened one (code enforces max-severity and unions, so the prompt is
not trusted for safety). That scrutiny did not improve decisions against the
labels: the shared deterministic layer already owns thresholds, budget
arithmetic, approval routing and escalation triggers.

**Why the comparison is trustworthy.** Code, never the model, computes budgets
and policy gates. The guardrail checks every evidence quote against the actual
tool envelope, recomputes approvals, takes the maximum severity, and forces a
safe path on missing, conflicting, expired or unavailable evidence. Injection
in request, catalog or vendor text is fenced and flagged, never obeyed. The
outage scenario is itself evaluated: on a 503 the failed tool's evidence was
dropped and the request escalated rather than guessed; on a malformed decision
the system fell back to human review rather than inventing a result.

**Limitations.** Free-text requests without a stated cost route to
`request_more_info` rather than guessing numbers. Output is not bit-stable at
temperature 0: a repeat live run held A's totals exactly but cost B two cases —
one to that fallback, one where the analyst skipped an optional tool and the
deterministic layer escalated on the missing evidence. Both are held-out and
were not retuned. The committed cache reproduces these numbers from a clean
checkout with no API key. During dev-split tuning three labels were corrected
where they contradicted the data; held-out labels were verified against data
facts only and not tuned.

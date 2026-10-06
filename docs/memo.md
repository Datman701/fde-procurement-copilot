# Architecture decision memo

**Decision: ship Architecture A** — the single-agent baseline.

**The evidence.** Both architectures were run over the same 28 hand-labelled
procurement cases (9 dev for tuning, 19 held out, run once per architecture)
with identical tools, decision schema, guardrail and LLM client; only the agent
stage differs. Model: `{{MODEL}}` at temperature 0; cached responses still count
as calls, so the numbers mean the same thing with or without a warm cache.

| Metric (all 28 cases) | A | B |
|---|---|---|
| Recommendation / next action match | {{A_REC}} | {{B_REC}} |
| Policy + deterministic rules followed | {{A_POL}} | {{B_POL}} |
| Evidence grounded in tool results | {{A_GRD}} | {{B_GRD}} |
| Escalation precision / recall | {{A_ESC}} | {{B_ESC}} |
| Latency p50 | {{A_P50}} | {{B_P50}} |
| Mean LLM calls per case | {{A_CALLS}} | {{B_CALLS}} |

Full per-tag breakdowns and failure lists are in
`eval/results/{{RESULTS_FILE}}`.

**Why A.** A matches or beats B on label agreement and deterministic-policy
compliance while using roughly half the LLM calls and latency. The simpler
system performs as well or better, so the second agent is unnecessary
orchestration.

**What B buys.** The tighten-only reviewer raised severity or added flags in
{{B_TIGHTENED}} of 28 cases and never loosened one (code enforces max-severity
and unions, so the prompt is not trusted for safety). That extra scrutiny did
not convert into measurably better decisions against the hand labels, because
the shared deterministic layer — not the model — already owns thresholds,
budget arithmetic, approval routing and escalation triggers.

**Why the comparison is trustworthy.** Code, never the model, computes budgets
and policy gates. The guardrail verifies every evidence quote against the
actual tool envelope, recomputes deterministic approvals, takes the maximum
severity, and forces a safe path on missing, conflicting, expired or
unavailable evidence. Prompt injection in request, catalog or vendor text is
fenced and flagged, never obeyed. When the provider failed mid-evaluation,
every affected case degraded to an honest human-review fallback — nothing was
fabricated, and the outage path is itself an evaluated scenario.

**Limitations.** Free-text requests without a stated cost route to
`request_more_info` rather than guessing numbers. Provider quality varies and
the fallback is deliberately conservative, so these numbers measure one model
on one date; the committed response cache reproduces them from a clean
checkout with no API key. During dev-split tuning, three case labels were
corrected where they contradicted the underlying data (department budget rows
and a vendor cross-region record); held-out labels were then verified against
data facts only and were not tuned.

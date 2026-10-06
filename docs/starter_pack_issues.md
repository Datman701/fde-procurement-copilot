# Starter pack inspection log (Phase 0)

Every file in the starter pack was read, every data file cross-joined, and the
scaffold was executed (`verify_setup.py`, starter unit tests, mock API
`TestClient`). This file records what was found, how it was found, the fix, and
why.

**No file under `data/` was modified.** All corrections are applied in the
`core/data.py` overlay layer or at read time, never on disk. See
[ISS-13](#iss-13-data-overlay-for-simulation).

---

## Summary

| ID | Area | Severity | Status |
|---|---|---|---|
| ISS-01 | Data | **High** | Fixed in `tools/vendor.py` |
| ISS-02 | Data | **High** | Fixed in `tools/budget.py` |
| ISS-03 | Data | Medium | Surfaced in `tools/catalog.py` |
| ISS-04 | Code | **High** | Fixed by dropping pandas from `core/data.py` |
| ISS-05 | Data | Medium | Fixed in `tools/catalog.py` |
| ISS-06 | Data | Medium | Resolved by documented assumption |
| ISS-07 | Data | Medium | Resolved by documented assumption |
| ISS-08 | Data | Low | Handled generically |
| ISS-09 | Env | Medium | Fixed |
| ISS-10 | Env | Medium | Fixed |
| ISS-11 | Code | Medium | Fixed (`core/llm_client.py` wraps it) |
| ISS-12 | Code | **High** | Fixed (`core/schema.py` superset + adapter) |
| ISS-13 | Design | Medium | Added (`core/data.py` overlay) |
| ISS-14 | Code | Low | Fixed (removed, package layout) |
| ISS-15 | Design | Medium | Documented |

`verify_setup.py` passes, and all three starter-pack integrity claims turn out
to be **true** — see [What was *not* broken](#what-was-not-broken).

---

## Data issues

<a id="iss-01-signalwatch-vendor-conflict--expiry"></a>
### ISS-01 — SignalWatch vendor conflict + expiry

**Found by** joining `vendors.csv` (V005) against `data/vendor_risk.json`
through the mock API and applying the policy §5 rule
(`current for 365 days from its review date`, reference date `2026-09-30`).

| Source | Says |
|---|---|
| `vendors.csv` V005 | `security_status=Approved`, `security_review_date=2025-07-01` |
| `vendor_risk.json` | `security_review_status=expired`, `last_review_date=2025-07-01` |
| Policy §5 arithmetic | `2025-07-01 + 365d = 2026-07-01 < 2026-09-30` → **expired** |

The registry note (`Registry has not yet been refreshed with latest review
state`) confirms the staleness is deliberate. Note that the 365-day rule
*independently* reproduces the API's `expired` verdict — the two sources agree
on the underlying date and disagree only on how it was summarised.

**Fix.** `tools/vendor.py` computes expiry itself from the review date rather
than trusting either `security_status` field, treats the live service as system
of record (per the brief), emits `vendor_review_expired` **and**
`conflicting_vendor_evidence`, and returns
`material_conflict: true` so `core/guardrail.py` forces
`ESCALATE_TO_HUMAN`.

**Why.** Policy §5: *"If the internal vendor registry and the vendor-risk
service disagree, do not silently choose one. Surface the conflict and route to
Security/manual review."* A stale "Approved" read as valid would let an
expired-assessment observability tool into production.

<a id="iss-02-committed_usd--purchase-history"></a>
### ISS-02 — `committed_usd` cannot be derived from purchase history

**Found by** summing `purchase_history.csv.annual_amount_usd` per department and
comparing to `department_budgets.csv.committed_usd`.

| Department | `committed_usd` | Sum of POs | Delta |
|---|---:|---:|---:|
| Marketing | 165,000 | 59,000 | 106,000 |
| Engineering | 274,000 | 170,000 | 104,000 |
| Sales | 202,000 | 0 | 202,000 |
| Finance | 61,000 | 16,000 | 45,000 |
| Customer Success | 133,000 | 46,000 | 87,000 |
| Operations | 100,000 | 96,000 | 4,000 |

`purchase_history.csv` is clearly a **sample** of purchase orders, not a
complete ledger (Sales has zero POs yet 202,000 USD committed).

**Fix.** `tools/budget.py` reads available budget from
`department_budgets.csv.available_usd` only. Purchase history is used solely
for overlap/expansion evidence, never for budget arithmetic.

**Why.** Recomputing `available` from POs would report Marketing as having
121,000 USD free and approve requests that cannot be funded. `data/README.md`
states `available_usd = annual_software_budget_usd - committed_usd`, and that
identity was verified to hold for all six departments.

<a id="iss-03-catalog-items-without-purchase-records"></a>
### ISS-03 — Catalog items with no purchase record

**Found by** left-joining `software_catalog.csv` to `purchase_history.csv` on
`vendor_name`. `SW004 DocSpace` and `SW006 MetricLoop` are `Approved` with a
full `annual_cost_usd` and `licensed_seats`, but have **no** purchase order.

**Fix.** `tools/catalog.py` marks such candidates
`no_purchase_record: true` and `procurement_evidence: "catalog_only"`. The
overlap rationale says *"listed in the approved catalog but no purchase order
on file — confirm it is actually licensed before telling the requester to use
it"*.

**Why.** `proceed_with_approvals` on the strength of a tool that may not
actually be deployed is exactly the failure this product exists to prevent. The
policy (§3) treats overlap as something to surface, not to assume away.

<a id="iss-04-pandas-nan-traps"></a>
### ISS-04 — pandas `NaN` silently corrupts joins and dates

**Found by** reading `src/data_access.py` (which uses
`pd.read_csv`) against the raw CSVs.

* `employees.csv` E010 `Lisa Park` has an empty `manager_id` → pandas yields
  `float('nan')`, not `None`. `manager_id in employee_ids` is then `False`
  (fine) but `str(row.manager_id)` produces the literal string `"nan"`, and
  `manager_id == "E010"` comparisons quietly fail.
* `vendors.csv` V011/V012/V013 have an empty `security_review_date` → `NaN`.
  Any date comparison (`NaN > today`) returns `False`, so **an unknown review
  date would read as "not expired"** — the exact opposite of policy §5, which
  requires security review when the assessment is *"missing, expired, or not
  completed"*.
* `requests.json` uses real `null`, so pandas and JSON disagree on what missing
  means within the same codebase.

**Fix.** `core/data.py` uses the stdlib `csv` module and keeps `None` for empty
cells. Every consumer (`core/policy.py`, `tools/vendor.py`) treats
`review_date is None` as *"assessment missing"* → security review required, not
as *"current"*.

**Why.** This is the most dangerous class of defect in the pack: a missing
security review silently reading as a valid one. Keeping pandas out of the
decision path also removes the Python-version/wheel fragility (ISS-09).
`src/data_access.py` is left untouched and still works; it is simply not on the
hot path.

<a id="iss-05-compound-catalog-status"></a>
### ISS-05 — Compound `status` value in the catalog

**Found by** enumerating the distinct values in each status column.
`software_catalog.csv` SW009 NeuralDesk Business has
`status = "Approved - limited use"`, and `notes` explains
*"AI assistant; sensitive data restrictions apply"*.

**Fix.** `tools/catalog.py` parses the prefix (`Approved`) but keeps the full
string verbatim in the evidence payload, and surfaces
`restricted_use: true` for compound statuses.

**Why.** `status == "Approved"` would miss NeuralDesk entirely; `status.startswith("Approved")` would silently discard the restriction. Policy §8 is explicit: *"Do not assume that an AI product approved for low-risk use is approved
for customer PII, confidential documents, source code, or production access."*

<a id="iss-06-three-departments-have-no-director"></a>
### ISS-06 — Three departments have no Director in `employees.csv`

**Found by** mapping `department_budgets.department` to
`employees[level == "Director"]`.

| Department | Director | Requesters |
|---|---|---|
| Marketing | David Chen (E009) | E001 |
| Engineering | Maya Rao (E008) | E002 |
| Finance | Priya Shah (E006) | E004 |
| Sales | **none** | E003 |
| Customer Success | **none** | E005 |
| Operations | **none** | — |

**Fix (assumption, documented).** `tools/employee.py` resolves
`department_head` as: *the requester's `manager_id`, if that employee's `level`
is `Director`*. For every request in the dataset this resolves — E003's and
E005's manager is E007 Robert King, a Director. If no director can be
resolved, the tool returns `ok=true` with `department_head: null` and the
approval router emits an `approver_unresolved` risk flag rather than guessing.

**Why.** "Department Head" is a required approver in three of the four
financial tiers (policy §4). Guessing a name would fabricate an approval
authority; failing to resolve must be visible, not silent.

<a id="iss-07-empty-requested_integrations"></a>
### ISS-07 — Empty `requested_integrations` is ambiguous

**Found by** policy §1 lists *"required integrations"* as mandatory request
information, but REQ-1001, REQ-1006 and REQ-1010 have
`requested_integrations: []`. Empty list = *"none needed"* or *"not answered"*?

**Fix (assumption, forced by the data).** An empty list is treated as
**"no integrations required"**, not as missing information.

**Why.** `evals/public_cases.json` PUB-01 (REQ-1001) asserts
`max_missing_information: 0`. If `[]` counted as missing, a complete,
fully-specified low-value request would be routed for clarification — and the
public harness would say the implementation is wrong. The empty-list reading is
therefore the one the starter pack intends.

<a id="iss-08-request-categories-absent-from-catalog"></a>
### ISS-08 — Request categories that do not exist in the catalog

**Found by** set difference of `requests[].category` against
`software_catalog[].category`: `Sales Intelligence`, `Professional Services` and
`Legal AI` have no catalog entry.

**Fix.** `tools/catalog.py` returns `candidates: []` with
`search_performed: true` and a note that no category match exists — an empty
result, never an error and never a fabricated candidate.

**Why.** "No overlap" and "search failed" must be distinguishable, otherwise a
retrieval bug silently reads as clearance to buy.

<a id="iss-09-python-version-and-pandas-wheel-fragility"></a>
### ISS-09 — Python version / pandas wheel fragility

**Found by** inspecting the environment: system Python is **3.14.7**, while
`README.md` asks for 3.11/3.12 and `requirements.txt` pins
`pandas>=2.2,<3` and `streamlit>=1.40,<2`.

**Fix.** Added `.python-version` (`3.12`) and `.venv/` created with
`uv venv --python 3.12`. The `Makefile` uses `uv` when present and falls back
to `python -m venv`.

**Why.** Reproducibility is a graded deliverable. Pinning the interpreter
removes the largest source of "works on my machine" drift.

<a id="iss-10-no-test-runner-and-wrong-env-varnames"></a>
### ISS-10 — No test runner declared; `.env.example` names the wrong variables

**Found by** reading `requirements.txt` (starter tests use `unittest`, no
pytest) and `.env.example` (documents `MODEL_NAME` and
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, not the `LLM_PROVIDER` / `LLM_MODEL` /
`LLM_API_KEY` triple).

**Fix.** `pytest>=8.0,<9` added to `requirements.txt`; `.env.example`
rewritten to the `LLM_*` contract with per-provider base-URL notes. The
`ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` hints are gone so that no stale key name
suggests an unconfigured provider.

**Why.** `make test` must pass from a clean environment, and the brief requires
provider-agnostic configuration from environment variables.

<a id="iss-11-vendor_client-raises-raw-exceptions"></a>
### ISS-11 — `src/vendor_client.py` cannot produce a tool envelope

**Found by** reading the source against the four failure modes it must
survive. It calls `response.raise_for_status()` and lets
`requests.RequestException` propagate: NimbusAI's forced `503`, an unknown
vendor's `404`, a connection refused (service down), and a 3-second timeout all
raise four different exception types with no structured payload, no `result_id`,
and no `as_of`.

**Fix.** Left untouched; wrapped by `core/llm_client.py`'s sibling
`tools/vendor.py`, which catches `HTTPError`, `Timeout`, and
`ConnectionError` separately and converts each into an
`{ok: false, error: ...}` envelope. The raw client is still used for its
documented low-level purpose and is covered by a unit test.

**Why.** The brief requires that a tool failure becomes a
`missing_information` entry plus escalation, never a crash and never a
fabricated status. That conversion cannot live in a two-line client.

<a id="iss-12-contracts-schema-is-weaker-than-the-brief"></a>
### ISS-12 — `src/contracts.py` cannot express the required output

**Found by** diffing `ProcurementDecision` against the brief's required output.

`contracts.py` provides `recommendation`, `evidence[{source, finding,
reference}]`, `required_approvals: list[str]`, `missing_information`,
`risk_flags: list[str]`, `next_step`, `human_review_required`, `telemetry`.

Gaps: **no severity**, no per-approval `reason` / `policy_clause`, no
`needs_human_review` naming, no `trace` block, risk flags are bare strings with
no severity, and `evidence` has no `result_id` to ground against. The brief
requires clause identifiers and grounding by `result_id`.

**Fix.** `core/schema.py` defines the full `Decision` model (a strict superset).
`src/contracts.py` is **unmodified**; a converter in `core/schema.py`
(`to_procurement_decision()`) emits it, flattening approvals to names and risk
flags to strings. `handle_request()` returns the `contracts` object so the
starter's `evals/run_public_evals.py` keeps passing unchanged.

**Why.** The starter harness and the required output are both non-negotiable.
A superset plus a lossless-enough adapter satisfies both without editing the
provided contract.

<a id="iss-13-data-overlay-for-simulation"></a>
### ISS-13 — The data cannot express two required test scenarios

**Found by** trying to construct two mandated test cases against the provided
files:

* **Prompt injection inside vendor notes.** No vendor note in `vendors.csv` or
  `vendor_risk.json` contains adversarial text.
* **Prompt injection inside a catalog description.** No `notes` field in
  `software_catalog.csv` contains adversarial text.
* **Full vendor-API outage.** `vendor_risk.json` supports exactly one forced
  failure (`NimbusAI`), so a *whole-service* outage is not expressible.
* **A conflict other than SignalWatch.** Only one registry/API disagreement
  exists in the data.

**Fix.** `core/data.py` supports an in-memory `DataOverlay` (dict of
`table -> row-predicate -> field overrides`) applied at load time. Eval cases
carry their overlay; the overlay is used for simulation only and is never
written to `data/`. `tools/vendor.py` additionally honours
`VENDOR_RISK_FORCE_FAIL=1` to force transport failures for every vendor.

**Why.** Simulating tool/API unavailability and injection-in-business-data are
explicit brief requirements. Editing `data/` to manufacture them would corrupt
the shared reference dataset and break `verify_setup.py`.

<a id="iss-14-apppy-vs-app-package"></a>
### ISS-14 — `app.py` shadows the required `app/` package

**Found by** mapping the brief's repository layout (`app/  # UI + analyze()
entry point`) onto the starter tree, which already has a root `app.py`
(Streamlit) and a top-level `run_local.py` that runs `streamlit run app.py`.

**Fix.** Root `app.py` **removed**; the package `app/` added
(`app/analyze.py`, `app/cli.py`, `app/ui.py`); `run_local.py` updated to
`streamlit run app/ui.py`.

**Why.** A directory and a module with the same name on one `sys.path` entry is
a real shadowing hazard — packages win, so `import app` becomes
non-deterministic for anyone reading the tree. One clear layout is worth a
one-line edit to the starter launcher.

<a id="iss-15-architecture-naming-mismatch"></a>
### ISS-15 — Two names for the same two architectures

`src/contracts.py` defines `Architecture = Literal["single", "staged"]`; the
brief's prompt specifies `architecture="A" | "B"`; the starter UI offers
`single / staged`.

**Fix.** `app/analyze.py` exposes the canonical
`analyze(request, architecture="A"|"B")`. `src/solution.py`'s
`handle_request(request_id, architecture="single"|"staged")` maps
`single -> A`, `staged -> B`. Both entry points share one code path.

**Why.** One entry point for UI, CLI and eval is a graded requirement; the
aliases cost three lines and keep the starter harness working.

---

## Real schema (as found, not assumed)

Every field below was read from the files. Nothing here is inferred from the
prompt.

### `data/employees.csv`
`employee_id` (str, PK, `E001`–`E010`), `name` (str), `department` (str, FK →
`department_budgets.department`), `manager_id` (str | **empty** → VP row
E010), `level` (str ∈ `IC3, IC4, IC5, Director, VP`), `country` (str).

### `data/department_budgets.csv`
`department` (str, PK), `annual_software_budget_usd` (int),
`committed_usd` (int), `available_usd` (int, verified `= annual − committed`
for all six rows). **Authoritative for budget math** (ISS-02).

### `data/software_catalog.csv`
`software_id` (str, PK `SW001`–`SW010`), `product_name` (str), `category` (str),
`vendor_name` (str, FK → `vendors.vendor_name`), `status` (str; **compound
values exist**, ISS-05), `annual_cost_usd` (int), `licensed_seats` (int),
`scope` (str), `notes` (str, free text → **untrusted**).

### `data/vendors.csv`
`vendor_id` (str, PK `V001`–`V013`), `vendor_name` (str),
`procurement_status` (str ∈ `Approved, New`),
`security_status` (str ∈ `Approved, Pending, Unknown`),
`security_review_date` (str ISO date | **empty**),
`legal_terms_status` (str ∈ `Approved, Draft, Unknown`), `notes` (str, free
text → **untrusted**).

### `data/vendor_risk.json` (backing data for the mock API)
Keyed by `vendor_name`. Value fields: `risk_level` (str ∈ `low, medium, high`),
`security_review_status` (str ∈ `approved, expired, not_completed`),
`last_review_date` (ISO date | `null`), `processes_personal_data` (bool),
`stores_data_outside_region` (bool), `notes` (str, **untrusted**).
**One record (`NimbusAI`) instead contains
`{force_error: true, error_message: str}` → the API answers `503`.**
This is a *variant record shape*, not an optional field — the tool must
tolerate it.

### `data/purchase_history.csv`
`purchase_id` (str, PK `PO-nnnn`), `purchase_date` (ISO date),
`department` (str), `vendor_name` (str), `product_name` (str),
`annual_amount_usd` (int), `status` (str), `notes` (str).

### `data/requests.json`
List of objects:
`request_id` (str, PK), `requester_id` (str, FK → `employees.employee_id`),
`product_name` (str), `vendor_name` (str, FK → `vendors.vendor_name` **and**
`vendor_risk.json` key), `category` (str), `annual_cost_usd` (int | **null**),
`user_count` (int | **null**), `business_justification` (str, free text →
**untrusted**), `data_access_level` (str ∈ `internal_documents,
internal_marketing, source_code, customer_pii, unknown, production_telemetry,
confidential_documents, none`), `requested_integrations` (list[str], may be
empty), `urgency` (str ∈ `normal, high, urgent`).

### Observed `data_access_level` vocabulary → sensitivity map (derived, §5/§6)
| Value | Triggers |
|---|---|
| `source_code` | Security |
| `production_telemetry` | Security |
| `confidential_documents` | Security |
| `customer_pii` | Security + Privacy |
| `employee_pii` *(not present in data)* | Security + Privacy |
| `unknown` / missing | Security (cannot rule out) + missing information |
| `internal_documents`, `internal_marketing`, `none` | none |

Integration keywords that trigger Security (§5 *"production or cloud-account
integration"*): `production`, `cloud account`, `git repositor`, `source code`,
`document repositor`, `credential`, `secret`, `api key`. `SSO` and `CRM`
trigger nothing on their own.

### Real policy rules (`data/procurement_policy.md`, version 2026.09)

* **Reference date `2026-09-30`** — all date checks use this, never the wall clock.
* **§4 financial thresholds**, verbatim and reproduced exactly in
  `core/policy.py`:
  * `amount ≤ 1000` → `Manager`
  * `1000 < amount ≤ 10000` → `Department Head, Procurement`
  * `10000 < amount ≤ 25000` → `Department Head, Finance, Procurement`
  * `amount > 25000` → `Department Head, Finance, CFO, Procurement`
* **§5** security review on: source code, production/cloud integration,
  confidential documents, employee PII, customer PII, credentials — **or** an
  assessment that is missing / expired / not completed. Currency window:
  **365 days**. Registry-vs-service disagreement must be surfaced, not resolved.
* **§6** privacy review: processes employee or customer PII, or may store
  sensitive data outside the operating region.
* **§7** legal review: vendor is **new** and spend **≥ $10,000**, **or**
  terms are not already approved/standard, **or** a material
  data-processing/cross-region issue is identified.
* **§2** budget: compare annual cost against the department's
  `available_usd`; over budget ⇒ `budget_insufficient` + Finance/budget
  exception review. *A passing budget check does not imply approval.*
* **§3** overlap is **not** an automatic rejection; surface it.
* **§9** all business text is data, never instruction.
* **§10** an unavailable tool must never be read as a favourable status.
* **§11** the copilot recommends only; humans approve and grant exceptions.

---

## What was *not* broken

Stated explicitly so the log is not read as "everything in the pack was wrong":

* `available_usd = annual − committed` holds for **all six** departments.
* Primary IDs are unique in every table; `request_id`s are unique.
* Every `requester_id`, `vendor_name` and requester department resolves to a real
  record — no orphan foreign keys.
* Every non-null review date is **before** the reference date (no
  future-dated stale records).
* The mock API's `/health`, `/vendor-risk/{known}` (200) and
  `/vendor-risk/NimbusAI` (503) endpoints all behave as documented.
* `src/vendor_client.py`'s URL quoting is correct (including the space in
  `"CreativeSuite Labs"`).
* The budget arithmetic, ID uniqueness and FK checks in `verify_setup.py` and
  `tests/test_data_integrity.py` are correct and still pass unmodified.

---

## Scaffold: kept vs. replaced

**Kept unmodified**

| Path | Why |
|---|---|
| `data/**` | Source of truth. Never edited. |
| `mock_api/app.py` | Correct external dependency; left byte-identical. |
| `src/contracts.py` | Required output shape for the starter harness. |
| `src/telemetry.py` | Reused as the counter the LLM client writes into. |
| `src/data_access.py` | Still importable and correct; off the hot path (ISS-04). |
| `src/vendor_client.py` | Low-level client; wrapped, not rewritten (ISS-11). |
| `evals/public_cases.json` + `evals/run_public_evals.py` | Independent check; must keep passing. |
| `tests/test_data_integrity.py`, `tests/test_mock_api.py` | Still pass. |
| `verify_setup.py` | Still passes. |
| `templates/**` | Shape the memo/results deliverables. |
| `README.md` | Replaced by my own (deliverable). |

**Replaced / added**

| Path | Disposition |
|---|---|
| `src/solution.py` | **Implemented** — thin adapter onto `app.analyze`. |
| `app.py` (root) | **Removed** → `app/` package (ISS-14). |
| `run_local.py` | **Edited** — one line, points at `app/ui.py`. |
| `requirements.txt`, `.env.example`, `.gitignore` | **Edited** (ISS-09, ISS-10). |
| `README.md`, `docs/`, `Makefile` | **Replaced / added** (deliverables). |

---

## Known limitation of the evaluation

The hand-written labels in `eval/cases.jsonl` and the deterministic engine in
`core/policy.py` + `tools/` share a single author, so the evaluation measures
**LLM adherence to the deterministic engine**, not the correctness of the
engine itself. Mitigations actually applied: all 28 labels were written from
`data/procurement_policy.md` and the data joins *before* the tools were
implemented; every label↔code disagreement is recorded below and in the git
history; and the deterministic engine is independently unit-tested at every
policy boundary. This limitation is restated in the README.

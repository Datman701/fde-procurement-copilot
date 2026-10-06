"""Streamlit UI for the procurement copilot.

Three areas, per the brief:

1. **Request details** - request picker plus free-text entry, architecture toggle.
2. **Evidence panel** - each item with its source tool and result_id, tool
   failures and conflicts visibly marked, raw tool results expandable.
3. **Recommendation and action** - recommendation, approvals, missing
   information, risk flags, next step, and simulated handoff buttons. Escalated
   cases are unmistakable.

Run with ``make run`` (which also starts the vendor-risk API on :8001).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.analyze import analyze  # noqa: E402
from core.data import DataStore  # noqa: E402

st.set_page_config(page_title="AI Procurement Request Copilot", layout="wide")

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "decision" not in st.session_state:
    st.session_state.decision = None
if "tool_results" not in st.session_state:
    st.session_state.tool_results = []
if "handoffs" not in st.session_state:
    st.session_state.handoffs = []


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("AI Procurement Request Copilot")
st.caption(
    "Evidence-gathering and recommendation only. The copilot cannot approve spend, "
    "purchase software, change a budget, or override Security / Privacy / Legal."
)

store = DataStore.load()
if not store.overlay_summary():
    with st.sidebar:
        st.markdown("**Reference date:** 2026-09-30 (policy 2026.09)")

# --------------------------------------------------------------------------- #
# Sidebar: request picker + architecture toggle
# --------------------------------------------------------------------------- #
st.sidebar.header("Request details")
mode = st.sidebar.radio("Source", ["Pick a request", "Write a free-text request"])

if mode == "Pick a request":
    request_ids = [r.request_id for r in store.requests]
    labels = {
        r.request_id: f"{r.request_id} — {r.product_name} ({r.vendor_name})"
        for r in store.requests
    }
    chosen_id = st.sidebar.selectbox(
        "Request", request_ids, format_func=lambda rid: labels[rid]
    )
    target = store.request(chosen_id)
    requester_id = None
else:
    requester_options = ["(not specified)"] + [f"{e.employee_id} — {e.name}" for e in store.employees]
    requester_pick = st.sidebar.selectbox("Requester (optional)", requester_options)
    requester_id = None if requester_pick.startswith("(") else requester_pick.split(" ")[0]
    free_text = st.sidebar.text_area(
        "Request in your own words",
        placeholder="e.g. Marketing needs a design tool for the new brand refresh, about $5k a year.",
        height=120,
    )
    target = free_text or None

architecture = st.sidebar.radio(
    "Architecture",
    ["A", "B"],
    horizontal=True,
    format_func=lambda a: "A — single agent" if a == "A" else "B — analyst + reviewer",
)
st.sidebar.caption(
    "Both architectures share the same tool layer, guardrail, output schema and LLM "
    "client. Only the agent stage differs."
)

use_cache = st.sidebar.checkbox("Use LLM response cache", value=True)
simulate_outage = st.sidebar.checkbox("Simulate vendor-risk service outage", value=False)

run_clicked = st.sidebar.button(
    "Run analysis", type="primary", use_container_width=True, disabled=(target is None)
)

if run_clicked:
    with st.spinner("Gathering evidence and analysing..."):
        overlay = {"vendor_risk_force_fail": True} if simulate_outage else None
        run_store = DataStore.load(overlay)
        if mode == "Pick a request":
            parsed = run_store.request(chosen_id)
        else:
            parsed = free_text
        try:
            actual_results: list = []
            decision = analyze(
                parsed,
                architecture=architecture,
                overlay=overlay,
                use_cache=use_cache,
                store=run_store,
                requester=requester_id,
                tool_result_sink=actual_results.extend,
            )
            st.session_state.decision = decision
            st.session_state.tool_results = actual_results
            st.session_state.handoffs = []
        except Exception as exc:  # the UI must never crash
            st.error(f"{type(exc).__name__}: {exc}")
            st.session_state.decision = None

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
decision = st.session_state.decision

if decision is None:
    st.info(
        "Pick a request or write one, choose architecture A or B, then press "
        "**Run analysis**."
    )
    st.stop()

results = st.session_state.tool_results
failures = [r for r in results if not r.ok]
conflicts = [r for r in results if r.ok and r.data.get("material_conflict")]

# ---- 3. Recommendation and action ---------------------------------------- #
left, right = st.columns([1.05, 0.95], gap="large")

with left:
    st.subheader("1 · Request")
    if mode == "Pick a request":
        st.json(target.to_dict())
    else:
        st.text(f"**Free-text request** from {requester_id or 'an unspecified requester'}")
        st.text(f"> {free_text}")

with right:
    st.subheader("2 · Recommendation")

    if decision.severity == "ESCALATE_TO_HUMAN":
        st.error(
            "**ESCALATE TO HUMAN** — a human must review this before anything is bought "
            "or approved. The copilot cannot proceed.",
            icon="🚨",
        )
    elif decision.severity == "NEEDS_APPROVAL":
        st.warning("**NEEDS APPROVAL** — required approvers and reviews must complete.", icon="⚠️")
    else:
        st.success("**PROCEED** — within policy; the line manager still approves.", icon="✅")

    st.markdown(f"### `{decision.recommendation}`")
    st.markdown(f"**Next step:** {decision.next_step}")

    st.markdown("#### Approvals required")
    if decision.approvals_required:
        for approval in decision.approvals_required:
            with st.expander(f"**{approval.approver}**  ·  `{approval.policy_clause}`"):
                st.write(approval.reason)
    else:
        st.caption("No approvers were required by any deterministic rule.")

    st.markdown("#### Missing information")
    if decision.missing_information:
        for item in decision.missing_information:
            st.markdown(f"- {item}")
    else:
        st.caption("Nothing outstanding.")

    st.markdown("#### Risk flags")
    if decision.risk_flags:
        for flag in decision.risk_flags:
            icon = {"high": "🔴", "warning": "🟠", "info": "🔵"}.get(flag.severity, "⚪")
            with st.expander(f"{icon} `{flag.flag}` ({flag.severity})"):
                if flag.detail:
                    st.write(flag.detail)
                if flag.source_result_id:
                    st.caption(f"source result: `{flag.source_result_id}`")
    else:
        st.caption("No risk flags.")

# ---- 2. Evidence panel ---------------------------------------------------- #
st.divider()
st.subheader("3 · Evidence")

if failures:
    st.error(
        f"**{len(failures)} tool call(s) failed.** A missing check is never a "
        "favourable status (policy section 10).",
        icon="🚨",
    )
if conflicts:
    st.error(
        "**Conflicting vendor evidence detected.** The internal registry and the "
        "vendor-risk service disagree; policy section 5 requires this to be surfaced, "
        "not resolved automatically.",
        icon="⚠️",
    )

if decision.evidence:
    st.markdown("**Grounded evidence** — every item cites a tool result:")
    for item in decision.evidence:
        st.markdown(f"- **{item.claim}**")
        st.caption(f"`{item.result_id}` → `{item.quote_or_value}`")
else:
    st.warning("No evidence survived the grounding check.")

st.markdown("**All tool results**")
for result in results:
    header = f"`{result.result_id}` · {result.tool}"
    if not result.ok:
        header += "  ·  ❌ FAILED"
    elif result.data.get("material_conflict"):
        header += "  ·  ⚠️ CONFLICT"
    elif result.data.get("review_expired") or result.data.get("security_verdict") == "expired":
        header += "  ·  ⏰ EXPIRED"
    with st.expander(header):
        st.caption(f"source: `{result.source}` · as of: `{result.as_of}`")
        if not result.ok:
            st.error(result.error or "tool failed")
        else:
            st.json(result.data, expanded=False)

# ---- Actions --------------------------------------------------------------- #
st.divider()
st.subheader("4 · Action")

st.caption(
    "These buttons simulate the handoff. Nothing is purchased, approved, or sent in "
    "this assessment build."
)

escalated = decision.severity == "ESCALATE_TO_HUMAN"
approver_names = decision.approver_names()

c1, c2, c3 = st.columns(3)
primary_approver = approver_names[0] if approver_names else "the line manager"

with c1:
    if st.button(
        f"Send to approver: {primary_approver}",
        use_container_width=True,
        disabled=not approver_names or escalated,
    ):
        st.session_state.handoffs.append(("Send to approver", primary_approver))
        st.toast(f"Simulated handoff to {primary_approver}")

with c2:
    if st.button(
        "Request info from employee",
        use_container_width=True,
        disabled=not decision.missing_information,
    ):
        st.session_state.handoffs.append(
            ("Request info from employee", decision.missing_information[0])
        )
        st.toast("Simulated request for more information")

with c3:
    if st.button(
        "Escalate to security",
        use_container_width=True,
        type="primary" if escalated else "secondary",
        disabled=not escalated,
    ):
        st.session_state.handoffs.append(("Escalate to security", "Security"))
        st.toast("Simulated escalation to Security")

if st.session_state.handoffs:
    st.markdown("**Simulated handoffs this session**")
    for action, target in st.session_state.handoffs:
        st.markdown(f"- `{action}` → {target}")

# ---- Trace ----------------------------------------------------------------- #
st.divider()
st.subheader("5 · Trace")

trace = decision.trace
c1, c2, c3, c4 = st.columns(4)
c1.metric("Architecture", trace.architecture)
c2.metric("LLM calls", trace.llm_calls)
c3.metric("Tool calls", len(trace.tool_result_ids))
c4.metric("Latency", f"{trace.latency_ms:.0f} ms")

with st.expander("Guardrail actions (code overriding the model)"):
    if trace.guardrail_notes:
        for note in trace.guardrail_notes:
            st.markdown(f"- {note}")
    else:
        st.caption("The guardrail changed nothing.")

with st.expander("Reviewer tightening (architecture B only)"):
    if trace.reviewer_tightened:
        for note in trace.reviewer_tightened:
            st.markdown(f"- {note}")
    else:
        st.caption(
            "Architecture A has no reviewer." if trace.architecture == "A" else "No changes."
        )

with st.expander("Raw decision JSON"):
    st.json(json.loads(json.dumps(decision.model_dump(), default=str)))

st.divider()
st.caption(
    "Policy version 2026.09, reference date 2026-09-30. Recommendations are advisory; "
    "a human remains responsible for every approval and exception."
)

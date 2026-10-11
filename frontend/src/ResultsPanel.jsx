// The right-hand results column. Pure presentation: App owns all state and
// passes it down, so this file has no effects and no fetching.
import {
  VerdictBanner,
  ExtractionPanel,
  ApprovalCard,
  FlagItem,
  EvidenceCard,
  ToolResultsList,
  RunStats,
} from "./components.jsx";

function toolNameOf(resultId, toolResults) {
  const match = toolResults.find((r) => r.result_id === resultId);
  return match ? match.tool : resultId;
}

// A greyed-out control reads as broken unless the precondition is on screen.
function handoffBlockers(decision) {
  const blockers = [];
  if (
    decision.approvals_required.length === 0 ||
    decision.severity === "ESCALATE_TO_HUMAN"
  ) {
    blockers.push(
      "“Send to approver” is unavailable: " +
        (decision.severity === "ESCALATE_TO_HUMAN"
          ? "an escalated verdict must be reviewed by a human before anything is routed for approval."
          : "no deterministic rule required an approver on this request."),
    );
  }
  if (decision.missing_information.length === 0) {
    blockers.push(
      "“Request info from employee” is unavailable: nothing was recorded as missing.",
    );
  }
  if (decision.severity !== "ESCALATE_TO_HUMAN") {
    blockers.push(
      "“Escalate to security” is unavailable: escalation is rule-driven and this verdict is " +
        `${decision.severity.replaceAll("_", " ")}.`,
    );
  }
  return blockers;
}

export default function ResultsPanel({
  error,
  loading,
  decision,
  toolResults,
  extractions,
  employees = [],
  handoffs,
  onAddHandoff,
}) {
  const blockers = decision ? handoffBlockers(decision) : [];

  return (
    <main className="results" aria-live="polite" aria-busy={loading}>
      {error && <div className="banner error" role="alert">⚠️ {error}</div>}

      {loading && (
        <div className="panel empty-state">
          <div className="spinner" />
          <p>Gathering budget, catalog, vendor-risk and policy evidence…</p>
          <p className="muted small">This usually takes 10–40 seconds.</p>
        </div>
      )}

      {!loading && !decision && !error && (
        <div className="panel empty-state">
          <h2>No analysis yet</h2>
          <p>
            Choose a request on the left, pick architecture A or B, then press
            <strong> Run analysis</strong>. The copilot will gather evidence and
            recommend the next human action — it never approves or purchases anything.
          </p>
        </div>
      )}

      {decision && (
        <>
          <VerdictBanner severity={decision.severity} />

          <ExtractionPanel extractions={extractions} employees={employees} />

          <section className="panel">
            <h2>Recommended next step</h2>
            <p className="next-step">{decision.next_step}</p>
            <p>
              <code className="rec-chip">{decision.recommendation}</code>
            </p>
          </section>

          <section className="panel">
            <h2>Who needs to sign off</h2>
            {decision.approvals_required.length === 0 && (
              <p className="muted">No approvers were required by any deterministic rule.</p>
            )}
            <div className="approval-grid">
              {decision.approvals_required.map((a, i) => (
                <ApprovalCard key={i} approval={a} />
              ))}
            </div>
          </section>

          <section className="panel">
            <h2>Risks &amp; missing information</h2>
            {decision.missing_information.length === 0 &&
              decision.risk_flags.length === 0 && (
                <p className="muted">Nothing outstanding and no risk flags.</p>
              )}
            {decision.missing_information.length > 0 && (
              <>
                <h3>Missing information</h3>
                <ul className="plain-list">
                  {decision.missing_information.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </>
            )}
            {decision.risk_flags.length > 0 && (
              <>
                <h3>Risk flags</h3>
                <ul className="plain-list">
                  {decision.risk_flags.map((f, i) => (
                    <FlagItem key={i} flag={f} />
                  ))}
                </ul>
              </>
            )}
          </section>

          <section className="panel">
            <h2>What the evidence says</h2>
            {toolResults.some((r) => !r.ok) && (
              <div className="banner error">
                Some tool calls failed. A missing check is never a favourable status
                (policy section 10).
              </div>
            )}
            {toolResults.some((r) => r.ok && r.data?.material_conflict) && (
              <div className="banner error">
                Conflicting vendor evidence: the internal registry and the vendor-risk
                service disagree. Policy requires surfacing this, not resolving it
                automatically.
              </div>
            )}
            {decision.evidence.length === 0 ? (
              <p className="muted">No evidence survived the grounding check.</p>
            ) : (
              <div className="evidence-list">
                {decision.evidence.map((e, i) => (
                  <EvidenceCard
                    key={i}
                    item={e}
                    toolName={toolNameOf(e.result_id, toolResults)}
                  />
                ))}
              </div>
            )}
            <ToolResultsList results={toolResults} />
          </section>

          <section className="panel">
            <h2>Handoff (simulated)</h2>
            <p className="muted small">
              These buttons only record a simulated handoff. Nothing is purchased,
              approved, or sent in this assessment build.
            </p>
            <div className="handoff-row">
              <button
                type="button"
                disabled={
                  decision.approvals_required.length === 0 ||
                  decision.severity === "ESCALATE_TO_HUMAN"
                }
                onClick={() =>
                  onAddHandoff(
                    "Send to approver",
                    decision.approvals_required[0]?.approver,
                  )
                }
              >
                Send to approver: {decision.approvals_required[0]?.approver ?? "—"}
              </button>
              <button
                type="button"
                disabled={decision.missing_information.length === 0}
                onClick={() =>
                  onAddHandoff("Request info from employee", decision.missing_information[0])
                }
              >
                Request info from employee
              </button>
              <button
                type="button"
                className={decision.severity === "ESCALATE_TO_HUMAN" ? "danger" : ""}
                disabled={decision.severity !== "ESCALATE_TO_HUMAN"}
                onClick={() => onAddHandoff("Escalate to security", "Security")}
              >
                Escalate to security
              </button>
            </div>
            {blockers.length > 0 && (
              <ul className="plain-list handoff-reason">
                {blockers.map((b, i) => (
                  <li key={i}>{b}</li>
                ))}
              </ul>
            )}
            {handoffs.length > 0 && (
              <ul className="plain-list handoff-log">
                {handoffs.map((h, i) => (
                  <li key={i}>
                    <code>{h.action}</code> → {h.target}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <RunStats trace={decision.trace} decision={decision} />
        </>
      )}
    </main>
  );
}

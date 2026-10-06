// Presentational components for the copilot UI. All data arrives as plain,
// JSON-safe structures from app/api.py.

const SEVERITY_STYLES = {
  PROCEED: {
    cls: "proceed",
    icon: "✅",
    label: "Within policy — proceed",
    text: "No escalation rules were triggered. The line manager still approves the spend.",
  },
  NEEDS_APPROVAL: {
    cls: "needs-approval",
    icon: "⚠️",
    label: "Needs approval",
    text: "Required approvers and reviews must complete before anything is bought.",
  },
  ESCALATE_TO_HUMAN: {
    cls: "escalate",
    icon: "🚨",
    label: "Escalate to a human",
    text: "Something is missing, conflicting, or out of policy. A human must review before any purchase or approval.",
  },
};

export function VerdictBanner({ severity }) {
  const s = SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.NEEDS_APPROVAL;
  return (
    <div className={`verdict ${s.cls}`}>
      <span className="verdict-icon">{s.icon}</span>
      <div>
        <strong>{s.label}</strong>
        <p>{s.text}</p>
      </div>
    </div>
  );
}

export function ApprovalCard({ approval }) {
  return (
    <div className="approval-card">
      <div className="approval-head">
        <strong>{approval.approver}</strong>
        <code className="clause">{approval.policy_clause}</code>
      </div>
      <p>{approval.reason}</p>
    </div>
  );
}

const FLAG_ICONS = { high: "🔴", warning: "🟠", info: "🔵" };

export function FlagItem({ flag }) {
  return (
    <li className="flag">
      <span>{FLAG_ICONS[flag.severity] ?? "⚪"}</span>
      <div>
        <code>{flag.flag}</code>
        {flag.detail && <p>{flag.detail}</p>}
        {flag.source_result_id && (
          <p className="muted small">source: {flag.source_result_id}</p>
        )}
      </div>
    </li>
  );
}

export function EvidenceCard({ item, toolName }) {
  return (
    <div className="evidence-card">
      <p className="claim">{item.claim}</p>
      <p className="quote">
        <code>{item.quote_or_value}</code>
      </p>
      <div className="evidence-meta">
        <span className="tool-badge">{toolName}</span>
        <code className="muted small">{item.result_id}</code>
      </div>
    </div>
  );
}

function resultBadge(result) {
  if (!result.ok) return { cls: "failed", text: "FAILED" };
  if (result.data?.material_conflict) return { cls: "conflict", text: "CONFLICT" };
  if (result.data?.review_expired || result.data?.security_verdict === "expired")
    return { cls: "expired", text: "EXPIRED" };
  return null;
}

export function ToolResultsList({ results }) {
  if (results.length === 0) return null;
  return (
    <details className="tool-results">
      <summary>All tool results ({results.length})</summary>
      {results.map((r) => {
        const badge = resultBadge(r);
        return (
          <details key={r.result_id} className="tool-result">
            <summary>
              <code>{r.result_id}</code> · {r.tool}
              {badge && <span className={`badge ${badge.cls}`}>{badge.text}</span>}
            </summary>
            <p className="muted small">
              source: {r.source} · as of: {r.as_of}
            </p>
            {!r.ok ? (
              <p className="error-text">{r.error || "tool failed"}</p>
            ) : (
              <pre>{JSON.stringify(r.data, null, 2)}</pre>
            )}
          </details>
        );
      })}
    </details>
  );
}

export function RunStats({ trace, decision }) {
  return (
    <section className="panel run-details">
      <h2>Run details</h2>
      <div className="stats">
        <div className="stat">
          <span className="stat-value">{trace.architecture}</span>
          <span className="stat-label">Architecture</span>
        </div>
        <div className="stat">
          <span className="stat-value">{trace.llm_calls}</span>
          <span className="stat-label">LLM calls</span>
        </div>
        <div className="stat">
          <span className="stat-value">{trace.tool_result_ids?.length ?? 0}</span>
          <span className="stat-label">Tool calls</span>
        </div>
        <div className="stat">
          <span className="stat-value">{(trace.latency_ms / 1000).toFixed(1)}s</span>
          <span className="stat-label">Latency</span>
        </div>
      </div>

      <details>
        <summary>Guardrail actions (code overriding the model)</summary>
        {trace.guardrail_notes?.length ? (
          <ul className="plain-list">
            {trace.guardrail_notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">The guardrail changed nothing.</p>
        )}
      </details>

      <details>
        <summary>Reviewer tightening (architecture B only)</summary>
        {trace.reviewer_tightened?.length ? (
          <ul className="plain-list">
            {trace.reviewer_tightened.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            {trace.architecture === "A" ? "Architecture A has no reviewer." : "No changes."}
          </p>
        )}
      </details>

      <details>
        <summary>Raw decision JSON</summary>
        <pre>{JSON.stringify(decision, null, 2)}</pre>
      </details>
    </section>
  );
}

import { useEffect, useState } from "react";
import { fetchCatalog, fetchRequestDetails, runAnalysis } from "./api.js";
import {
  VerdictBanner,
  ApprovalCard,
  FlagItem,
  EvidenceCard,
  ToolResultsList,
  RunStats,
} from "./components.jsx";

const ARCHITECTURE_INFO = {
  A: {
    title: "A — Single agent",
    detail: "One AI analyst gathers evidence in up to four tool rounds. Faster and cheaper.",
  },
  B: {
    title: "B — Analyst + reviewer",
    detail: "A second AI reviews the draft and can only tighten it. Slower, extra scrutiny.",
  },
};

export default function App() {
  const [catalog, setCatalog] = useState({ requests: [], employees: [] });
  const [source, setSource] = useState("catalog");
  const [requestId, setRequestId] = useState("");
  const [requestDetails, setRequestDetails] = useState(null);
  const [freeText, setFreeText] = useState("");
  const [requesterId, setRequesterId] = useState("");
  const [architecture, setArchitecture] = useState("A");
  const [useCache, setUseCache] = useState(true);
  const [simulateOutage, setSimulateOutage] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [handoffs, setHandoffs] = useState([]);

  useEffect(() => {
    fetchCatalog()
      .then((data) => {
        setCatalog(data);
        if (data.requests.length > 0) setRequestId(data.requests[0].request_id);
      })
      .catch((err) => setError(`Could not load request list: ${err.message}`));
  }, []);

  useEffect(() => {
    if (!requestId) return;
    setRequestDetails(null);
    fetchRequestDetails(requestId).then(setRequestDetails).catch(() => setRequestDetails(null));
  }, [requestId]);

  const canRun =
    source === "catalog" ? Boolean(requestId) : freeText.trim().length > 0;

  async function analyze() {
    setLoading(true);
    setError(null);
    setResult(null);
    setHandoffs([]);
    try {
      const data = await runAnalysis({
        source,
        request_id: requestId || null,
        free_text: freeText || null,
        requester_id: requesterId || null,
        architecture,
        use_cache: useCache,
        simulate_outage: simulateOutage,
      });
      setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const decision = result?.decision;
  const toolResults = result?.tool_results ?? [];

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>AI Procurement Request Copilot</h1>
          <p className="muted">
            Evidence-gathering and recommendation only. It cannot approve spend, purchase
            software, change a budget, or override Security / Privacy / Legal.
          </p>
        </div>
        <span className="chip">Policy 2026.09 · reference date 2026-09-30</span>
      </header>

      <div className="layout">
        {/* ------------------------------------------------ setup panel ---- */}
        <aside className="panel setup">
          <h2>1 · What is being requested?</h2>

          <div className="segmented">
            <button
              className={source === "catalog" ? "active" : ""}
              onClick={() => setSource("catalog")}
            >
              Pick a request
            </button>
            <button
              className={source === "free_text" ? "active" : ""}
              onClick={() => setSource("free_text")}
            >
              Describe it in words
            </button>
          </div>

          {source === "catalog" ? (
            <>
              <label className="field-label" htmlFor="request-select">
                Employee request
              </label>
              <select
                id="request-select"
                value={requestId}
                onChange={(e) => setRequestId(e.target.value)}
              >
                {catalog.requests.map((r) => (
                  <option key={r.request_id} value={r.request_id}>
                    {r.request_id} — {r.product_name} ({r.vendor_name})
                  </option>
                ))}
              </select>

              {requestDetails && (
                <dl className="request-summary">
                  <div>
                    <dt>Product</dt>
                    <dd>{requestDetails.product_name}</dd>
                  </div>
                  <div>
                    <dt>Vendor</dt>
                    <dd>{requestDetails.vendor_name}</dd>
                  </div>
                  <div>
                    <dt>Annual cost</dt>
                    <dd>${Number(requestDetails.annual_cost_usd).toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Seats</dt>
                    <dd>{requestDetails.user_count}</dd>
                  </div>
                  <div>
                    <dt>Requester</dt>
                    <dd>{requestDetails.requester_id}</dd>
                  </div>
                  <div>
                    <dt>Data access</dt>
                    <dd>{requestDetails.data_access_level}</dd>
                  </div>
                  <div className="wide">
                    <dt>Justification</dt>
                    <dd>“{requestDetails.business_justification}”</dd>
                  </div>
                </dl>
              )}
            </>
          ) : (
            <>
              <label className="field-label" htmlFor="free-text">
                Request in your own words
              </label>
              <textarea
                id="free-text"
                rows={5}
                placeholder="e.g. Marketing needs a design tool for the new brand refresh, about $5k a year."
                value={freeText}
                onChange={(e) => setFreeText(e.target.value)}
              />
              <label className="field-label" htmlFor="requester-select">
                Requester (optional)
              </label>
              <select
                id="requester-select"
                value={requesterId}
                onChange={(e) => setRequesterId(e.target.value)}
              >
                <option value="">(not specified)</option>
                {catalog.employees.map((e) => (
                  <option key={e.employee_id} value={e.employee_id}>
                    {e.employee_id} — {e.name}
                  </option>
                ))}
              </select>
            </>
          )}

          <h2>2 · How should it be analysed?</h2>
          <div className="arch-cards">
            {["A", "B"].map((arch) => (
              <label key={arch} className={`arch-card ${architecture === arch ? "active" : ""}`}>
                <input
                  type="radio"
                  name="architecture"
                  value={arch}
                  checked={architecture === arch}
                  onChange={() => setArchitecture(arch)}
                />
                <strong>{ARCHITECTURE_INFO[arch].title}</strong>
                <span>{ARCHITECTURE_INFO[arch].detail}</span>
              </label>
            ))}
          </div>
          <p className="muted small">
            Both share the same tools, guardrail, output schema and LLM client — only the
            agent stage differs.
          </p>

          <label className="toggle">
            <input
              type="checkbox"
              checked={useCache}
              onChange={(e) => setUseCache(e.target.checked)}
            />
            Reuse cached LLM responses when available
          </label>
          <label className="toggle">
            <input
              type="checkbox"
              checked={simulateOutage}
              onChange={(e) => setSimulateOutage(e.target.checked)}
            />
            Simulate vendor-risk service outage
          </label>
          {simulateOutage && (
            <p className="small warn-note">
              The copilot must treat a missing vendor check as a risk, never as a
              favourable status.
            </p>
          )}

          <button className="primary" onClick={analyze} disabled={!canRun || loading}>
            {loading ? "Gathering evidence…" : "Run analysis"}
          </button>
          <p className="muted small">
            A live analysis calls the LLM and usually takes 10–40 seconds.
          </p>
        </aside>

        {/* ----------------------------------------------- results panel ---- */}
        <main className="results">
          {error && <div className="banner error">⚠️ {error}</div>}

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
                      <EvidenceCard key={i} item={e} toolName={toolNameOf(e.result_id, toolResults)} />
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
                    disabled={decision.approvals_required.length === 0 || decision.severity === "ESCALATE_TO_HUMAN"}
                    onClick={() =>
                      addHandoff(setHandoffs, "Send to approver", decision.approvals_required[0]?.approver)
                    }
                  >
                    Send to approver: {decision.approvals_required[0]?.approver ?? "—"}
                  </button>
                  <button
                    disabled={decision.missing_information.length === 0}
                    onClick={() =>
                      addHandoff(setHandoffs, "Request info from employee", decision.missing_information[0])
                    }
                  >
                    Request info from employee
                  </button>
                  <button
                    className={decision.severity === "ESCALATE_TO_HUMAN" ? "danger" : ""}
                    disabled={decision.severity !== "ESCALATE_TO_HUMAN"}
                    onClick={() => addHandoff(setHandoffs, "Escalate to security", "Security")}
                  >
                    Escalate to security
                  </button>
                </div>
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
      </div>

      <footer className="footer">
        Recommendations are advisory; a human remains responsible for every approval and
        exception.
      </footer>
    </div>
  );
}

function addHandoff(setHandoffs, action, target) {
  if (!target) return;
  setHandoffs((prev) => [...prev, { action, target }]);
}

function toolNameOf(resultId, toolResults) {
  const match = toolResults.find((r) => r.result_id === resultId);
  return match ? match.tool : resultId;
}

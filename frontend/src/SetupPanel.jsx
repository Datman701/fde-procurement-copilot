// The left-hand input column. Pure presentation: App owns all state and
// passes it down, so this file has no effects and no fetching.
import RequesterPicker from "./RequesterPicker.jsx";

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

// Read the first dollar figure from the prose so the budget preview can warn
// before the analysis runs. Derived for display only — never sent anywhere.
function peekAnnualCost(text) {
  const match = /\$\s?([\d,]+(?:\.\d+)?)/.exec(text || "");
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

const CATALOG_NAMES =
  "SignFlow, TaskFlow, DocSpace, MetricLoop, HelpDeskly, CodeMate, PixelCraft Pro, CreativeSuite, SignalWatch, NeuralDesk Business";

function RequestGuide() {
  return (
    <details className="guide-card">
      <summary>How to write a request that parses</summary>
      <div className="guide-body">
        <p className="muted small">
          The extractor pulls five fields from plain English. Anything it can't
          find stays blank and the request comes back incomplete — nothing is
          guessed.
        </p>
        <ul className="guide-list">
          <li>
            <strong>Product</strong> — must be a real catalog name: {CATALOG_NAMES}.
          </li>
          <li>
            <strong>Vendor</strong> — matched against the vendor registry; usually
            the same as the product.
          </li>
          <li>
            <strong>Cost</strong> — <code>$640</code> or{" "}
            <code>about $5k a year</code>. Sets the approval tier.
          </li>
          <li>
            <strong>Seats</strong> — <code>for 4 users</code> or{" "}
            <code>12 seats</code>.
          </li>
          <li>
            <strong>Data access</strong> — <code>internal documents</code>,{" "}
            <code>customer PII</code>, <code>source code</code>,{" "}
            <code>production telemetry</code>, or <code>none</code>. Left
            unstated it becomes "unknown", which always triggers a security
            review.
          </li>
        </ul>
        <p className="guide-callout">
          <strong>Adding seats to something we already own?</strong> Use the
          exact phrase <em>additional seats</em>, <em>more seats</em>, or{" "}
          <em>add seats</em>. Any other wording ("add four more dashboard seats")
          reads as a possible duplicate and you'll get{" "}
          <code>use_existing_tool</code> instead of a clean approval path.
        </p>
        <p className="guide-callout">
          <strong>Testing without spending AI calls?</strong> Leave "Reuse
          cached AI responses" ticked — the published evaluation's responses are
          committed, so repeat runs replay instantly.
        </p>
        <p className="guide-example">
          <strong>Working example:</strong> SignFlow for the Sales team. We need
          additional seats for the quarter-end contract signing rush. Annual cost
          is $640 for 4 users. It only needs access to internal documents.
        </p>
      </div>
    </details>
  );
}

export default function SetupPanel({
  catalog,
  catalogError,
  onRetryCatalog,
  source,
  setSource,
  requestId,
  setRequestId,
  requestDetails,
  freeText,
  setFreeText,
  requesterId,
  setRequesterId,
  architecture,
  setArchitecture,
  useCache,
  setUseCache,
  simulateOutage,
  setSimulateOutage,
  loading,
  canRun,
  onRun,
}) {
  return (
    <aside className="panel setup">
      <h2>1 · What is being requested?</h2>

      <div className="segmented" role="group" aria-label="Request source">
        <button
          type="button"
          aria-pressed={source === "catalog"}
          onClick={() => setSource("catalog")}
        >
          Pick a request
        </button>
        <button
          type="button"
          aria-pressed={source === "free_text"}
          onClick={() => setSource("free_text")}
        >
          Describe it in words
        </button>
      </div>

      {catalogError && (
        <div className="banner error" role="alert">
          <p>Could not load the request list: {catalogError}</p>
          <button type="button" onClick={onRetryCatalog}>
            Try again
          </button>
        </div>
      )}

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
          <RequesterPicker
            employees={catalog.employees}
            value={requesterId}
            onChange={setRequesterId}
            annualCost={peekAnnualCost(freeText)}
          />
          <RequestGuide />
        </>
      )}

      <h2>2 · How should it be analysed?</h2>
      <div className="arch-cards">
        {["A", "B"].map((arch) => (
          <label
            key={arch}
            className={`arch-card ${architecture === arch ? "active" : ""}`}
          >
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
        Both share the same tools, guardrail, output schema and AI client — only the
        agent stage differs.
      </p>

      <label className="toggle">
        <input
          type="checkbox"
          checked={useCache}
          onChange={(e) => setUseCache(e.target.checked)}
        />
        Reuse cached AI responses when available
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

      <button
        type="button"
        className="primary"
        onClick={onRun}
        disabled={!canRun || loading}
        aria-busy={loading}
      >
        {loading ? "Gathering evidence…" : "Run analysis"}
      </button>
      <p className="muted small">
        A live analysis calls the AI and usually takes 10–40 seconds.
      </p>
    </aside>
  );
}

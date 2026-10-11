// "Policy & reference" — the source data behind every verdict, so a reviewer
// can check the copilot's claims without leaving the page. Pure presentation:
// App owns the fetch and passes the parsed payload down.
import { useEffect, useState } from "react";
import { fetchReference } from "./api.js";

const money = (n) =>
  n == null ? "—" : `$${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

function Thresholds({ thresholds }) {
  return (
    <table className="ref-table">
      <thead>
        <tr>
          <th scope="col">Annual cost</th>
          <th scope="col">Approvers required</th>
        </tr>
      </thead>
      <tbody>
        {thresholds.map((t) => (
          <tr key={t.label}>
            <td className="ref-num">{t.label}</td>
            <td>{t.approvers.join(" + ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DataAccess({ dataAccess, validityDays }) {
  return (
    <div className="ref-split">
      <div>
        <h4>Triggers a security review</h4>
        <ul className="plain-list ref-list">
          {Object.entries(dataAccess.security_triggers).map(([key, why]) => (
            <li key={key}>
              <code>{key}</code> <span className="muted small">— {why}</span>
            </li>
          ))}
        </ul>
        <p className="muted small">
          Integrations matching cloud accounts, source code, production, credentials
          or secrets also trigger review.
        </p>
      </div>
      <div>
        <h4>No trigger</h4>
        <ul className="plain-list ref-list">
          {dataAccess.no_trigger.map((key) => (
            <li key={key}>
              <code>{key}</code>
            </li>
          ))}
        </ul>
        <p className="muted small">
          A security assessment stays current for {validityDays} days; after that it
          is expired and review is required again.
        </p>
      </div>
    </div>
  );
}

function Vendors({ vendors, referenceDate }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = q
    ? vendors.filter((v) =>
        `${v.vendor_name} ${v.procurement_status} ${v.legal_terms_status}`
          .toLowerCase()
          .includes(q),
      )
    : vendors;

  return (
    <div>
      <input
        className="text-input ref-search"
        type="search"
        placeholder="Filter vendors by name or status…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Filter the vendor registry"
      />
      <p className="muted small">
        Registry status versus the risk-assessment service, as of {referenceDate}.
        <span className="ref-legend">
          <span className="badge ok">current</span>
          <span className="badge bad">expired / none</span>
        </span>
      </p>
      <table className="ref-table">
        <thead>
          <tr>
            <th scope="col">Vendor</th>
            <th scope="col">Procurement</th>
            <th scope="col">Legal terms</th>
            <th scope="col">Security review</th>
            <th scope="col">Risk</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((v) => (
            <tr key={v.vendor_id}>
              <td>
                <strong>{v.vendor_name}</strong>
                {v.is_new && <span className="badge warn ref-badge">new</span>}
                <div className="muted small ref-notes">{v.notes}</div>
              </td>
              <td>{v.procurement_status}</td>
              <td>{v.legal_terms_status}</td>
              <td>
                <span className={`badge ${v.security_review_current ? "ok" : "bad"}`}>
                  {v.security_review_current ? "current" : "expired"}
                </span>
                <div className="muted small">
                  {v.security_review_date
                    ? `reviewed ${v.security_review_date}`
                    : "no review on record"}
                  {v.expires_on && ` · expires ${v.expires_on}`}
                </div>
              </td>
              <td>{v.risk_level ?? "—"}</td>
            </tr>
          ))}
          {shown.length === 0 && (
            <tr>
              <td colSpan={5} className="muted small">
                No vendor matches “{query}”.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function Budgets({ budgets }) {
  const sorted = [...budgets].sort((a, b) => a.available_usd - b.available_usd);
  return (
    <div>
      <p className="muted small">
        Department software budgets at the policy reference date. Requests are
        charged against the available balance.
      </p>
      <table className="ref-table">
        <thead>
          <tr>
            <th scope="col">Department</th>
            <th scope="col">Annual</th>
            <th scope="col">Committed</th>
            <th scope="col">Available</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((b) => (
            <tr key={b.department}>
              <td>{b.department}</td>
              <td className="ref-num">{money(b.annual_usd)}</td>
              <td className="ref-num">{money(b.committed_usd)}</td>
              <td className="ref-num">
                <strong>{money(b.available_usd)}</strong>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ReferencePanel() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchReference(controller.signal)
      .then(setData)
      .catch((err) => {
        if (err.name !== "AbortError") setError(err.message);
      });
    return () => controller.abort();
  }, []);

  return (
    <details className="panel reference">
      <summary>
        <h2 className="reference-title">2 · Policy &amp; reference data</h2>
      </summary>

      {error && <p className="error-text">Could not load reference data: {error}</p>}
      {!data && !error && <p className="muted">Loading policy data…</p>}

      {data && (
        <div className="reference-body">
          <p className="muted small">
            Policy version <strong>{data.policy_version}</strong> · evaluated as of{" "}
            <strong>{data.reference_date}</strong>. Every number below is read from
            the same modules the guardrail uses, so it cannot drift from what the
            copilot reports.
          </p>

          <h3>Approval thresholds</h3>
          <Thresholds thresholds={data.thresholds} />
          <p className="muted small">
            New vendors above {money(data.new_vendor_legal_threshold_usd)} also need
            legal review.
          </p>

          <h3>When is a human review required?</h3>
          <DataAccess
            dataAccess={data.data_access}
            validityDays={data.security_review_validity_days}
          />

          <h3>Vendor registry</h3>
          <Vendors vendors={data.vendors} referenceDate={data.reference_date} />

          <h3>Department budgets</h3>
          <Budgets budgets={data.budgets} />
        </div>
      )}
    </details>
  );
}

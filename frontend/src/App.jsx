import { useEffect, useRef, useState } from "react";
import { fetchCatalog, fetchRequestDetails, runAnalysis } from "./api.js";
import SetupPanel from "./SetupPanel.jsx";
import ReferencePanel from "./ReferencePanel.jsx";
import ResultsPanel from "./ResultsPanel.jsx";

// The UI already quotes "10-40 seconds" to the user, so the timeout has
// generous headroom but still guarantees an escape from a hung call.
const ANALYSIS_TIMEOUT_MS = 90_000;

export default function App() {
  const [catalog, setCatalog] = useState({ requests: [], employees: [] });
  const [catalogError, setCatalogError] = useState(null);
  const [catalogAttempt, setCatalogAttempt] = useState(0);

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

  const analysisController = useRef(null);
  const abortReason = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogError(null);
    fetchCatalog(controller.signal)
      .then((data) => {
        setCatalog(data);
        if (data.requests.length > 0) setRequestId(data.requests[0].request_id);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setCatalogError(err.message);
      });
    return () => controller.abort();
  }, [catalogAttempt]);

  // Abort on change so a slow response for the previous request can never
  // overwrite the preview of the one currently selected.
  useEffect(() => {
    if (!requestId) return;
    const controller = new AbortController();
    setRequestDetails(null);
    fetchRequestDetails(requestId, controller.signal)
      .then(setRequestDetails)
      .catch(() => {
        if (!controller.signal.aborted) setRequestDetails(null);
      });
    return () => controller.abort();
  }, [requestId]);

  useEffect(() => () => analysisController.current?.abort(), []);

  const canRun =
    source === "catalog" ? Boolean(requestId) : freeText.trim().length > 0;

  async function analyze() {
    const controller = new AbortController();
    analysisController.current = controller;
    abortReason.current = null;
    const timer = setTimeout(() => {
      abortReason.current = "timeout";
      controller.abort();
    }, ANALYSIS_TIMEOUT_MS);

    setLoading(true);
    setError(null);
    setResult(null);
    setHandoffs([]);
    try {
      const data = await runAnalysis(
        {
          source,
          request_id: requestId || null,
          free_text: freeText || null,
          requester_id: requesterId || null,
          architecture,
          use_cache: useCache,
          simulate_outage: simulateOutage,
        },
        controller.signal,
      );
      setResult(data);
    } catch (err) {
      if (abortReason.current === "timeout") {
        setError(
          `Analysis timed out after ${ANALYSIS_TIMEOUT_MS / 1000} seconds. ` +
            "Try again — leaving the response cache on makes a retry instant.",
        );
      } else if (abortReason.current !== "cancelled") {
        setError(err.message);
      }
    } finally {
      clearTimeout(timer);
      analysisController.current = null;
      abortReason.current = null;
      setLoading(false);
    }
  }

  function cancelAnalysis() {
    abortReason.current = "cancelled";
    analysisController.current?.abort();
  }

  function addHandoff(action, target) {
    if (!target) return;
    setHandoffs((prev) => [...prev, { action, target }]);
  }

  const decision = result?.decision;

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
        <div className="left-column">
          <SetupPanel
            catalog={catalog}
            catalogError={catalogError}
            onRetryCatalog={() => setCatalogAttempt((n) => n + 1)}
            source={source}
            setSource={setSource}
            requestId={requestId}
            setRequestId={setRequestId}
            requestDetails={requestDetails}
            freeText={freeText}
            setFreeText={setFreeText}
            requesterId={requesterId}
            setRequesterId={setRequesterId}
            architecture={architecture}
            setArchitecture={setArchitecture}
            useCache={useCache}
            setUseCache={setUseCache}
            simulateOutage={simulateOutage}
            setSimulateOutage={setSimulateOutage}
            loading={loading}
            canRun={canRun}
            onRun={analyze}
          />
          <ReferencePanel />
        </div>

        <div className="results-column">
          {loading && (
            <button type="button" className="cancel" onClick={cancelAnalysis}>
              Cancel analysis
            </button>
          )}
          <ResultsPanel
            error={error}
            loading={loading}
            decision={decision}
            toolResults={result?.tool_results ?? []}
            extractions={result?.extractions ?? []}
            employees={catalog.employees}
            handoffs={handoffs}
            onAddHandoff={addHandoff}
          />
        </div>
      </div>

      <footer className="footer">
        Recommendations are advisory; a human remains responsible for every approval and
        exception.
      </footer>
    </div>
  );
}

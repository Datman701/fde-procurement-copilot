"""FastAPI bridge between the React UI and the shared ``analyze()`` entry point.

The API adds no business logic: it loads data stores (with an optional outage
overlay), dispatches to :func:`app.analyze.analyze`, and returns the validated
Decision plus the real tool envelopes produced during the run. The built React
app in ``frontend/dist`` is served from the same process so ``make run`` stays
a two-process stack (vendor-risk API + this server).
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analyze import analyze
from core.data import DataStore

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

app = FastAPI(title="AI Procurement Copilot API")

DIST = ROOT / "frontend" / "dist"


def _json_safe(obj: object) -> object:
    """Convert pydantic models/dates/enums into plain JSON-safe structures."""
    return json.loads(json.dumps(obj, default=str))


class AnalyzeBody(BaseModel):
    source: str  # "catalog" or "free_text"
    request_id: str | None = None
    free_text: str | None = None
    requester_id: str | None = None
    architecture: str = "A"
    use_cache: bool = True
    simulate_outage: bool = False


@app.get("/api/requests")
def list_requests() -> dict:
    store = DataStore.load()

    by_id = {e.employee_id: e for e in store.employees}
    managed_by = {e.manager_id for e in store.employees if e.manager_id}

    def _head_for(employee) -> dict | None:
        """The resolved department head, using the same rule as
        tools/employee.department_head(): the requester's manager, if that
        person holds the Director level."""
        manager = by_id.get(employee.manager_id) if employee.manager_id else None
        if manager is not None and manager.level.strip().lower() == "director":
            return {"employee_id": manager.employee_id, "name": manager.name}
        return None

    employees = []
    for e in store.employees:
        manager = by_id.get(e.manager_id) if e.manager_id else None
        budget = store.budget(e.department)
        employees.append(
            {
                "employee_id": e.employee_id,
                "name": e.name,
                "department": e.department,
                "level": e.level,
                "manager_id": e.manager_id,
                "manager_name": manager.name if manager else None,
                # A Director is a department head when someone reports to them.
                "is_department_head": (
                    e.level.strip().lower() == "director"
                    and e.employee_id in managed_by
                ),
                "department_head": _head_for(e),
                "budget": (
                    {
                        "annual_software_budget_usd": budget.annual_software_budget_usd,
                        "committed_usd": budget.committed_usd,
                        "available_usd": budget.available_usd,
                    }
                    if budget
                    else None
                ),
            }
        )

    return {
        "requests": [
            {
                "request_id": r.request_id,
                "product_name": r.product_name,
                "vendor_name": r.vendor_name,
            }
            for r in store.requests
        ],
        "employees": employees,
    }


@app.get("/api/requests/{request_id}")
def request_details(request_id: str) -> dict:
    store = DataStore.load()
    try:
        return _json_safe(store.request(request_id).to_dict())
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown request: {request_id}") from exc


@app.post("/api/analyze")
def run_analysis(body: AnalyzeBody) -> dict:
    overlay = {"vendor_risk_force_fail": True} if body.simulate_outage else None
    store = DataStore.load(overlay)

    if body.source == "free_text":
        target = (body.free_text or "").strip() or None
        if target is None:
            raise HTTPException(status_code=400, detail="Free-text request is empty.")
    else:
        try:
            target = store.request(body.request_id or "")
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail=f"Unknown request id: {body.request_id}"
            ) from exc
        # DataStore.request returns None rather than raising for an id it does
        # not hold, which would otherwise reach analyze() and surface as a 500.
        if target is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown request id: {body.request_id}"
            )

    tool_results: list = []
    extractions: list = []
    try:
        decision = analyze(
            target,
            architecture=body.architecture,
            overlay=overlay,
            use_cache=body.use_cache,
            store=store,
            requester=body.requester_id,
            tool_result_sink=tool_results.extend,
            extraction_sink=extractions.extend,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return {
        "decision": _json_safe(decision.model_dump()),
        "tool_results": [_json_safe(r.model_dump()) for r in tool_results],
        "extractions": _json_safe(extractions),
    }


if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
else:  # pragma: no cover - fallback page before the frontend is built
    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(
            "<h1>UI not built</h1><p>Run <code>make ui-install ui-build</code> "
            "(requires Node.js), or <code>make run-streamlit</code> for the "
            "Streamlit UI.</p>"
        )

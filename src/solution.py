"""Assessment adapter required by the starter pack.

``evals/run_public_evals.py`` and any hidden harness call
``handle_request(request_id, architecture)`` and expect a
``src.contracts.ProcurementDecision``. This module is a thin shim over the real
entry point :func:`app.analyze.analyze` so the starter harness and the product
share one code path (ISS-15).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts import Architecture, ProcurementDecision  # noqa: E402

from app.analyze import analyze  # noqa: E402


def handle_request(
    request_id: str,
    architecture: Architecture = "single",
) -> ProcurementDecision:
    """Analyse a request and return the starter-pack contract.

    ``single`` -> architecture A, ``staged`` -> architecture B.
    """
    decision = analyze(request_id, architecture=architecture)
    return decision.to_procurement_decision()


__all__ = ["handle_request"]

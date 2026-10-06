"""Output and tool-envelope schemas.

Two shapes live here:

* :class:`ToolResult` - the uniform envelope every tool returns.
* :class:`Decision`   - the rich internal decision (superset of the required
  output) produced by the agent stage + guardrail.

``src/contracts.ProcurementDecision`` is the starter pack's required output and
is intentionally *not* modified. :func:`to_procurement_decision` converts a
:class:`Decision` into it so the starter harness keeps passing unchanged.
See docs/starter_pack_issues.md (ISS-12).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

Recommendation = Literal[
    "use_existing_tool",
    "proceed_with_approvals",
    "request_more_info",
    "escalate_security_review",
    "reject",
]

Severity = Literal["PROCEED", "NEEDS_APPROVAL", "ESCALATE_TO_HUMAN"]

FlagSeverity = Literal["info", "warning", "high"]

SEVERITY_ORDER: dict[str, int] = {
    "PROCEED": 0,
    "NEEDS_APPROVAL": 1,
    "ESCALATE_TO_HUMAN": 2,
}


def max_severity(*severities: str) -> str:
    """The most severe of the given severities (severity can only be raised)."""
    known = [s for s in severities if s in SEVERITY_ORDER]
    if not known:
        return "NEEDS_APPROVAL"
    return max(known, key=lambda s: SEVERITY_ORDER[s])


# --------------------------------------------------------------------------- #
# Tool envelope
# --------------------------------------------------------------------------- #
class ToolResult(BaseModel):
    """Uniform envelope. Every tool call returns exactly this, success or not."""

    ok: bool
    tool: str
    result_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    source: str
    as_of: str | None = None
    error: str | None = None

    def fact(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def brief(self) -> str:
        """Prompt-safe rendering of the payload, prefixed with the result_id.

        The agent is told to cite ``result_id`` in its evidence, so the id has to
        be visible in the tool output it reads.
        """
        header = f"{self.result_id} [{self.tool}] source={self.source} as_of={self.as_of}"
        if not self.ok:
            return f"{header}\nFAILED: {self.error}"
        return f"{header}\n" + json.dumps(self.data, default=str, sort_keys=True)[:4000]


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
class EvidenceItem(BaseModel):
    claim: str
    result_id: str
    quote_or_value: str


class Approval(BaseModel):
    approver: str
    reason: str
    policy_clause: str


class RiskFlag(BaseModel):
    flag: str
    severity: FlagSeverity = "warning"
    source_result_id: str | None = None
    detail: str = ""


class Trace(BaseModel):
    architecture: Literal["A", "B"]
    llm_calls: int = 0
    tool_calls: int = 0
    tool_names: list[str] = Field(default_factory=list)
    tool_result_ids: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    llm_cached_calls: int = 0
    schema_retries: int = 0
    guardrail_notes: list[str] = Field(default_factory=list)
    reviewer_tightened: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """The six required fields plus the deterministic/trace metadata."""

    request_id: str
    recommendation: Recommendation
    severity: Severity
    evidence: list[EvidenceItem] = Field(default_factory=list)
    approvals_required: list[Approval] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    next_step: str
    needs_human_review: bool = True
    trace: Trace

    @field_validator("recommendation")
    @classmethod
    def _known_recommendation(cls, v: str) -> str:
        allowed = {
            "use_existing_tool",
            "proceed_with_approvals",
            "request_more_info",
            "escalate_security_review",
            "reject",
        }
        if v not in allowed:
            raise ValueError(f"unknown recommendation {v!r}; expected one of {sorted(allowed)}")
        return v

    # -- convenience ------------------------------------------------------- #
    def approver_names(self) -> list[str]:
        return [a.approver for a in self.approvals_required]

    def flag_names(self) -> list[str]:
        return [f.flag for f in self.risk_flags]

    def to_procurement_decision(self):
        """Convert to the starter pack's ``src.contracts.ProcurementDecision``."""
        from src.contracts import EvidenceItem as StarterEvidence
        from src.contracts import ProcurementDecision, RunTelemetry

        return ProcurementDecision(
            request_id=self.request_id,
            recommendation=self.recommendation,
            evidence=[
                StarterEvidence(
                    source=item.result_id,
                    finding=item.claim,
                    reference=item.quote_or_value[:400] or None,
                )
                for item in self.evidence
            ],
            required_approvals=self.approver_names(),
            missing_information=list(self.missing_information),
            risk_flags=self.flag_names(),
            next_step=self.next_step,
            human_review_required=self.needs_human_review,
            telemetry=RunTelemetry(
                llm_calls=self.trace.llm_calls,
                # tool_calls comes from the registry (every invocation, including
                # failures); tool_names is the de-duplicated list of tool kinds.
                tool_calls=max(
                    self.trace.tool_calls, len(self.trace.tool_result_ids)
                ),
                tool_names=list(self.trace.tool_names),
            ),
        )


#: The contract the agent stage is asked to produce (model output only).
AgentDraft = {
    "recommendation": "one of use_existing_tool | proceed_with_approvals | "
                      "request_more_info | escalate_security_review | reject",
    "severity": "one of PROCEED | NEEDS_APPROVAL | ESCALATE_TO_HUMAN",
    "evidence": [
        {
            "claim": "one factual statement",
            "result_id": "the exact result_id of the tool result this came from",
            "quote_or_value": "the literal value from that tool result",
        }
    ],
    "missing_information": ["short phrase, empty if nothing is missing"],
    "next_step": "one concrete action for a human",
}


def fallback_decision(
    request_id: str,
    architecture: str,
    trace: Trace,
    missing_information: list[str],
    next_step: str,
    evidence: list[EvidenceItem] | None = None,
) -> Decision:
    """Safe fallback used when the LLM is unavailable or output is unusable.

    Never fabricates: escalates to a human and records why.
    """
    return Decision(
        request_id=request_id,
        recommendation="escalate_security_review",
        severity="ESCALATE_TO_HUMAN",
        evidence=evidence or [],
        approvals_required=[],
        missing_information=missing_information,
        risk_flags=[
            RiskFlag(
                flag="model_unavailable",
                severity="high",
                source_result_id=None,
                detail=next_step,
            )
        ],
        next_step=next_step,
        needs_human_review=True,
        trace=trace,
    )

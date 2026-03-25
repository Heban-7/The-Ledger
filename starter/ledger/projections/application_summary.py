"""
ledger/projections/application_summary.py — ApplicationSummary projection

Read-optimized view: one row per application.

Lifecycle columns (aligned with loan aggregate ApplicationState + coarse phase):
- state: canonical state string (matches ApplicationState)
- lifecycle_phase: INTAKE | CREDIT | FRAUD | COMPLIANCE | DECISION | HUMAN_REVIEW | TERMINAL
- previous_state: prior state before last transition (None on first event)
- state_entered_at: ISO timestamp when current state was entered
- loan_stream_position: last stream_position seen on loan-{application_id} (None until first loan event)
- terminal_outcome: APPROVED | DECLINED | DECLINED_COMPLIANCE | REFERRED | None

For SQL DDL alignment, see APPLICATION_SUMMARY_ROW_SCHEMA (documentation of logical columns).
"""
from __future__ import annotations

from ledger.projections.daemon import Projection

# Document logical schema for DB migrations / reporting (in-memory projection uses the same keys)
APPLICATION_SUMMARY_ROW_SCHEMA = {
    "application_id": "text PRIMARY KEY",
    "state": "text NOT NULL",
    "lifecycle_phase": "text NOT NULL",
    "previous_state": "text",
    "state_entered_at": "timestamptz",
    "loan_stream_position": "bigint",
    "terminal_outcome": "text",
    "applicant_id": "text",
    "requested_amount_usd": "numeric",
    "approved_amount_usd": "numeric",
    "risk_tier": "text",
    "fraud_score": "double precision",
    "compliance_status": "text",
    "decision": "text",
    "agent_sessions_completed": "text[]",
    "last_event_type": "text",
    "last_event_at": "timestamptz",
    "last_stream_id": "text",
    "last_stream_position": "bigint",
    "global_position": "bigint",
}


def _lifecycle_phase(state: str) -> str:
    if state in ("NEW", "SUBMITTED", "DOCUMENTS_PENDING", "DOCUMENTS_UPLOADED", "DOCUMENTS_PROCESSED"):
        return "INTAKE"
    if state in ("CREDIT_ANALYSIS_REQUESTED", "CREDIT_ANALYSIS_COMPLETE"):
        return "CREDIT"
    if state in ("FRAUD_SCREENING_REQUESTED", "FRAUD_SCREENING_COMPLETE"):
        return "FRAUD"
    if state in ("COMPLIANCE_CHECK_REQUESTED", "COMPLIANCE_CHECK_COMPLETE"):
        return "COMPLIANCE"
    if state == "PENDING_DECISION":
        return "DECISION"
    if state == "PENDING_HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    if state in ("APPROVED", "DECLINED", "DECLINED_COMPLIANCE", "REFERRED"):
        return "TERMINAL"
    return "UNKNOWN"


def _terminal_outcome(state: str) -> str | None:
    if state == "APPROVED":
        return "APPROVED"
    if state == "DECLINED":
        return "DECLINED"
    if state == "DECLINED_COMPLIANCE":
        return "DECLINED_COMPLIANCE"
    if state == "REFERRED":
        return "REFERRED"
    return None


def _set_state(row: dict, new_state: str, recorded_at) -> None:
    if row.get("state") != new_state:
        row["previous_state"] = row.get("state")
        row["state"] = new_state
        row["state_entered_at"] = recorded_at
    row["lifecycle_phase"] = _lifecycle_phase(new_state)
    row["terminal_outcome"] = _terminal_outcome(new_state)


class ApplicationSummaryProjection(Projection):
    """Read-optimized view: one row per application."""

    name = "ApplicationSummary"

    def __init__(self):
        self._rows: dict[str, dict] = {}

    async def handle(self, store, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        app_id = p.get("application_id")
        if not app_id:
            return

        stream_id = event.get("stream_id") or ""
        stream_pos = event.get("stream_position")
        recorded_at = event.get("recorded_at")

        row = self._rows.setdefault(
            app_id,
            {
                "application_id": app_id,
                "state": "NEW",
                "lifecycle_phase": "INTAKE",
                "previous_state": None,
                "state_entered_at": None,
                "loan_stream_position": None,
                "terminal_outcome": None,
                "applicant_id": None,
                "requested_amount_usd": None,
                "approved_amount_usd": None,
                "risk_tier": None,
                "fraud_score": None,
                "compliance_status": None,
                "decision": None,
                "agent_sessions_completed": [],
                "last_event_type": et,
                "last_event_at": recorded_at,
                "last_stream_id": stream_id,
                "last_stream_position": stream_pos,
                "global_position": event.get("global_position"),
            },
        )
        row["last_event_type"] = et
        row["last_event_at"] = recorded_at
        row["last_stream_id"] = stream_id
        row["last_stream_position"] = stream_pos
        row["global_position"] = event.get("global_position")

        if stream_id == f"loan-{app_id}" and stream_pos is not None:
            row["loan_stream_position"] = stream_pos

        if et == "ApplicationSubmitted":
            _set_state(row, "SUBMITTED", recorded_at)
            row["applicant_id"] = p.get("applicant_id")
            row["requested_amount_usd"] = p.get("requested_amount_usd")
        elif et == "DocumentUploadRequested":
            _set_state(row, "DOCUMENTS_PENDING", recorded_at)
        elif et == "DocumentUploaded":
            _set_state(row, "DOCUMENTS_UPLOADED", recorded_at)
        elif et == "CreditAnalysisRequested":
            _set_state(row, "CREDIT_ANALYSIS_REQUESTED", recorded_at)
        elif et == "CreditAnalysisCompleted":
            _set_state(row, "CREDIT_ANALYSIS_COMPLETE", recorded_at)
            dec = p.get("decision", {})
            if isinstance(dec, dict):
                row["risk_tier"] = dec.get("risk_tier")
        elif et == "FraudScreeningRequested":
            _set_state(row, "FRAUD_SCREENING_REQUESTED", recorded_at)
        elif et == "FraudScreeningCompleted":
            _set_state(row, "FRAUD_SCREENING_COMPLETE", recorded_at)
            row["fraud_score"] = p.get("fraud_score")
        elif et == "ComplianceCheckRequested":
            _set_state(row, "COMPLIANCE_CHECK_REQUESTED", recorded_at)
        elif et == "ComplianceCheckCompleted":
            _set_state(row, "COMPLIANCE_CHECK_COMPLETE", recorded_at)
            row["compliance_status"] = p.get("overall_verdict", "PENDING")
        elif et == "DecisionRequested":
            _set_state(row, "PENDING_DECISION", recorded_at)
        elif et == "DecisionGenerated":
            row["decision"] = p.get("recommendation")
        elif et == "HumanReviewRequested":
            _set_state(row, "PENDING_HUMAN_REVIEW", recorded_at)
        elif et == "HumanReviewCompleted":
            pass
        elif et == "ApplicationApproved":
            _set_state(row, "APPROVED", recorded_at)
            row["approved_amount_usd"] = p.get("approved_amount_usd")
        elif et == "ApplicationDeclined":
            codes = p.get("adverse_action_codes") or []
            reasons = p.get("decline_reasons") or []
            if "COMPLIANCE_BLOCK" in codes or any("Compliance" in str(r) for r in reasons):
                _set_state(row, "DECLINED_COMPLIANCE", recorded_at)
            else:
                _set_state(row, "DECLINED", recorded_at)

    def get(self, application_id: str) -> dict | None:
        return self._rows.get(application_id)

    def rebuild_from_scratch(self) -> None:
        """Clear all rows so the projection can replay from checkpoint=-1."""
        self._rows.clear()

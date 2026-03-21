"""
ledger/projections/application_summary.py — ApplicationSummary projection
"""
from __future__ import annotations
from collections import defaultdict

from ledger.projections.daemon import Projection


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
        row = self._rows.setdefault(app_id, {
            "application_id": app_id,
            "state": "NEW",
            "applicant_id": None,
            "requested_amount_usd": None,
            "approved_amount_usd": None,
            "risk_tier": None,
            "fraud_score": None,
            "compliance_status": None,
            "decision": None,
            "agent_sessions_completed": [],
            "last_event_type": et,
            "last_event_at": event.get("recorded_at"),
        })
        row["last_event_type"] = et
        row["last_event_at"] = event.get("recorded_at")

        if et == "ApplicationSubmitted":
            row["state"] = "SUBMITTED"
            row["applicant_id"] = p.get("applicant_id")
            row["requested_amount_usd"] = p.get("requested_amount_usd")
        elif et == "DocumentUploadRequested":
            row["state"] = "DOCUMENTS_PENDING"
        elif et == "DocumentUploaded":
            row["state"] = "DOCUMENTS_UPLOADED"
        elif et == "CreditAnalysisRequested":
            row["state"] = "CREDIT_ANALYSIS_REQUESTED"
        elif et == "CreditAnalysisCompleted":
            row["state"] = "CREDIT_ANALYSIS_COMPLETE"
            dec = p.get("decision", {})
            if isinstance(dec, dict):
                row["risk_tier"] = dec.get("risk_tier")
        elif et == "FraudScreeningRequested":
            row["state"] = "FRAUD_SCREENING_REQUESTED"
        elif et == "FraudScreeningCompleted":
            row["state"] = "FRAUD_SCREENING_COMPLETE"
            row["fraud_score"] = p.get("fraud_score")
        elif et == "ComplianceCheckRequested":
            row["state"] = "COMPLIANCE_CHECK_REQUESTED"
        elif et == "ComplianceCheckCompleted":
            row["state"] = "COMPLIANCE_CHECK_COMPLETE"
            row["compliance_status"] = p.get("overall_verdict", "PENDING")
        elif et == "DecisionRequested":
            row["state"] = "PENDING_DECISION"
        elif et == "DecisionGenerated":
            row["decision"] = p.get("recommendation")
        elif et == "ApplicationApproved":
            row["state"] = "APPROVED"
            row["approved_amount_usd"] = p.get("approved_amount_usd")
        elif et == "ApplicationDeclined":
            row["state"] = "DECLINED"

    def get(self, application_id: str) -> dict | None:
        return self._rows.get(application_id)

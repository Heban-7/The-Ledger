"""
ledger/what_if/projector.py — What-If Projections & Counterfactual Analysis (Phase 6 Bonus)

Enables risk and compliance officers to run counterfactual scenarios:
"What would the decision have been if we had used the March risk model or if risk_tier had been HIGH?"

CRITICAL GUARANTEES:
1. NEVER writes counterfactual events to the real event store.
2. Causal dependency filtering: skips events whose causation_id or domain semantics
   trace back to the branched event, preserving causally independent events.
3. Evaluates fresh projection instances over the counterfactual event stream to compute
   divergent outcomes and plain-English divergence summaries.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ledger.event_store import InMemoryEventStore
from ledger.projections.application_summary import ApplicationSummaryProjection
from ledger.projections.compliance_audit import ComplianceAuditProjection
from ledger.projections.agent_performance import AgentPerformanceLedgerProjection


@dataclass
class WhatIfResult:
    """Structured result of a counterfactual projection run."""
    application_id: str
    branch_at_event_type: str
    counterfactual_event_types: list[str]
    divergence_detected: bool
    real_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any]
    divergence_events: list[dict[str, Any]]
    causally_dependent_events_skipped: list[dict[str, Any]]
    total_real_events: int
    total_counterfactual_events_replayed: int
    narrative_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "branch_at_event_type": self.branch_at_event_type,
            "counterfactual_event_types": self.counterfactual_event_types,
            "divergence_detected": self.divergence_detected,
            "real_outcome": self.real_outcome,
            "counterfactual_outcome": self.counterfactual_outcome,
            "divergence_events": self.divergence_events,
            "causally_dependent_events_skipped": self.causally_dependent_events_skipped,
            "total_real_events": self.total_real_events,
            "total_counterfactual_events_replayed": self.total_counterfactual_events_replayed,
            "narrative_summary": self.narrative_summary,
        }


# Events that are downstream domain decisions dependent on Credit/Fraud/Compliance analyses
_DECISION_DEPENDENT_TYPES = frozenset({
    "DecisionGenerated",
    "ApplicationApproved",
    "ApplicationDeclined",
    "HumanReviewRequested",
    "HumanReviewCompleted",
})


def _is_causally_dependent(
    event: dict,
    branched_event_ids: set[str],
    branched_positions: set[int],
    branch_index: int,
    current_index: int,
) -> bool:
    """
    Determines if an event occurring after the branch point is causally dependent on the branched event.
    An event is dependent if:
      - its metadata.causation_id references any branched event ID or position
      - its payload references a branched session or event
      - it is a downstream decision/approval event that evaluates the branched analysis
    """
    meta = event.get("metadata", {}) or {}
    payload = event.get("payload", {}) or {}
    etype = event.get("event_type", "")

    cause = meta.get("causation_id")
    if cause:
        if str(cause) in branched_event_ids:
            return True
        for pos in branched_positions:
            if f":{pos}" in str(cause) or str(cause) == str(pos):
                return True

    triggered_by = payload.get("triggered_by_event_id")
    if triggered_by and str(triggered_by) in branched_event_ids:
        return True

    # If the branch is a credit analysis, fraud screening, or compliance check,
    # all subsequent decision and lifecycle approval/decline events are causally dependent.
    if current_index > branch_index and etype in _DECISION_DEPENDENT_TYPES:
        return True

    return False


async def run_what_if(
    store,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[dict],
    projections: list[Any] | None = None,
) -> WhatIfResult:
    """
    Executes a counterfactual replay for an application:
    1. Load all real events for the application (loan, credit, fraud, compliance streams).
    2. Split events into pre-branch, branched, and post-branch.
    3. Filter post-branch events by causal independence.
    4. Inject counterfactual events at the branch point.
    5. Evaluate projections on both the real stream and the counterfactual stream.
    6. Return a comprehensive WhatIfResult.
    """
    # 1. Load all related streams for this application
    loan_stream_id = f"loan-{application_id}"
    credit_stream_id = f"credit-{application_id}"
    fraud_stream_id = f"fraud-{application_id}"
    compliance_stream_id = f"compliance-{application_id}"

    all_raw_events: list[dict] = []
    for sid in [loan_stream_id, credit_stream_id, fraud_stream_id, compliance_stream_id]:
        stream_events = await store.load_stream(sid)
        all_raw_events.extend(stream_events)

    # Sort real events globally by recorded_at / global_position
    def _sort_key(ev: dict):
        rec = ev.get("recorded_at") or ""
        gpos = ev.get("global_position", 0)
        spos = ev.get("stream_position", 0)
        return (str(rec), gpos, spos)

    all_real_events = sorted(all_raw_events, key=_sort_key)

    # 2. Find branch point
    branch_indices = [
        i for i, ev in enumerate(all_real_events)
        if ev.get("event_type") == branch_at_event_type
    ]
    if not branch_indices:
        # If event type not in history, append counterfactual at the end
        branch_index = len(all_real_events)
        branched_event_ids: set[str] = set()
        branched_positions: set[int] = set()
    else:
        branch_index = branch_indices[0]
        branched_event = all_real_events[branch_index]
        branched_event_ids = {str(branched_event.get("event_id", ""))}
        branched_positions = {int(branched_event.get("stream_position", -1))}

    pre_branch_events = all_real_events[:branch_index]
    post_branch_candidates = all_real_events[branch_index + (1 if branch_indices else 0):]

    # 3. Filter post-branch events for causal independence
    post_branch_independent: list[dict] = []
    skipped_dependent: list[dict] = []

    for i, ev in enumerate(post_branch_candidates):
        actual_idx = branch_index + 1 + i
        if _is_causally_dependent(ev, branched_event_ids, branched_positions, branch_index, actual_idx):
            skipped_dependent.append(ev)
        else:
            post_branch_independent.append(ev)

    # 4. Construct counterfactual event stream
    # Ensure counterfactual events have appropriate timestamps and shapes
    cf_events_formatted = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, cf in enumerate(counterfactual_events):
        cf_dict = dict(cf)
        cf_dict.setdefault("event_id", f"cf-event-{i}")
        cf_dict.setdefault("stream_id", cf_dict.get("stream_id", loan_stream_id))
        cf_dict.setdefault("stream_position", len(pre_branch_events) + i)
        cf_dict.setdefault("recorded_at", now_iso)
        cf_events_formatted.append(cf_dict)

    # If the counterfactual is a CreditAnalysisCompleted with HIGH risk tier or low confidence,
    # generate the appropriate synthetic cascading decision events for the counterfactual stream.
    cascading_events = []
    for cf in cf_events_formatted:
        if cf.get("event_type") == "CreditAnalysisCompleted":
            payload = cf.get("payload", {})
            decision = payload.get("decision", {}) if isinstance(payload.get("decision"), dict) else payload
            risk_tier = decision.get("risk_tier", payload.get("risk_tier", "MEDIUM"))
            conf = decision.get("confidence", payload.get("confidence_score", 0.8))

            if risk_tier == "HIGH":
                cascading_events.append({
                    "event_id": f"cf-decision-{application_id}",
                    "stream_id": loan_stream_id,
                    "stream_position": 999,
                    "event_type": "DecisionGenerated",
                    "event_version": 2,
                    "payload": {
                        "application_id": application_id,
                        "orchestrator_session_id": "cf-orch",
                        "recommendation": "DECLINE",
                        "confidence": conf or 0.85,
                        "decision_basis_summary": "Counterfactual evaluation: risk_tier is HIGH",
                        "model_versions": {"orchestrator": "counterfactual-v1"},
                        "generated_at": now_iso,
                    },
                    "metadata": {"correlation_id": "what-if-run"},
                    "recorded_at": now_iso,
                })
                cascading_events.append({
                    "event_id": f"cf-decline-{application_id}",
                    "stream_id": loan_stream_id,
                    "stream_position": 1000,
                    "event_type": "ApplicationDeclined",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "decline_reasons": ["Risk tier HIGH exceeds credit policy thresholds"],
                        "adverse_action_notice_required": True,
                        "declined_by": "cf-orch",
                        "declined_at": now_iso,
                    },
                    "metadata": {"correlation_id": "what-if-run"},
                    "recorded_at": now_iso,
                })
            elif conf is not None and conf < 0.6:
                cascading_events.append({
                    "event_id": f"cf-decision-{application_id}",
                    "stream_id": loan_stream_id,
                    "stream_position": 999,
                    "event_type": "DecisionGenerated",
                    "event_version": 2,
                    "payload": {
                        "application_id": application_id,
                        "orchestrator_session_id": "cf-orch",
                        "recommendation": "REFER",
                        "confidence": conf,
                        "decision_basis_summary": "Counterfactual evaluation: confidence below regulatory floor",
                        "model_versions": {"orchestrator": "counterfactual-v1"},
                        "generated_at": now_iso,
                    },
                    "metadata": {"correlation_id": "what-if-run"},
                    "recorded_at": now_iso,
                })
                cascading_events.append({
                    "event_id": f"cf-refer-{application_id}",
                    "stream_id": loan_stream_id,
                    "stream_position": 1000,
                    "event_type": "HumanReviewRequested",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "reason": "REFER (Confidence < 0.60)",
                        "requested_at": now_iso,
                    },
                    "metadata": {"correlation_id": "what-if-run"},
                    "recorded_at": now_iso,
                })

    counterfactual_stream = pre_branch_events + cf_events_formatted + cascading_events + post_branch_independent

    # 5. Evaluate real vs counterfactual projections
    # Real outcome
    real_app_proj = ApplicationSummaryProjection()
    real_comp_proj = ComplianceAuditProjection()
    isolated_real_store = InMemoryEventStore()
    for ev in all_real_events:
        await real_app_proj.handle(isolated_real_store, ev)
        await real_comp_proj.handle(isolated_real_store, ev)

    real_app_state = real_app_proj.get_application(application_id) or {}
    real_comp_state = real_comp_proj.get_compliance(application_id) or {}

    # Counterfactual outcome
    cf_app_proj = ApplicationSummaryProjection()
    cf_comp_proj = ComplianceAuditProjection()
    isolated_cf_store = InMemoryEventStore()
    for ev in counterfactual_stream:
        await cf_app_proj.handle(isolated_cf_store, ev)
        await cf_comp_proj.handle(isolated_cf_store, ev)

    cf_app_state = cf_app_proj.get_application(application_id) or {}
    cf_comp_state = cf_comp_proj.get_compliance(application_id) or {}

    # 6. Detect divergence
    real_outcome = {
        "state": real_app_state.get("state"),
        "decision": real_app_state.get("decision"),
        "approved_amount_usd": real_app_state.get("approved_amount_usd"),
        "risk_tier": real_app_state.get("risk_tier"),
        "fraud_score": real_app_state.get("fraud_score"),
        "compliance_status": real_app_state.get("compliance_status"),
        "last_event_type": real_app_state.get("last_event_type"),
    }

    cf_outcome = {
        "state": cf_app_state.get("state"),
        "decision": cf_app_state.get("decision"),
        "approved_amount_usd": cf_app_state.get("approved_amount_usd"),
        "risk_tier": cf_app_state.get("risk_tier"),
        "fraud_score": cf_app_state.get("fraud_score"),
        "compliance_status": cf_app_state.get("compliance_status"),
        "last_event_type": cf_app_state.get("last_event_type"),
    }

    divergence_detected = (
        real_outcome["state"] != cf_outcome["state"] or
        real_outcome["decision"] != cf_outcome["decision"] or
        real_outcome["risk_tier"] != cf_outcome["risk_tier"] or
        real_outcome["approved_amount_usd"] != cf_outcome["approved_amount_usd"]
    )

    divergence_events = cf_events_formatted + cascading_events

    cf_types = [e.get("event_type", "Unknown") for e in counterfactual_events]
    summary_msg = (
        f"Counterfactual branch at '{branch_at_event_type}' injected {len(counterfactual_events)} events. "
        f"Real outcome was {real_outcome.get('decision', 'N/A')} (state: {real_outcome.get('state')}). "
        f"Counterfactual outcome is {cf_outcome.get('decision', 'N/A')} (state: {cf_outcome.get('state')}). "
        f"Divergence detected: {divergence_detected}."
    )

    return WhatIfResult(
        application_id=application_id,
        branch_at_event_type=branch_at_event_type,
        counterfactual_event_types=cf_types,
        divergence_detected=divergence_detected,
        real_outcome=real_outcome,
        counterfactual_outcome=cf_outcome,
        divergence_events=divergence_events,
        causally_dependent_events_skipped=skipped_dependent,
        total_real_events=len(all_real_events),
        total_counterfactual_events_replayed=len(counterfactual_stream),
        narrative_summary=summary_msg,
    )

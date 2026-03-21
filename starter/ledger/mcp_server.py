"""
ledger/mcp_server.py — MCP Server for The Ledger

8 Tools (commands) + Resources (queries via get_application, get_health).
Tools write events; Resources read from projections.
"""
from __future__ import annotations
import json

try:
    from fastmcp import FastMCP
except ImportError:
    FastMCP = None

# Global store and projections - set by init_ledger_mcp()
_store = None
_projections = None


def _get_store():
    if _store is None:
        raise RuntimeError("MCP server not initialized: call init_ledger_mcp(store, projections) first")
    return _store


def _get_projection(name: str):
    if _projections is None:
        return None
    return _projections.get(name)


def init_ledger_mcp(store, projections: dict = None):
    global _store, _projections
    _store = store
    _projections = projections or {}


if FastMCP:
    mcp = FastMCP(
        name="The Ledger",
        instructions="Event store and audit infrastructure for Apex Financial Services loan applications.",
    )
else:
    mcp = None


def _tool(desc: str):
    """Decorator helper: use mcp.tool when available, else no-op."""
    return (mcp.tool(description=desc) if mcp else (lambda f: f))


# ─── TOOLS (Commands) ─────────────────────────────────────────────────────────


@_tool("Submit a new loan application. Returns stream_id and initial version. Precondition: application_id must not already exist.")
async def submit_application(
    application_id: str,
    applicant_id: str,
    requested_amount_usd: float,
    loan_purpose: str,
) -> str:
    """Submit a new loan application."""
    from ledger.commands.handlers import handle_submit_application, SubmitApplicationCommand
    store = _get_store()
    try:
        cmd = SubmitApplicationCommand(
            application_id=application_id,
            applicant_id=applicant_id,
            requested_amount_usd=requested_amount_usd,
            loan_purpose=loan_purpose,
        )
        stream_id, ver = await handle_submit_application(cmd, store)
        return json.dumps({"stream_id": stream_id, "initial_version": ver})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e), "suggested_action": "retry_with_unique_application_id"})


@_tool("Start an agent session (Gas Town pattern). REQUIRED before record_credit_analysis, record_fraud_screening, or generate_decision. Call this first or you will get PreconditionFailed.")
async def start_agent_session(
    agent_type: str,
    session_id: str,
    application_id: str,
    model_version: str = "v1",
) -> str:
    """Start an agent session."""
    from ledger.commands.handlers import handle_start_agent_session, StartAgentSessionCommand
    store = _get_store()
    try:
        cmd = StartAgentSessionCommand(agent_type=agent_type, session_id=session_id, application_id=application_id, model_version=model_version)
        stream_id, pos = await handle_start_agent_session(cmd, store)
        return json.dumps({"session_id": session_id, "stream_id": stream_id, "context_position": pos})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e), "suggested_action": "use_unique_session_id"})


@_tool("Record credit analysis completion. REQUIRES active agent session from start_agent_session. May raise OptimisticConcurrencyError — suggested_action: reload_stream_and_retry.")
async def record_credit_analysis(
    application_id: str,
    agent_type: str,
    session_id: str,
    model_version: str,
    risk_tier: str,
    recommended_limit_usd: float,
    confidence: float,
    duration_ms: int = 0,
) -> str:
    """Record credit analysis result."""
    from ledger.commands.handlers import handle_credit_analysis_completed, CreditAnalysisCompletedCommand
    store = _get_store()
    try:
        cmd = CreditAnalysisCompletedCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            model_version=model_version,
            risk_tier=risk_tier,
            recommended_limit_usd=recommended_limit_usd,
            confidence=confidence,
            duration_ms=duration_ms,
            input_data_hash="hash",
        )
        stream_id, ver = await handle_credit_analysis_completed(cmd, store)
        return json.dumps({"event_id": "ok", "new_stream_version": ver})
    except Exception as e:
        err = {"error_type": type(e).__name__, "message": str(e)}
        if hasattr(e, "stream_id"):
            err["stream_id"] = e.stream_id
        if hasattr(e, "expected"):
            err["expected_version"] = e.expected
        if hasattr(e, "actual"):
            err["actual_version"] = e.actual
        err["suggested_action"] = "reload_stream_and_retry"
        return json.dumps(err)


@_tool("Record fraud screening completion. REQUIRES active agent session.")
async def record_fraud_screening(
    application_id: str,
    agent_type: str,
    session_id: str,
    fraud_score: float,
    model_version: str = "v1",
) -> str:
    """Record fraud screening result."""
    from ledger.commands.handlers import handle_fraud_screening_completed, FraudScreeningCompletedCommand
    store = _get_store()
    try:
        cmd = FraudScreeningCompletedCommand(
            application_id=application_id,
            agent_type=agent_type,
            session_id=session_id,
            fraud_score=fraud_score,
            anomaly_flags=[],
            model_version=model_version,
            input_data_hash="hash",
        )
        stream_id, ver = await handle_fraud_screening_completed(cmd, store)
        return json.dumps({"event_id": "ok", "new_stream_version": ver})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e)})


@_tool("Record compliance check result. rule_id must exist in regulation set (REG-001..REG-006). May raise DomainError.")
async def record_compliance_check(
    application_id: str,
    rule_id: str,
    passed: bool,
    is_hard_block: bool = False,
    failure_reason: str = "",
) -> str:
    """Record compliance check result."""
    from ledger.commands.handlers import handle_compliance_check, ComplianceCheckCommand
    store = _get_store()
    try:
        cmd = ComplianceCheckCommand(
            application_id=application_id,
            rule_id=rule_id,
            passed=passed,
            is_hard_block=is_hard_block,
            failure_reason=failure_reason or ("" if passed else f"Rule {rule_id} failed"),
        )
        stream_id, ver = await handle_compliance_check(cmd, store)
        return json.dumps({"event_id": "ok", "new_stream_version": ver})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e)})


@_tool("Generate decision. All analyses must be present; confidence < 0.6 forces REFER.")
async def generate_decision(
    application_id: str,
    recommendation: str,
    confidence: float,
    approved_amount_usd: float | None = None,
) -> str:
    """Generate decision (APPROVE/DECLINE/REFER)."""
    from ledger.commands.handlers import handle_generate_decision, GenerateDecisionCommand
    store = _get_store()
    try:
        cmd = GenerateDecisionCommand(
            application_id=application_id,
            recommendation=recommendation,
            confidence=confidence,
            approved_amount_usd=approved_amount_usd,
        )
        stream_id, ver = await handle_generate_decision(cmd, store)
        return json.dumps({"event_id": "ok", "new_stream_version": ver})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e)})


@_tool("Record human review. If override=True, override_reason is required.")
async def record_human_review(
    application_id: str,
    reviewer_id: str,
    final_decision: str,
    override: bool = False,
    override_reason: str = "",
) -> str:
    """Record human review completion."""
    from ledger.commands.handlers import handle_human_review_completed, HumanReviewCompletedCommand
    store = _get_store()
    try:
        cmd = HumanReviewCompletedCommand(
            application_id=application_id,
            reviewer_id=reviewer_id,
            final_decision=final_decision,
            override=override,
            override_reason=override_reason,
        )
        stream_id, ver = await handle_human_review_completed(cmd, store)
        return json.dumps({"event_id": "ok", "new_stream_version": ver})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e)})


@_tool("Run integrity check on audit chain. Compliance role only; rate limit 1/min per entity.")
async def run_integrity_check(entity_type: str, entity_id: str) -> str:
    from ledger.integrity.audit_chain import run_integrity_check as run_check
    store = _get_store()
    try:
        result = await run_check(store, entity_type, entity_id)
        return json.dumps({"chain_valid": result.chain_valid, "events_verified": result.events_verified})
    except Exception as e:
        return json.dumps({"error_type": type(e).__name__, "message": str(e)})


# ─── RESOURCE HELPERS (query via tools for MCP compatibility) ──────────────────

@_tool("Get application summary. Reads from ApplicationSummary projection. URI: ledger://applications/{id}")
async def get_application(id: str) -> str:
    """Application summary from projection."""
    proj = _get_projection("ApplicationSummary")
    if proj:
        row = proj.get(id)
        return json.dumps(row) if row else json.dumps({"error": "not_found"})
    return json.dumps({"error": "projection_not_initialized"})


@_tool("Ledger health and lag. URI: ledger://ledger/health")
async def get_health() -> str:
    """Daemon health."""
    return json.dumps({"status": "ok", "lags": {}})


@_tool("Get compliance audit for application. URI: ledger://applications/{id}/compliance. Use as_of=ISO8601 for temporal query.")
async def get_compliance(application_id: str, as_of: str | None = None) -> str:
    """Compliance audit from ComplianceAuditView projection."""
    proj = _get_projection("ComplianceAuditView") or _get_projection("ComplianceAudit")
    if not proj:
        return json.dumps({"error": "projection_not_initialized"})
    try:
        if as_of:
            from datetime import datetime
            ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            row = proj.get_compliance_at(application_id, ts)
        else:
            row = proj.get_current_compliance(application_id)
        return json.dumps(row) if row else json.dumps({"error": "not_found"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@_tool("Get audit trail for entity. URI: ledger://applications/{id}/audit-trail")
async def get_audit_trail(entity_type: str, entity_id: str) -> str:
    """Audit trail from stream."""
    store = _get_store()
    stream_id = f"audit-{entity_type}-{entity_id}"
    events = await store.load_stream(stream_id)
    return json.dumps([{"event_type": e.get("event_type"), "payload": e.get("payload", {})} for e in events])


@_tool("Get agent performance. URI: ledger://agents/{id}/performance")
async def get_agent_performance(agent_id: str, model_version: str | None = None) -> str:
    """Agent performance from AgentPerformanceLedger projection."""
    proj = _get_projection("AgentPerformanceLedger") or _get_projection("AgentPerformance")
    if not proj:
        return json.dumps({"error": "projection_not_initialized"})
    rows = proj.get(agent_id, model_version)
    return json.dumps(rows if rows else {"error": "not_found"})


@_tool("Get agent session. URI: ledger://agents/{id}/sessions/{session_id}")
async def get_agent_session(agent_type: str, session_id: str) -> str:
    """Agent session stream."""
    store = _get_store()
    stream_id = f"agent-{agent_type}-{session_id}"
    events = await store.load_stream(stream_id)
    return json.dumps([{"event_type": e.get("event_type"), "payload": e.get("payload", {})} for e in events])


def run():
    """Run the MCP server."""
    if mcp:
        mcp.run()

"""
ledger/projections/query_service.py — CQRS read-side facade

Queries read from projections (eventually consistent). Stream loads are used only
where justified (audit trail, agent session replay, integrity).
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ledger.projections.daemon import ProjectionDaemon


class LedgerQueryService:
    """
    Single entry point for read models + health (LLM/API friendly).

    Pass the same projection instances and optional daemon used by writers so
    query results reflect processed checkpoints.
    """

    def __init__(
        self,
        projections: dict[str, Any],
        daemon: "ProjectionDaemon | None" = None,
    ):
        self._projections = projections or {}
        self._daemon = daemon

    def get_application_summary(self, application_id: str) -> dict | None:
        p = self._projections.get("ApplicationSummary")
        if not p or not hasattr(p, "get"):
            return None
        return p.get(application_id)

    def get_compliance_current(self, application_id: str) -> dict | None:
        p = self._projections.get("ComplianceAuditView") or self._projections.get("ComplianceAudit")
        if not p or not hasattr(p, "get_current_compliance"):
            return None
        return p.get_current_compliance(application_id)

    def get_compliance_at(self, application_id: str, as_of) -> dict | None:
        p = self._projections.get("ComplianceAuditView") or self._projections.get("ComplianceAudit")
        if not p or not hasattr(p, "get_compliance_at"):
            return None
        return p.get_compliance_at(application_id, as_of)

    def get_agent_performance(self, agent_id: str, model_version: str | None = None):
        p = self._projections.get("AgentPerformanceLedger") or self._projections.get("AgentPerformance")
        if not p or not hasattr(p, "get"):
            return None
        return p.get(agent_id, model_version)

    async def health(self, store) -> dict:
        """Projection lag vs tail of `events` (global_position)."""
        lags_ms: dict[str, float] = {}
        if self._daemon:
            for name in self._daemon.projection_names:
                lags_ms[name] = await self._daemon.get_lag_ms(name, store)
        max_pos = -1
        if hasattr(store, "get_max_global_position"):
            max_pos = await store.get_max_global_position()
        return {
            "projection_lags_ms": lags_ms,
            "max_global_position": max_pos,
            "cqrs_note": "Reads are eventually consistent; use stream load for strong consistency on critical fields.",
        }

    @property
    def projection_names(self) -> list[str]:
        return list(self._projections.keys())

"""
ledger/projections/agent_performance.py — AgentPerformanceLedger projection
"""
from __future__ import annotations
from collections import defaultdict

from ledger.projections.daemon import Projection


class AgentPerformanceProjection(Projection):
    """Aggregated metrics per agent model version."""

    name = "AgentPerformanceLedger"

    def __init__(self):
        self._rows: dict[tuple[str, str], dict] = {}

    def _key(self, agent_id: str, model_version: str) -> tuple[str, str]:
        return (agent_id or "unknown", model_version or "unknown")

    async def handle(self, store, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        agent_id = p.get("agent_id") or p.get("agent_type") or "unknown"
        model_version = p.get("model_version") or "unknown"
        key = self._key(agent_id, model_version)
        row = self._rows.setdefault(key, {
            "agent_id": agent_id,
            "model_version": model_version,
            "analyses_completed": 0,
            "decisions_generated": 0,
            "avg_confidence_score": 0.0,
            "approve_rate": 0.0,
            "decline_rate": 0.0,
            "refer_rate": 0.0,
            "human_override_rate": 0.0,
        })
        if et == "CreditAnalysisCompleted":
            row["analyses_completed"] = row.get("analyses_completed", 0) + 1
            conf = p.get("decision", {})
            if isinstance(conf, dict):
                conf = conf.get("confidence", 0) or 0
            row["avg_confidence_score"] = (row["avg_confidence_score"] + float(conf)) / 2
        elif et == "DecisionGenerated":
            row["decisions_generated"] = row.get("decisions_generated", 0) + 1

    def get(self, agent_id: str, model_version: str | None = None) -> list[dict]:
        if model_version:
            return [self._rows[self._key(agent_id, model_version)]] if self._key(agent_id, model_version) in self._rows else []
        return [r for k, r in self._rows.items() if k[0] == agent_id]

    def rebuild_from_scratch(self) -> None:
        """Clear metrics so the projection can replay from scratch."""
        self._rows.clear()

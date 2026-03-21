"""
ledger/projections/compliance_audit.py — ComplianceAuditView projection

Supports temporal query: get_compliance_at(application_id, timestamp).
"""
from __future__ import annotations
from datetime import datetime
from collections import defaultdict

from ledger.projections.daemon import Projection


class ComplianceAuditProjection(Projection):
    """Regulatory read model — complete compliance record per application."""

    name = "ComplianceAuditView"

    def __init__(self):
        self._by_app: dict[str, list[dict]] = defaultdict(list)
        self._snapshots: dict[str, list[tuple[float, dict]]] = defaultdict(list)

    async def handle(self, store, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        app_id = p.get("application_id")
        if not app_id:
            return
        evt = {"event_type": et, "payload": p, "recorded_at": event.get("recorded_at")}
        self._by_app[app_id].append(evt)

    def get_current_compliance(self, application_id: str) -> dict:
        events = self._by_app.get(application_id, [])
        return {"application_id": application_id, "events": events}

    def get_compliance_at(self, application_id: str, timestamp: datetime) -> dict:
        events = self._by_app.get(application_id, [])
        ts = timestamp.timestamp() if hasattr(timestamp, "timestamp") else float(timestamp)
        filtered = [e for e in events if _parse_ts(e.get("recorded_at", 0)) <= ts]
        return {"application_id": application_id, "events": filtered, "as_of": timestamp}

    def rebuild_from_scratch(self) -> None:
        self._by_app.clear()


def _parse_ts(v) -> float:
    if hasattr(v, "timestamp"):
        return v.timestamp()
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0

"""
ledger/domain/aggregates/audit_ledger.py

AuditLedger aggregate — append-only cross-cutting audit trail.
Stream: audit-{entity_type}-{entity_id}
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AuditLedgerAggregate:
    entity_type: str
    entity_id: str
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @classmethod
    async def load(cls, store, entity_type: str, entity_id: str) -> "AuditLedgerAggregate":
        stream_id = f"audit-{entity_type}-{entity_id}"
        events = await store.load_stream(stream_id)
        agg = cls(entity_type=entity_type, entity_id=entity_id)
        for event in events:
            agg.apply(event)
        return agg

    def apply(self, event: dict) -> None:
        self.version = event.get("stream_position", self.version + 1)
        self.events.append(event)

"""
ledger/upcasters.py — UpcasterRegistry (canonical)

Upcasters transform old event versions to the current version ON READ.
They NEVER write to the events table. Immutability is non-negotiable.

Chains: CreditAnalysisCompleted v1→v2, DecisionGenerated v1→v2 (extend with TARGET_VERSION + steps).
"""
from __future__ import annotations

# Current catalogue version per event type (extend as schema evolves)
TARGET_EVENT_VERSION: dict[str, int] = {
    "CreditAnalysisCompleted": 2,
    "DecisionGenerated": 2,
}


class UpcasterRegistry:
    """Apply on load_stream() and load_all() — never on append()."""

    def __init__(self):
        self._upcasters: dict[str, dict[int, callable]] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator to register an upcaster: from_version -> from_version+1 payload shape."""

        def decorator(fn):
            self._upcasters.setdefault(event_type, {})[from_version] = fn
            return fn

        return decorator

    def upcast(self, event: dict) -> dict:
        """Apply registered upcasters, then built-ins, until target version reached."""
        e = dict(event)
        et = e.get("event_type")
        target = TARGET_EVENT_VERSION.get(et, e.get("event_version", 1))

        for _ in range(32):
            ver = e.get("event_version", 1)
            if ver >= target:
                break
            chain = self._upcasters.get(et, {})
            if ver in chain:
                e = dict(e)
                e["payload"] = chain[ver](dict(e.get("payload", {})))
                e["event_version"] = ver + 1
                continue
            e = self._builtin_upcast_one(e)
            if e.get("event_version", 1) == ver:
                break
        return e

    def _builtin_upcast_one(self, event: dict) -> dict:
        """Single-step built-in migration."""
        et = event.get("event_type")
        ver = event.get("event_version", 1)
        if et == "CreditAnalysisCompleted" and ver < 2:
            event = dict(event)
            event["event_version"] = 2
            p = dict(event.get("payload", {}))
            p.setdefault("model_version", "legacy-pre-2026")
            p.setdefault("confidence_score", None)
            p.setdefault("regulatory_basis", [])
            event["payload"] = p
            return event
        if et == "DecisionGenerated" and ver < 2:
            event = dict(event)
            event["event_version"] = 2
            p = dict(event.get("payload", {}))
            p.setdefault("model_versions", {})
            event["payload"] = p
            return event
        return event


def create_default_registry() -> UpcasterRegistry:
    """Returns UpcasterRegistry with built-in chains."""
    return UpcasterRegistry()

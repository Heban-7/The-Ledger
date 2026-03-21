"""
ledger/upcasters.py — UpcasterRegistry

Upcasters transform old event versions to the current version ON READ.
They NEVER write to the events table. Immutability is non-negotiable.

CreditAnalysisCompleted v1→v2: model_version (infer "legacy-pre-2026"), confidence_score (null), regulatory_basis ([])
DecisionGenerated v1→v2: model_versions={}
"""
from __future__ import annotations


class UpcasterRegistry:
    """Apply on load_stream() and load_all() — never on append()."""

    def __init__(self):
        self._upcasters: dict[str, dict[int, callable]] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator to register an upcaster function."""

        def decorator(fn):
            self._upcasters.setdefault(event_type, {})[from_version] = fn
            return fn

        return decorator

    def upcast(self, event: dict) -> dict:
        """Apply chain of upcasters until latest version reached."""
        et = event.get("event_type")
        ver = event.get("event_version", 1)
        chain = self._upcasters.get(et, {})

        # Use built-in upcasters if no custom ones registered
        if not chain:
            return self._builtin_upcast(event)

        while ver in chain:
            event = dict(event)
            event["payload"] = chain[ver](dict(event.get("payload", {})))
            ver += 1
            event["event_version"] = ver
        return event

    def _builtin_upcast(self, event: dict) -> dict:
        """Built-in upcasters for CreditAnalysisCompleted and DecisionGenerated."""
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

        if et == "DecisionGenerated" and ver < 2:
            event = dict(event)
            event["event_version"] = 2
            p = dict(event.get("payload", {}))
            p.setdefault("model_versions", {})
            event["payload"] = p

        return event


def create_default_registry() -> UpcasterRegistry:
    """Returns UpcasterRegistry with built-in upcasters (always applied)."""
    return UpcasterRegistry()

"""
ledger/domain/regulation.py — Catalog of regulation rule IDs (single source of truth for compliance commands).
"""
from __future__ import annotations

from ledger.exceptions import DomainError

# Mandatory / catalog rule identifiers (aligned with seed events and challenge spec)
REGULATION_RULE_IDS = frozenset(
    {"REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"}
)


def assert_valid_rule_id(rule_id: str) -> None:
    """Guard: rule_id must exist in the active regulation catalog."""
    if rule_id not in REGULATION_RULE_IDS:
        raise DomainError(f"rule_id {rule_id} not in regulation set", rule="invalid_rule_id")

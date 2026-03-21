"""
ledger/domain/aggregates/compliance_record.py

ComplianceRecord aggregate — tracks mandatory checks, regulation versions.
Stream: compliance-{application_id}
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ledger.exceptions import DomainError


@dataclass
class ComplianceRecordAggregate:
    application_id: str
    regulation_set_version: str = ""
    required_rules: list[str] = field(default_factory=list)
    rules_passed: set[str] = field(default_factory=set)
    rules_failed: set[str] = field(default_factory=set)
    has_hard_block: bool = False
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @classmethod
    async def load(cls, store, application_id: str) -> "ComplianceRecordAggregate":
        stream_id = f"compliance-{application_id}"
        events = await store.load_stream(stream_id)
        agg = cls(application_id=application_id)
        for event in events:
            agg.apply(event)
        return agg

    def apply(self, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        self.version = event.get("stream_position", self.version + 1)
        self.events.append(event)

        if et == "ComplianceCheckInitiated":
            self.regulation_set_version = p.get("regulation_set_version", "")
            self.required_rules = list(p.get("rules_to_evaluate", []))
        elif et == "ComplianceRulePassed":
            self.rules_passed.add(p.get("rule_id", ""))
        elif et == "ComplianceRuleFailed":
            rid = p.get("rule_id", "")
            self.rules_failed.add(rid)
            if p.get("is_hard_block"):
                self.has_hard_block = True
        elif et == "ComplianceCheckCompleted":
            pass

    def all_mandatory_checks_complete(self) -> bool:
        """True if all required rules have been evaluated (passed or failed)."""
        if not self.required_rules:
            return True
        evaluated = self.rules_passed | self.rules_failed
        return all(r in evaluated for r in self.required_rules)

    def assert_all_checks_complete(self) -> None:
        if not self.all_mandatory_checks_complete():
            missing = set(self.required_rules) - self.rules_passed - self.rules_failed
            raise DomainError(
                f"Mandatory compliance checks incomplete: {missing}",
                rule="mandatory_checks",
            )

    def assert_no_hard_block(self) -> None:
        if self.has_hard_block:
            raise DomainError("Compliance hard block present", rule="no_hard_block")

    def assert_ready_for_decision_generation(self) -> None:
        """All mandatory checks evaluated and no hard block — single guard for decision commands."""
        self.assert_all_checks_complete()
        self.assert_no_hard_block()

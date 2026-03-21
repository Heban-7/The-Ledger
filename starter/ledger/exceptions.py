"""
ledger/exceptions.py — Domain and store exceptions
"""
from __future__ import annotations


class OptimisticConcurrencyError(Exception):
    """Raised when expected_version doesn't match current stream version."""

    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"OCC on '{stream_id}': expected v{expected}, actual v{actual}")

    def to_structured(self) -> dict:
        """Structured error for LLM consumption."""
        return {
            "error_type": "OptimisticConcurrencyError",
            "message": str(self),
            "stream_id": self.stream_id,
            "expected_version": self.expected,
            "actual_version": self.actual,
            "suggested_action": "reload_stream_and_retry",
        }


class DomainError(Exception):
    """Raised when a business rule is violated."""

    def __init__(self, message: str, rule: str | None = None):
        self.rule = rule
        super().__init__(message)

    def to_structured(self) -> dict:
        return {
            "error_type": "DomainError",
            "message": str(self),
            "rule": self.rule,
        }


class PreconditionFailedError(Exception):
    """Raised when a tool precondition is not met (e.g. no active agent session)."""

    def __init__(self, message: str, precondition: str | None = None):
        self.precondition = precondition
        super().__init__(message)

    def to_structured(self) -> dict:
        return {
            "error_type": "PreconditionFailed",
            "message": str(self),
            "precondition": self.precondition,
        }

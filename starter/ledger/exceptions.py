"""
ledger/exceptions.py — Domain and store exceptions

The exceptions themselves are classic Python exceptions, but they also expose
a typed Pydantic representation via `to_structured()` for MCP/LLM retry logic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class StructuredErrorBase(BaseModel):
    """Common stable error envelope used by MCP and LLM clients."""

    model_config = ConfigDict(extra="forbid")

    error_type: str
    message: str
    suggested_action: str | None = None


class OptimisticConcurrencyErrorDetails(StructuredErrorBase):
    error_type: Literal["OptimisticConcurrencyError"] = "OptimisticConcurrencyError"
    stream_id: str
    expected_version: int
    actual_version: int
    suggested_action: Literal["reload_stream_and_retry"] = "reload_stream_and_retry"


class DomainErrorDetails(StructuredErrorBase):
    error_type: Literal["DomainError"] = "DomainError"
    rule: str | None = None


class PreconditionFailedErrorDetails(StructuredErrorBase):
    error_type: Literal["PreconditionFailed"] = "PreconditionFailed"
    precondition: str | None = None


class OptimisticConcurrencyError(Exception):
    """Raised when expected_version doesn't match current stream version."""

    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"OCC on '{stream_id}': expected v{expected}, actual v{actual}")

    def to_structured(self) -> dict:
        """Structured error for LLM consumption."""
        return OptimisticConcurrencyErrorDetails(
            message=str(self),
            stream_id=self.stream_id,
            expected_version=self.expected,
            actual_version=self.actual,
        ).model_dump(exclude_none=True)


class DomainError(Exception):
    """Raised when a business rule is violated."""

    def __init__(self, message: str, rule: str | None = None):
        self.rule = rule
        super().__init__(message)

    def to_structured(self) -> dict:
        return DomainErrorDetails(message=str(self), rule=self.rule).model_dump(exclude_none=True)


class PreconditionFailedError(Exception):
    """Raised when a tool precondition is not met (e.g. no active agent session)."""

    def __init__(self, message: str, precondition: str | None = None):
        self.precondition = precondition
        super().__init__(message)

    def to_structured(self) -> dict:
        return PreconditionFailedErrorDetails(message=str(self), precondition=self.precondition).model_dump(
            exclude_none=True
        )

"""
ledger/models/events.py — stored row models

These models represent events and stream metadata as loaded from storage.
They are used for typed boundaries and stable serialization, and are
intentionally independent from the canonical domain event payload schema
in `ledger/schema/events.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BaseEvent(BaseModel):
    """Shared shape for event records."""

    model_config = ConfigDict(from_attributes=True)

    event_id: UUID | str
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime | str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Stable dict for existing callers and JSON serialization."""
        return {
            "event_id": str(self.event_id),
            "stream_id": self.stream_id,
            "stream_position": self.stream_position,
            "global_position": self.global_position,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "payload": self.payload,
            "metadata": self.metadata,
            "recorded_at": self.recorded_at,
        }


class StoredEvent(BaseEvent):
    """Event as loaded from the store (with optional upcasting applied)."""

    @classmethod
    def from_row(cls, row: dict) -> "StoredEvent":
        payload = row.get("payload", {})
        metadata = row.get("metadata", {}) or {}
        return cls(
            event_id=row.get("event_id"),
            stream_id=row["stream_id"],
            stream_position=row["stream_position"],
            global_position=row.get("global_position", 0),
            event_type=row["event_type"],
            event_version=row.get("event_version", 1),
            payload=dict(payload) if hasattr(payload, "items") else payload,
            metadata=dict(metadata) if hasattr(metadata, "items") else metadata,
            recorded_at=row.get("recorded_at"),
        )


class StreamMetadata(BaseModel):
    """Metadata for an event stream."""

    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime | str | None = None
    archived_at: datetime | str | None = None
    metadata: dict[str, Any] = {}

    @classmethod
    def from_row(cls, row: dict) -> "StreamMetadata":
        md = row.get("metadata") or {}
        return cls(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row.get("created_at"),
            archived_at=row.get("archived_at"),
            metadata=dict(md) if hasattr(md, "items") else md,
        )

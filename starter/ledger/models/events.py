"""
ledger/models/events.py — StoredEvent and StreamMetadata
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class StoredEvent:
    """Event as loaded from the store (with optional upcasting applied)."""

    event_id: UUID | str
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime | str

    @classmethod
    def from_row(cls, row: dict) -> "StoredEvent":
        return cls(
            event_id=row.get("event_id"),
            stream_id=row["stream_id"],
            stream_position=row["stream_position"],
            global_position=row.get("global_position", 0),
            event_type=row["event_type"],
            event_version=row.get("event_version", 1),
            payload=dict(row["payload"]) if hasattr(row["payload"], "items") else row["payload"],
            metadata=dict(row["metadata"]) if hasattr(row["metadata"], "items") else row.get("metadata", {}),
            recorded_at=row.get("recorded_at"),
        )

    def to_dict(self) -> dict:
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


@dataclass
class StreamMetadata:
    """Metadata for an event stream."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime | str
    archived_at: datetime | str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict) -> "StreamMetadata":
        return cls(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row.get("created_at"),
            archived_at=row.get("archived_at"),
            metadata=dict(row["metadata"]) if hasattr(row.get("metadata"), "items") else (row.get("metadata") or {}),
        )

"""
ledger/event_store.py — PostgreSQL-backed EventStore
"""
from __future__ import annotations
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID, uuid4

import asyncpg

from ledger.exceptions import OptimisticConcurrencyError
from ledger.models.events import StreamMetadata

# Re-export for backward compatibility
__all__ = ["EventStore", "InMemoryEventStore", "OptimisticConcurrencyError"]

# UpcasterRegistry is in ledger.upcasters — import when needed to avoid circular deps


class EventStore:
    """
    Append-only PostgreSQL event store. All agents and projections use this class.
    """

    def __init__(self, db_url: str, upcaster_registry=None):
        self.db_url = db_url
        self.upcasters = upcaster_registry
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.db_url, min_size=2, max_size=10)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def stream_version(self, stream_id: str) -> int:
        """Returns current version, or -1 if stream doesn't exist."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
            return int(row["current_version"]) if row else -1

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[int]:
        """Appends events atomically with OCC. Writes to outbox in same transaction. Returns list of positions assigned."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")

        meta = dict(metadata or {})
        if causation_id:
            meta["causation_id"] = causation_id
        if correlation_id:
            meta["correlation_id"] = correlation_id

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT current_version FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                    stream_id,
                )
                current = int(row["current_version"]) if row else -1
                if current != expected_version:
                    raise OptimisticConcurrencyError(stream_id, expected_version, current)

                if row is None:
                    agg_type = stream_id.split("-")[0] if "-" in stream_id else "unknown"
                    await conn.execute(
                        "INSERT INTO event_streams(stream_id, aggregate_type, current_version) VALUES($1, $2, 0)",
                        stream_id,
                        agg_type,
                    )

                positions = []
                now = datetime.now(timezone.utc)
                for i, event in enumerate(events):
                    pos = expected_version + 1 + i
                    event_id = uuid4()
                    await conn.execute(
                        "INSERT INTO events(event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at) "
                        "VALUES($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
                        event_id,
                        stream_id,
                        pos,
                        event.get("event_type", "Unknown"),
                        event.get("event_version", 1),
                        json.dumps(event.get("payload", {})),
                        json.dumps(meta),
                        now,
                    )
                    await conn.execute(
                        "INSERT INTO outbox(event_id, destination, payload, created_at) VALUES($1, $2, $3::jsonb, $4)",
                        event_id,
                        "projections",
                        json.dumps({"event_id": str(event_id), "stream_id": stream_id, "stream_position": pos, "event_type": event.get("event_type"), "payload": event.get("payload", {})}),
                        now,
                    )
                    positions.append(pos)

                await conn.execute(
                    "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
                    expected_version + len(events),
                    stream_id,
                )
                return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[dict]:
        """Loads events from a stream in stream_position order. Applies upcasters if set."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")

        q = (
            "SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at "
            "FROM events WHERE stream_id = $1 AND stream_position >= $2"
        )
        params: list = [stream_id, from_position]
        if to_position is not None:
            q += " AND stream_position <= $3"
            params.append(to_position)
        q += " ORDER BY stream_position ASC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(q, *params)
            result = []
            for row in rows:
                e = {
                    "event_id": row["event_id"],
                    "stream_id": row["stream_id"],
                    "stream_position": row["stream_position"],
                    "global_position": row["global_position"],
                    "event_type": row["event_type"],
                    "event_version": row["event_version"],
                    "payload": dict(row["payload"]),
                    "metadata": dict(row["metadata"]),
                    "recorded_at": row["recorded_at"],
                }
                if self.upcasters:
                    e = self.upcasters.upcast(e)
                result.append(e)
            return result

    async def load_all(
        self,
        from_global_position: int = 0,
        from_position: int | None = None,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncGenerator[dict, None]:
        """Async generator yielding events by global_position. Used by ProjectionDaemon."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        pos = from_position if from_position is not None else from_global_position
        while True:
            if event_types:
                q = (
                    "SELECT global_position, event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at "
                    "FROM events WHERE global_position > $1 AND event_type = ANY($2::text[]) "
                    "ORDER BY global_position ASC LIMIT $3"
                )
                params: list = [pos, event_types, batch_size]
            else:
                q = (
                    "SELECT global_position, event_id, stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at "
                    "FROM events WHERE global_position > $1 ORDER BY global_position ASC LIMIT $2"
                )
                params = [pos, batch_size]

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(q, *params)
            if not rows:
                break
            for row in rows:
                e = {
                    "global_position": row["global_position"],
                    "event_id": row["event_id"],
                    "stream_id": row["stream_id"],
                    "stream_position": row["stream_position"],
                    "event_type": row["event_type"],
                    "event_version": row["event_version"],
                    "payload": dict(row["payload"]),
                    "metadata": dict(row["metadata"]),
                    "recorded_at": row["recorded_at"],
                }
                if self.upcasters:
                    e = self.upcasters.upcast(e)
                yield e
            pos = rows[-1]["global_position"]
            if len(rows) < batch_size:
                break

    async def get_event(self, event_id: UUID | str) -> dict | None:
        """Loads one event by UUID."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM events WHERE event_id = $1", event_id)
            if not row:
                return None
            e = dict(row)
            e["payload"] = dict(row["payload"])
            e["metadata"] = dict(row["metadata"])
            if self.upcasters:
                e = self.upcasters.upcast(e)
            return e

    async def archive_stream(self, stream_id: str) -> None:
        """Marks a stream as archived."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        now = datetime.now(timezone.utc)
        await self._pool.execute(
            "UPDATE event_streams SET archived_at = $1 WHERE stream_id = $2",
            now,
            stream_id,
        )

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata | None:
        """Returns stream metadata, or None if stream doesn't exist."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
            if not row:
                return None
            return StreamMetadata.from_row(dict(row))

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        """Save projection checkpoint."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        now = datetime.now(timezone.utc)
        await self._pool.execute(
            "INSERT INTO projection_checkpoints(projection_name, last_position, updated_at) VALUES($1, $2, $3) "
            "ON CONFLICT (projection_name) DO UPDATE SET last_position = $2, updated_at = $3",
            projection_name,
            position,
            now,
        )

    async def load_checkpoint(self, projection_name: str) -> int:
        """Load projection checkpoint. Returns -1 if none (process from start)."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        row = await self._pool.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            projection_name,
        )
        return int(row["last_position"]) if row else -1

    async def get_max_global_position(self) -> int:
        """Returns the highest global_position in events. For lag calculation."""
        if not self._pool:
            raise RuntimeError("EventStore not connected")
        row = await self._pool.fetchrow("SELECT COALESCE(MAX(global_position), 0) AS mx FROM events")
        return int(row["mx"]) if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# InMemoryEventStore — for tests only
# ─────────────────────────────────────────────────────────────────────────────


class InMemoryEventStore:
    """
    In-memory event store for unit tests. Same interface as EventStore.
    Supports upcasters and projection checkpoints. Uses asyncio locks for OCC.
    """

    def __init__(self, upcaster_registry=None):
        self.upcasters = upcaster_registry
        self._streams: dict[str, list[dict]] = defaultdict(list)
        self._versions: dict[str, int] = {}
        self._global: list[dict] = []
        self._checkpoints: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def stream_version(self, stream_id: str) -> int:
        return self._versions.get(stream_id, -1)

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[int]:
        async with self._locks[stream_id]:
            current = self._versions.get(stream_id, -1)
            if current != expected_version:
                raise OptimisticConcurrencyError(stream_id, expected_version, current)

            meta = dict(metadata or {})
            if causation_id:
                meta["causation_id"] = causation_id
            if correlation_id:
                meta["correlation_id"] = correlation_id

            positions = []
            for i, event in enumerate(events):
                pos = current + 1 + i
                stored = {
                    "event_id": str(uuid4()),
                    "stream_id": stream_id,
                    "stream_position": pos,
                    # 1-based sequence (matches PostgreSQL SERIAL on `events.global_position`)
                    "global_position": len(self._global) + 1,
                    "event_type": event.get("event_type", "Unknown"),
                    "event_version": event.get("event_version", 1),
                    "payload": dict(event.get("payload", {})),
                    "metadata": meta,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                self._streams[stream_id].append(stored)
                self._global.append(stored)
                positions.append(pos)

            self._versions[stream_id] = current + len(events)
            return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[dict]:
        events = [
            e
            for e in self._streams.get(stream_id, [])
            if e["stream_position"] >= from_position
            and (to_position is None or e["stream_position"] <= to_position)
        ]
        events = sorted(events, key=lambda x: x["stream_position"])
        if self.upcasters:
            return [self.upcasters.upcast(dict(e)) for e in events]
        return [dict(e) for e in events]

    async def load_all(
        self,
        from_global_position: int = 0,
        from_position: int | None = None,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncGenerator[dict, None]:
        start = from_position if from_position is not None else from_global_position
        # Match PostgreSQL: WHERE global_position > $checkpoint (exclusive) so checkpoints do not double-process.
        for e in self._global:
            if e["global_position"] > start:
                if event_types and e.get("event_type") not in event_types:
                    continue
                ev = dict(e)
                if self.upcasters:
                    ev = self.upcasters.upcast(ev)
                yield ev

    async def get_event(self, event_id: str | UUID) -> dict | None:
        eid = str(event_id)
        for e in self._global:
            if str(e["event_id"]) == eid:
                ev = dict(e)
                if self.upcasters:
                    ev = self.upcasters.upcast(ev)
                return ev
        return None

    async def archive_stream(self, stream_id: str) -> None:
        pass  # No-op for in-memory

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata | None:
        if stream_id not in self._versions:
            return None
        return StreamMetadata(
            stream_id=stream_id,
            aggregate_type=stream_id.split("-")[0] if "-" in stream_id else "unknown",
            current_version=self._versions[stream_id],
            created_at=datetime.now(timezone.utc).isoformat(),
            archived_at=None,
            metadata={},
        )

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        self._checkpoints[projection_name] = position

    async def load_checkpoint(self, projection_name: str) -> int:
        return self._checkpoints.get(projection_name, -1)

    async def get_max_global_position(self) -> int:
        return len(self._global) if self._global else 0

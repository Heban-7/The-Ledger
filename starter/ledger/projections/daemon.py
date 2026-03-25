"""
ledger/projections/daemon.py — Async Projection Daemon (CQRS read-side)

Polls `events` by global_position, routes to projections, updates checkpoints.
Fault-tolerant: configurable error policy; projection lag exposed for SLO monitoring.

Checkpoint semantics: `load_checkpoint` returns last processed global_position; `load_all`
must yield only events with global_position **greater than** checkpoint (exclusive),
matching PostgreSQL EventStore implementation.
"""
from __future__ import annotations
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from enum import Enum

logger = logging.getLogger(__name__)

# Heuristic: approximate ms lag from event backlog (tune per deployment)
_MS_PER_BACKLOG_EVENT = 2.0


class ProjectionErrorPolicy(str, Enum):
    """What to do when a projection handler raises."""

    RAISE = "raise"  # stop batch; checkpoint not advanced for failed event
    SKIP = "skip"  # log, advance checkpoint (poison-pill handling; dev only)


class Projection(ABC):
    """Base class for projections."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def handle(self, store, event: dict) -> None:
        """Process one event."""
        ...


class ProjectionDaemon:
    def __init__(
        self,
        store,
        projections: list[Projection],
        poll_interval_ms: int = 100,
        retry_count: int = 3,
        on_handler_error: ProjectionErrorPolicy = ProjectionErrorPolicy.RAISE,
    ):
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._poll_interval_ms = poll_interval_ms
        self._retry_count = retry_count
        self._on_handler_error = on_handler_error
        self._running = False
        self._last_position: dict[str, int] = {}

    @property
    def projection_names(self) -> list[str]:
        return list(self._projections.keys())

    async def run_forever(self) -> None:
        """Run daemon until stopped."""
        self._running = True
        while self._running:
            try:
                await self._process_batch()
            except Exception as e:
                logger.exception("ProjectionDaemon batch error: %s", e)
            await asyncio.sleep(self._poll_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    async def _process_batch(self) -> None:
        """Process one batch of events for all projections."""
        for proj_name, proj in self._projections.items():
            try:
                checkpoint = await self._store.load_checkpoint(proj_name)
                batch: list[dict] = []
                async for event in self._store.load_all(from_global_position=checkpoint, batch_size=100):
                    batch.append(event)

                for event in batch:
                    try:
                        await proj.handle(self._store, event)
                        pos = event.get("global_position", checkpoint)
                        self._last_position[proj_name] = pos
                        await self._store.save_checkpoint(proj_name, pos)
                    except Exception as e:
                        logger.error(
                            "Projection %s failed on event %s: %s",
                            proj_name,
                            event.get("event_id"),
                            e,
                        )
                        if self._on_handler_error == ProjectionErrorPolicy.SKIP:
                            pos = event.get("global_position", checkpoint)
                            self._last_position[proj_name] = pos
                            await self._store.save_checkpoint(proj_name, pos)
                            continue
                        break
            except Exception as e:
                logger.error("Projection %s batch failed: %s", proj_name, e)

    async def get_lag_ms(self, projection_name: str, store=None) -> float:
        """
        Approximate lag in ms from backlog of unprocessed global positions.
        Checkpoint stores last processed `global_position`; tail is `get_max_global_position()`.
        """
        st = await self.get_projection_status(projection_name, store)
        return float(st["estimated_lag_ms"])

    async def get_projection_status(self, projection_name: str, store=None) -> dict:
        """Structured lag + checkpoint for LLM/ops dashboards."""
        store = store or self._store
        chk = await store.load_checkpoint(projection_name)
        last = self._last_position.get(projection_name, chk)
        max_pos = await store.get_max_global_position() if hasattr(store, "get_max_global_position") else 0
        # Checkpoint -1 means never processed; otherwise last is last processed global_position (exclusive scan uses > last).
        if last < 0:
            behind = max_pos
        else:
            behind = max(0, max_pos - last)
        return {
            "projection_name": projection_name,
            "checkpoint_global_position": chk,
            "last_processed_global_position": last,
            "tail_global_position": max_pos,
            "events_behind": behind,
            "estimated_lag_ms": float(behind * _MS_PER_BACKLOG_EVENT),
        }

    async def health_snapshot(self, store=None) -> dict:
        store = store or self._store
        out = {"projections": {}, "tail_global_position": None}
        if hasattr(store, "get_max_global_position"):
            out["tail_global_position"] = await store.get_max_global_position()
        for name in self.projection_names:
            out["projections"][name] = await self.get_projection_status(name, store)
        return out

    def get_lag(self, projection_name: str) -> float:
        """Deprecated: sync wrapper; prefer get_lag_ms."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return 0.0
            return loop.run_until_complete(self.get_lag_ms(projection_name))
        except Exception:
            return 0.0


async def run_daemon_once(store, projections: list[Projection]) -> None:
    """Run one batch for testing."""
    daemon = ProjectionDaemon(store, projections)
    await daemon._process_batch()

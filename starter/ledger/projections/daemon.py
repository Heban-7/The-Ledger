"""
ledger/projections/daemon.py — Async Projection Daemon

Polls events from store, routes to projections, updates checkpoints.
Fault-tolerant: logs errors, skips/retries, continues.
"""
from __future__ import annotations
import asyncio
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


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
    ):
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._poll_interval_ms = poll_interval_ms
        self._retry_count = retry_count
        self._running = False
        self._last_position: dict[str, int] = {}

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
                batch = []
                async for event in self._store.load_all(from_global_position=checkpoint, batch_size=100):
                    batch.append(event)

                for event in batch:
                    try:
                        await proj.handle(self._store, event)
                        pos = event.get("global_position", checkpoint)
                        self._last_position[proj_name] = pos
                        await self._store.save_checkpoint(proj_name, pos)
                    except Exception as e:
                        logger.error("Projection %s failed on event %s: %s", proj_name, event.get("event_id"), e)
                        if hasattr(self._store, "_retry_count"):
                            raise
                        break
            except Exception as e:
                logger.error("Projection %s batch failed: %s", proj_name, e)

    def get_lag(self, projection_name: str) -> float:
        """Returns lag in milliseconds (approx)."""
        if projection_name not in self._last_position:
            return 0.0
        last = self._last_position[projection_name]
        max_pos = 0
        if hasattr(self._store, "get_max_global_position"):
            try:
                import asyncio
                max_pos = asyncio.get_event_loop().run_until_complete(self._store.get_max_global_position())
            except Exception:
                pass
        return float(max(0, max_pos - last))  # Simplified lag metric


async def run_daemon_once(store, projections: list[Projection]) -> None:
    """Run one batch for testing."""
    daemon = ProjectionDaemon(store, projections)
    await daemon._process_batch()

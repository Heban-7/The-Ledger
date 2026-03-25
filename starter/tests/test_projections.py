"""
tests/test_projections.py — Projection daemon tests
"""
import pytest

from ledger.event_store import InMemoryEventStore
from ledger.projections import ProjectionDaemon, ApplicationSummaryProjection
from ledger.projections.daemon import ProjectionErrorPolicy


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_projection_daemon_processes_events():
    store = InMemoryEventStore()
    proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store, [proj])

    await store.append("loan-APP-001", [_ev("ApplicationSubmitted", application_id="APP-001", applicant_id="C1", requested_amount_usd=100000)], expected_version=-1)
    await daemon._process_batch()

    row = proj.get("APP-001")
    assert row is not None
    assert row["state"] == "SUBMITTED"
    assert row["applicant_id"] == "C1"


@pytest.mark.asyncio
async def test_projection_daemon_high_load_catches_up_with_zero_lag():
    store = InMemoryEventStore()
    proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store, [proj], poll_interval_ms=1)

    total_events = 1200
    for i in range(total_events):
        app_id = f"APP-HIGH-{i:04d}"
        await store.append(
            f"loan-{app_id}",
            [_ev("ApplicationSubmitted", application_id=app_id, applicant_id=f"C{i}", requested_amount_usd=1000 + i)],
            expected_version=-1,
        )

    pre = await daemon.get_projection_status("ApplicationSummary")
    assert pre["events_behind"] == total_events
    assert pre["checkpoint_global_position"] == -1
    assert pre["tail_global_position"] == total_events

    post = None
    max_cycles = 20
    for _ in range(max_cycles):
        await daemon._process_batch()
        post = await daemon.get_projection_status("ApplicationSummary")
        if post["events_behind"] == 0:
            break

    assert post is not None
    assert post["events_behind"] == 0
    assert post["estimated_lag_ms"] == 0.0
    assert post["checkpoint_global_position"] == total_events
    assert proj.get("APP-HIGH-0000")["state"] == "SUBMITTED"
    assert proj.get(f"APP-HIGH-{total_events - 1:04d}")["state"] == "SUBMITTED"


class _FlakyProjection(ApplicationSummaryProjection):
    name = "FlakyProjection"

    def __init__(self):
        super().__init__()
        self.failed_once = False

    async def handle(self, store, event: dict) -> None:
        app_id = (event.get("payload") or {}).get("application_id")
        if app_id == "APP-CRASH-2" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("simulated crash during projection")
        await super().handle(store, event)


@pytest.mark.asyncio
async def test_projection_daemon_recovers_from_crash_and_resumes_from_checkpoint():
    store = InMemoryEventStore()
    proj = _FlakyProjection()
    daemon = ProjectionDaemon(store, [proj], on_handler_error=ProjectionErrorPolicy.RAISE)

    for i in range(5):
        app_id = f"APP-CRASH-{i}"
        await store.append(
            f"loan-{app_id}",
            [_ev("ApplicationSubmitted", application_id=app_id, applicant_id=f"C{i}", requested_amount_usd=2000 + i)],
            expected_version=-1,
        )

    await daemon._process_batch()
    checkpoint_after_crash = await store.load_checkpoint("FlakyProjection")
    assert checkpoint_after_crash < 5
    assert proj.get("APP-CRASH-0") is not None
    assert proj.get("APP-CRASH-2") is None

    await daemon._process_batch()
    checkpoint_after_recovery = await store.load_checkpoint("FlakyProjection")
    assert checkpoint_after_recovery == 5
    for i in range(5):
        assert proj.get(f"APP-CRASH-{i}")["state"] == "SUBMITTED"

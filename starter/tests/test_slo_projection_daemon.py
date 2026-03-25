"""
SLO-style projection daemon stress test

Goals:
- Many concurrent writers while the projection daemon keeps up.
- Explicit lag assertions (events_behind must converge to 0).
- rebuild_from_scratch exercised under live load (clear projection + reset checkpoint).
"""

import asyncio

import pytest

from ledger.event_store import InMemoryEventStore
from ledger.projections import ApplicationSummaryProjection
from ledger.projections.daemon import ProjectionDaemon


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_slo_projection_daemon_high_concurrency_rebuild_from_scratch():
    store = InMemoryEventStore()
    proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store, [proj], poll_interval_ms=1)

    n_streams = 100
    events_per_stream = 5
    total_events = n_streams * events_per_stream

    async def _writer(i: int) -> None:
        app_id = f"APP-SLO-{i:04d}"
        stream_id = f"loan-{app_id}"
        expected = await store.stream_version(stream_id)  # should be -1 initially
        await store.append(
            stream_id,
            [
                _ev(
                    "ApplicationSubmitted",
                    application_id=app_id,
                    applicant_id=f"C{i:04d}",
                    requested_amount_usd=1000 + i,
                ),
                _ev("DocumentUploadRequested", application_id=app_id),
                _ev("DocumentUploaded", application_id=app_id),
                _ev("CreditAnalysisRequested", application_id=app_id),
                _ev(
                    "CreditAnalysisCompleted",
                    application_id=app_id,
                    decision={"risk_tier": "HIGH"},
                ),
            ],
            expected_version=expected,
        )

    writers = [asyncio.create_task(_writer(i)) for i in range(n_streams)]
    sample_app_after_rebuild = "APP-SLO-0042"

    rebuilt = False
    cycle = 0
    max_cycles = 60
    prev_events_behind = None

    while True:
        cycle += 1
        await daemon._process_batch()

        status = await daemon.get_projection_status("ApplicationSummary")
        events_behind = status["events_behind"]
        assert status["estimated_lag_ms"] == float(events_behind * 2.0)

        all_writers_done = all(t.done() for t in writers)
        if not rebuilt and cycle >= 3 and not all_writers_done:
            # Production-like rebuild: clear read model + reset checkpoint, while writes continue.
            proj.rebuild_from_scratch()
            await store.reset_checkpoint("ApplicationSummary")
            rebuilt = True
            assert proj.get(sample_app_after_rebuild) is None

        if all_writers_done:
            if prev_events_behind is not None:
                assert events_behind <= prev_events_behind
            prev_events_behind = events_behind

            if events_behind == 0:
                break

        if cycle >= max_cycles:
            pytest.fail(
                f"Projection daemon did not catch up: events_behind={events_behind} after {cycle} cycles "
                f"(expected 0, tail={status['tail_global_position']})"
            )

        await asyncio.sleep(0.001)

    # Post-conditions: projection matches the latest lifecycle events.
    for i in (0, n_streams // 2, n_streams - 1):
        app_id = f"APP-SLO-{i:04d}"
        row = proj.get(app_id)
        assert row is not None
        assert row["state"] == "CREDIT_ANALYSIS_COMPLETE"
        assert row["risk_tier"] == "HIGH"
        assert row["global_position"] is not None


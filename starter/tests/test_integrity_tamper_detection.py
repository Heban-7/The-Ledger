"""
Cryptographic integrity tamper-detection tests

We run audit integrity checks, mutate stored event payloads, and assert:
- tamper_detected becomes True
- full replay hash validation is consistent end-to-end
"""

import pytest

from ledger.event_store import InMemoryEventStore
from ledger.integrity.audit_chain import run_integrity_check, verify_audit_stream_full_replay


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_integrity_check_detects_payload_tampering_via_full_replay_hash():
    store = InMemoryEventStore()
    entity_type = "loan"
    entity_id = "APP-TAMPER-1"
    stream_id = f"audit-{entity_type}-{entity_id}"

    await store.append(
        stream_id,
        [
            _ev("AuditRuleEvaluated", rule_id="R1", verdict="PASS"),
            _ev("AuditRuleEvaluated", rule_id="R2", verdict="PASS"),
        ],
        expected_version=-1,
    )

    r1 = await run_integrity_check(store, entity_type=entity_type, entity_id=entity_id)
    assert r1.tamper_detected is False
    assert r1.chain_valid is True

    baseline_verify = await verify_audit_stream_full_replay(store, entity_type=entity_type, entity_id=entity_id)
    assert baseline_verify["full_replay_integrity_hash"] == r1.full_replay_integrity_hash

    # Mutate a non-checkpoint event payload (simulated stored tampering).
    mutated = False
    for e in store._global:
        if e.get("stream_id") != stream_id:
            continue
        if e.get("event_type") == "AuditIntegrityCheckRun":
            continue
        if (e.get("payload") or {}).get("rule_id") == "R1":
            e["payload"]["verdict"] = "FAIL"
            mutated = True
            break
    assert mutated, "Expected audit event to mutate not found"

    r2 = await run_integrity_check(store, entity_type=entity_type, entity_id=entity_id)
    assert r2.tamper_detected is True
    assert r2.chain_valid is False
    assert r2.full_replay_integrity_hash != r1.full_replay_integrity_hash

    verify2 = await verify_audit_stream_full_replay(store, entity_type=entity_type, entity_id=entity_id)
    assert verify2["full_replay_integrity_hash"] == r2.full_replay_integrity_hash

    # End-to-end: the last checkpoint event payload must carry the tamper flag.
    events = await store.load_stream(stream_id)
    last_cp = next(e for e in reversed(events) if e.get("event_type") == "AuditIntegrityCheckRun")
    assert last_cp.get("payload", {}).get("tamper_detected") is True


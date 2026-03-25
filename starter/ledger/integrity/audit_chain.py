"""
ledger/integrity/audit_chain.py — Cryptographic audit chain

Each event contributes a canonical fingerprint (stream, position, type, version, payload).
Rolling SHA-256 detects tampering with any field. AuditIntegrityCheckRun checkpoints the chain.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone


def canonical_event_fingerprint(event: dict) -> bytes:
    """Stable bytes for one stored event."""
    payload = event.get("payload", {})
    if hasattr(payload, "items"):
        payload = dict(payload)
    canonical = {
        "stream_id": event.get("stream_id", ""),
        "stream_position": event.get("stream_position", -1),
        "event_type": event.get("event_type", ""),
        "event_version": event.get("event_version", 1),
        "payload": payload,
    }
    return json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")


def rolling_hash(previous_hash: str | None, event: dict) -> str:
    h = hashlib.sha256()
    if previous_hash:
        h.update(previous_hash.encode("utf-8"))
    h.update(canonical_event_fingerprint(event))
    return h.hexdigest()


@dataclass
class IntegrityCheckResult:
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    full_replay_integrity_hash: str
    previous_hash: str | None


async def run_integrity_check(
    store,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    Incremental hash over non-checkpoint events since last AuditIntegrityCheckRun, then append new run.
    """
    stream_id = f"audit-{entity_type}-{entity_id}"
    events = await store.load_stream(stream_id)

    previous_hash = None
    previous_full_replay: str | None = None
    events_to_hash: list[dict] = []
    for ev in events:
        if ev.get("event_type") == "AuditIntegrityCheckRun":
            p = ev.get("payload", {})
            previous_hash = p.get("integrity_hash")
            previous_full_replay = p.get("full_replay_integrity_hash")
            events_to_hash = []
        else:
            events_to_hash.append(ev)

    running = previous_hash
    for ev in events_to_hash:
        running = rolling_hash(running, ev)
    if running is None:
        new_hash = hashlib.sha256(b"").hexdigest()
    elif not events_to_hash and previous_hash:
        new_hash = previous_hash
    else:
        new_hash = running

    # Full replay (excluding checkpoints) for explicit tamper signal vs incremental segment
    full_h: str | None = None
    for ev in events:
        if ev.get("event_type") == "AuditIntegrityCheckRun":
            continue
        full_h = rolling_hash(full_h, ev)
    full_replay = full_h or hashlib.sha256(b"").hexdigest()
    tamper_detected = False
    if previous_full_replay is not None and full_replay != previous_full_replay:
        # If any prior non-checkpoint event changed, the full replay hash must drift.
        tamper_detected = True
    chain_valid = not tamper_detected

    new_event = {
        "event_type": "AuditIntegrityCheckRun",
        "event_version": 1,
        "payload": {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "check_timestamp": datetime.now(timezone.utc).isoformat(),
            "events_verified_count": len(events_to_hash),
            "integrity_hash": new_hash,
            "previous_hash": previous_hash,
            "chain_valid": chain_valid,
            "tamper_detected": tamper_detected,
            "full_replay_integrity_hash": full_replay,
        },
    }
    version = await store.stream_version(stream_id)
    await store.append(stream_id, [new_event], expected_version=version)

    return IntegrityCheckResult(
        events_verified=len(events_to_hash),
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        integrity_hash=new_hash,
        full_replay_integrity_hash=full_replay,
        previous_hash=previous_hash,
    )


async def verify_audit_stream_full_replay(store, entity_type: str, entity_id: str) -> dict:
    """
    Recompute hash over all non-checkpoint events; use to compare with a prior baseline or detect drift.
    """
    stream_id = f"audit-{entity_type}-{entity_id}"
    events = await store.load_stream(stream_id)
    full_h: str | None = None
    n = 0
    for ev in events:
        if ev.get("event_type") == "AuditIntegrityCheckRun":
            continue
        full_h = rolling_hash(full_h, ev)
        n += 1
    return {
        "stream_id": stream_id,
        "data_events_count": n,
        "full_replay_integrity_hash": full_h or hashlib.sha256(b"").hexdigest(),
    }

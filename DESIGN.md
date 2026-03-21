# DESIGN.md — The Ledger

**Architecture and design decisions for the Agentic Event Store.**

---

## 1. Aggregate Boundary Justification

We use four aggregates: **LoanApplication**, **AgentSession**, **ComplianceRecord**, and **AuditLedger**.

**ComplianceRecord separate from LoanApplication**

We considered merging compliance events into the loan stream. Rejected because:

- **Write contention:** ComplianceAgent and CreditAnalysis agent would both append to `loan-{id}`. Parallel appends with the same `expected_version` cause one to succeed and the other to raise `OptimisticConcurrencyError`. Compliance would be forced to retry and re-load the entire loan stream (credit, fraud, documents) just to append a single rule result — OCC thrashing.

- **Coupling:** Compliance checks are independent regulatory work. They should not block or be blocked by loan-state updates. Separate stream `compliance-{application_id}` isolates write contention.

- **Load:** At 100 apps × 4 agents, compliance writes can run in parallel with decision writes. Merged, they would serialize.

**Other boundaries**

- **AgentSession** (`agent-{agent_type}-{session_id}`): Gas Town pattern requires a dedicated stream for agent context. Prevents mixing agent memory with loan events.

- **AuditLedger** (`audit-{entity_type}-{entity_id}`): Append-only audit trail. Keeps causal ordering and integrity checks independent of business aggregates.

---

## 2. Projection Strategy

| Projection             | Inline vs Async | SLO      | Notes                                                                 |
|------------------------|-----------------|----------|-----------------------------------------------------------------------|
| ApplicationSummary     | Async (daemon)  | &lt;500ms | Primary read model for loan state. Polls `events` from checkpoint.    |
| AgentPerformanceLedger | Async           | &lt;2s    | Aggregates CreditAnalysisCompleted, DecisionGenerated per agent.      |
| ComplianceAuditView    | Async           | &lt;2s    | Full compliance record; supports `get_compliance_at(timestamp)`.      |

**Inline vs async:** All projections are async via `ProjectionDaemon`. Inline (synchronous) would block append latency and couple writes to reads. Async allows appends to return immediately; projections catch up.

**ComplianceAuditView snapshot strategy:** Event-count based. Each event is applied to `_by_app[application_id]`. For `get_compliance_at(timestamp)`, we filter events by `recorded_at <= timestamp`. No separate snapshots table; replay is cheap for typical compliance event volume per application. For very high volume, we could add time-bucketed snapshots (e.g. daily) and replay from nearest snapshot.

**Fault tolerance:** On handler error, daemon logs and continues. Checkpoints advance only on success. Configurable retry (e.g. dead-letter) can be added.

---

## 3. Concurrency Analysis

**OCC rate at 100 apps × 4 agents**

- Assumption: ~4 concurrent writers per application (credit, fraud, compliance, decision). Each append reads `stream_version`, validates, then appends.
- Conflict window: from read to commit. Typical append latency ~5–10ms.
- Expected OCC rate: Low for disjoint streams (credit, fraud, compliance are separate). OCC occurs mainly on `loan-{id}` when decision and human review race, or when multiple agents touch the same stream.
- Estimate: &lt;5% of appends under normal load.

**Retry budget**

- Command handlers catch `OptimisticConcurrencyError`, reload aggregate, re-validate, retry.
- Max retries: 3. After 3 OCC failures, return structured error to caller (MCP tool returns `suggested_action: reload_stream_and_retry`).
- Exponential backoff optional: 0ms, 50ms, 100ms to reduce contention spikes.

---

## 4. Upcasting Inference Decisions

**CreditAnalysisCompleted v1→v2**

| Field            | Strategy                    | Error rate / consequences                                       |
|------------------|-----------------------------|------------------------------------------------------------------|
| `model_version`  | Infer `"legacy-pre-2026"`   | No error. Downstream treats as legacy; exclude from version analytics. |
| `confidence_score` | **null** (do not infer)   | Null signals unknown. Fabricating a value would pollute analytics. |
| `regulatory_basis` | Infer `[]`                | Empty list. Optional: infer from `recorded_at` if rule-versions-by-date registry exists. |

**DecisionGenerated v1→v2**

- `model_versions`: Infer `{}` or `{"orchestrator": "legacy"}` for historical events.

**Immutability:** Upcasters run only on read (`load_stream`, `load_all`). Stored payloads in `events` table are never modified. Test: insert v1, load via store → v2; query raw table → v1 unchanged.

---

## 5. EventStoreDB Comparison

| Feature              | EventStoreDB                         | Our Postgres implementation                                |
|----------------------|--------------------------------------|-------------------------------------------------------------|
| Streams              | Native streams, built-in              | `event_streams` + `events` tables, stream_id grouping       |
| Append               | Single RPC                           | `append()` with OCC via `expected_version`                  |
| Outbox               | Not built-in; use projection          | Explicit `outbox` table, same transaction as append         |
| Projections          | Built-in projection engine            | Custom `ProjectionDaemon`, poll `events` from checkpoint    |
| $all stream          | Native                               | `load_all()` via `global_position` ordering                 |
| Subscriptions        | Server-side, push                    | Poll-based daemon; push would need LISTEN/NOTIFY or poll    |
| Metadata             | Stream metadata API                  | `get_stream_metadata()` from `event_streams`                |
| Archive              | Soft delete                          | `archive_stream()` sets `archived_at`                       |
| Idempotency          | Client-controlled event IDs           | Not implemented; would add `event_id` uniqueness constraint |

**What EventStoreDB gives that Postgres requires extra work**

- **Push subscriptions:** EventStoreDB pushes events to projection consumers. We poll. Adding LISTEN/NOTIFY or logical replication would reduce latency.
- **Scavenging / compaction:** EventStoreDB can merge chunks. We keep all events; archive is logical only.
- **Built-in idempotency:** EventStoreDB deduplicates by event ID. We would add a unique index on `(stream_id, event_id)` and `ON CONFLICT DO NOTHING` for idempotent appends.

---

## 6. What You Would Do Differently

**Single biggest reconsideration with another day**

**Snapshotting for aggregates.** Loading a long-lived stream (e.g. `loan-APEX-001` with 50+ events) replays every event on each command. With another day, we would add optional **snapshot storage**: periodically persist `{stream_id, version, state_json}`. On load, read snapshot and replay only events after `version`. This reduces read load and latency for hot aggregates. Trade-off: snapshot format must be versioned; schema changes require migration. We would start with LoanApplication and AgentSession as candidates.

---

*End of DESIGN.md*

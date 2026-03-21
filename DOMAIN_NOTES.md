# DOMAIN_NOTES.md — The Ledger

**Graded deliverable.** Answers to the six required questions with specificity.

---

## 1. EDA vs. ES Distinction

**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?

**Answer:** Callbacks that capture traces as the agent runs are **Event-Driven Architecture (EDA)**, not Event Sourcing (ES).

In EDA with callbacks:
- Events are *emitted* as side effects during execution.
- The sender "fires and forgets" — no guarantee the consumer persists them.
- If the process crashes mid-run, the trace is lost.
- There is no single source of truth; trace storage is a downstream consumer.

**With The Ledger (ES), the architecture would change as follows:**

| EDA (Current) | ES (Ledger) |
|---------------|-------------|
| Agent runs → callback fires → trace logged | Agent writes `AgentContextLoaded` to event store *before* execution |
| Traces stored in a separate system (e.g. LangSmith) | Traces *are* the event stream; events are the database |
| No replay — cannot reconstruct agent state after crash | Full replay: `load_stream(agent-{id}-{session})` rebuilds exact context |
| Trace loss on crash | Gas Town: agent replays stream, resumes from last event |
| Event ordering not guaranteed across aggregates | Causal chains via `correlation_id` / `causation_id` |

**What we gain:** Immutability, replayability, auditability, and the ability for an agent to recover its context from the event store on restart — the Gas Town persistent ledger pattern.

---

## 2. The Aggregate Question

**Question:** In the scenario below, you will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?

**Answer:** We considered and **rejected** merging **ComplianceRecord** into **LoanApplication**.

**Alternative boundary rejected:** A single aggregate that combines LoanApplication + ComplianceRecord — one stream `loan-{application_id}` containing both loan lifecycle events and compliance check events.

**Coupling problem this would cause:** Under concurrent load, the ComplianceAgent and the CreditAnalysis agent would both append to the same stream. When the ComplianceAgent writes `ComplianceRulePassed` for REG-001 and the CreditAnalysis agent writes `CreditAnalysisCompleted` in parallel, both read `expected_version=5`. One succeeds (v6); the other gets `OptimisticConcurrencyError`. The ComplianceAgent, which is doing independent regulatory work, would be forced to retry and re-load the *entire* loan stream — including credit analysis, fraud screening, and document events — just to append its compliance result. This causes **OCC thrashing**: compliance writes contend with loan-state writes unnecessarily.

**Chosen boundary:** ComplianceRecord as a separate aggregate with stream `compliance-{application_id}`. Compliance checks append to their own stream. The LoanApplication aggregate holds a *reference* (e.g. `compliance_checks_complete: bool` or a list of required rule IDs) and, when appending `ApplicationApproved`, validates by loading the ComplianceRecord stream. This keeps write contention isolated: compliance writes do not block loan writes, and vice versa.

---

## 3. Concurrency in Practice

**Question:** Two AI agents simultaneously process the same loan application and both call `append_events` with `expected_version=3`. Trace the exact sequence of operations in your event store. What does the losing agent receive, and what must it do next?

**Answer:**

**Initial state:** Stream `loan-APEX-001` has 4 events (positions 0–3). `current_version = 3`. Both agents have read the stream and both pass `expected_version=3`.

**Exact sequence:**

1. **Agent A** acquires connection, starts transaction, runs `SELECT ... FROM event_streams WHERE stream_id='loan-APEX-001' FOR UPDATE`. Row is locked.
2. **Agent B** acquires connection, starts transaction, runs the same `SELECT ... FOR UPDATE`. Blocks waiting on the row lock.
3. **Agent A** checks: `current_version (3) == expected_version (3)` — OK.
4. **Agent A** inserts event at `stream_position=4`, updates `event_streams.current_version=4`, commits. Lock released.
5. **Agent B** unblocks, acquires lock, reads row: `current_version=4`.
6. **Agent B** checks: `current_version (4) != expected_version (3)` — **FAIL**.
7. **Agent B** raises `OptimisticConcurrencyError(stream_id='loan-APEX-001', expected=3, actual=4)`.
8. **Agent B** rolls back, does *not* insert.

**What the losing agent receives:**  
`OptimisticConcurrencyError` with `stream_id`, `expected_version=3`, `actual_version=4`.

**What it must do next:**  
1. Catch the exception.  
2. Call `load_stream('loan-APEX-001')` to reload the current state (now 5 events, version 4).  
3. Re-apply business logic: is its analysis still valid? (e.g. another agent may have completed a superseding analysis.)  
4. If still valid: call `append(..., expected_version=4)` and retry.  
5. If superseded: abandon the append (or escalate, depending on business rules).

**Structured error for LLM consumption:**
```json
{
  "error_type": "OptimisticConcurrencyError",
  "message": "OCC on 'loan-APEX-001': expected v3, actual v4",
  "stream_id": "loan-APEX-001",
  "expected_version": 3,
  "actual_version": 4,
  "suggested_action": "reload_stream_and_retry"
}
```

---

## 4. Projection Lag and Its Consequences

**Question:** Your LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do, and how do you communicate this to the user interface?

**Answer:**

**What the system does:** The projection daemon has not yet processed the disbursement event. The ApplicationSummary row for that application still shows the pre-disbursement `approved_amount_usd` (or available limit). The query returns stale data. The system does *not* block or wait — eventual consistency is by design.

**How we communicate this to the UI:**

1. **Expose projection lag as a metric:** `ProjectionDaemon.get_lag('ApplicationSummary')` returns milliseconds behind the latest event. The API can include `projection_lag_ms: 180` in the response.

2. **Timestamp the read:** Return `last_event_at` (or `projection_updated_at`) with the summary. The UI can display: "Data as of 14:32:01.234 UTC" so the user knows the freshness.

3. **Stale indicator for critical fields:** For "available credit limit" specifically, if `projection_lag_ms > 500` (our SLO), the UI shows a warning: "Limit may be updating. Refresh in a moment."

4. **Optional strong-read path:** For high-value operations (e.g. final disbursement confirmation), we can offer a "strong read" that loads the aggregate stream directly (bypassing the projection) for that one query. This adds latency but guarantees consistency. Document this as an escape hatch, not the default.

5. **Refresh guidance:** The UI provides a "Refresh" button. After 500ms (our ApplicationSummary SLO), a refresh will likely return updated data.

**SLO commitment:** ApplicationSummary lag &lt; 500ms in normal operation. If lag exceeds 500ms, the UI surfaces the staleness; we do not hide it.

---

## 5. The Upcasting Scenario

**Question:** The CreditDecisionMade event was defined in 2024 with `{application_id, decision, reason}`. In 2026 it needs `{application_id, decision, reason, model_version, confidence_score, regulatory_basis}`. Write the upcaster. What is your inference strategy for historical events that predate `model_version`?

**Answer:** The challenge event catalogue uses **CreditAnalysisCompleted** (not CreditDecisionMade). We address CreditAnalysisCompleted v1→v2 with the same inference logic.

**Upcaster implementation:**

```python
@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_analysis_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_version": payload.get("model_version") or "legacy-pre-2026",
        "confidence_score": payload.get("confidence_score"),  # None for historical
        "regulatory_basis": payload.get("regulatory_basis") or [],
    }
```

**Inference strategy for historical events:**

| Field | Strategy | Rationale |
|-------|----------|-----------|
| `model_version` | Infer `"legacy-pre-2026"` | We cannot know the exact model. Using a sentinel value is honest and traceable. Downstream logic can treat it as "legacy" (e.g. exclude from model-version analytics). Fabricating a fake version would mislead auditors. |
| `confidence_score` | **null** (do not infer) | We have no basis to guess. Fabricating a confidence score would be worse than null: it would pollute analytics (e.g. "legacy model had 0.85 confidence") and could mislead risk decisions. Null clearly signals "unknown." |
| `regulatory_basis` | Infer `[]` or from `recorded_at` | If we have a registry of regulation versions active by date, we can infer `["REG-2024-Q1"]` from `recorded_at`. Otherwise, empty list. Document that inference in DESIGN.md. |

**When to choose null over inference:** When the field affects business decisions (e.g. confidence_score for REFER threshold) or audit conclusions, and we have no evidence — use null. When the field is metadata for categorization (e.g. model_version as "legacy"), inference is acceptable.

---

## 6. The Marten Async Daemon Parallel

**Question:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in your Python implementation. What coordination primitive do you use, and what failure mode does it guard against?

**Answer:**

**Pattern:** Multiple ProjectionDaemon processes run on different nodes. Each projection (e.g. ApplicationSummary) is processed by exactly one daemon at a time. If a daemon dies, another takes over within a lease timeout.

**Coordination primitive:** A **projection_leases** table:

```sql
CREATE TABLE projection_leases (
  projection_name TEXT PRIMARY KEY,
  lease_holder TEXT NOT NULL,           -- hostname + pid
  leased_until TIMESTAMPTZ NOT NULL,    -- lease expiration
  last_position BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
```

**Algorithm:**

1. **Acquire lease:** Daemon runs `UPDATE projection_leases SET lease_holder=$1, leased_until=NOW()+interval '30 seconds' WHERE projection_name=$2 AND (leased_until < NOW() OR lease_holder=$1)` and checks rows affected. If 1, lease acquired. If 0, another node holds it — sleep and retry.

2. **Process batch:** Read events from `last_position`, run projection handlers, update `projection_checkpoints` and `projection_leases.last_position`, set `leased_until=NOW()+interval '30 seconds'` (heartbeat).

3. **Heartbeat:** Every 10 seconds, update `leased_until` to extend the lease. If the daemon crashes, it stops heartbeating; after 30 seconds the lease expires.

4. **Release:** On graceful shutdown, set `leased_until=NOW()` so another node can acquire immediately.

**Alternative:** PostgreSQL advisory locks: `SELECT pg_advisory_lock(hashtext('projection:ApplicationSummary'))` before processing. If the process dies, the connection closes and the lock is released. Simpler but requires holding a connection for the entire run.

**Failure mode guarded against:** **Duplicate processing** — two daemons processing the same projection and double-applying events to the read model. The lease ensures only one holder processes at a time. The **split-brain** scenario (both think they hold the lease) is prevented by the database-enforced `WHERE` condition and single updater wins.

---

*End of DOMAIN_NOTES.md*

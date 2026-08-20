# The Ledger

Agentic Event Store and Enterprise Audit Infrastructure for Apex Financial Services.

## Project Status

All phases are **100% complete, fully implemented, and validated**:

- **Phase 0**: Domain Reconnaissance & Conceptual Foundations (`DOMAIN_NOTES.md`)
- **Phase 1**: Event Store Core (`EventStore` with OCC, outbox, metadata, load_stream, load_all, PostgreSQL + InMemory)
- **Phase 2**: Domain Aggregates & Business Rules (`LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger` + 6 core business rules + command handlers)
- **Phase 3**: Projections & Async Daemon (`ProjectionDaemon`, `ApplicationSummary`, `AgentPerformanceLedger`, `ComplianceAuditView` with temporal time-travel query `get_compliance_at` & `rebuild_from_scratch`)
- **Phase 4**: Upcasting, Integrity & Gas Town (`UpcasterRegistry` for v1→v2, `run_integrity_check` with rolling SHA-256 hash chains, `reconstruct_agent_context` for crash recovery)
- **Phase 5**: FastMCP Server (8 Tools + 6 Resources with structured typed errors & LLM precondition docstrings)
- **Phase 6 (Bonus Deliverables)**: Counterfactual What-If Projector (`what_if/projector.py` with causal filtering) and Regulatory Examination Package Generator (`regulatory/package.py`)
- **Narratives Integration**: All 5 narrative scenario integration tests passing (`test_narratives.py`: NARR-01 to NARR-05)

## Key Report Artifacts

- **`FINAL_REPORT.md`**: PDF-ready, comprehensive final submission report covering all 8 rubric criteria.
- **`DOMAIN_NOTES.md`**: Architectural decisions, EDA vs. ES, aggregate boundaries, OCC mechanics, projection lag, and upcasting.
- **`DESIGN.md`**: System design document and trade-off analysis.
- **`schema.sql`** & **`starter/schema.sql`**: Complete PostgreSQL enterprise schema.

## Repository Layout

- `starter/ledger/` — Core implementation source code
  - `event_store.py` — Core EventStore (PostgreSQL + In-Memory)
  - `upcasters.py` — Upcaster registry for schema evolution
  - `domain/aggregates/` — Domain aggregate models
  - `commands/` — Command handlers enforcing invariants
  - `projections/` — CQRS projections & async daemon
  - `integrity/` — Audit hash chain & Gas Town crash recovery
  - `what_if/` — Counterfactual what-if projection engine (Phase 6)
  - `regulatory/` — Regulatory examination package generator (Phase 6)
  - `agents/` — Operational AI agent implementations (LangGraph)
  - `mcp_server.py` — FastMCP server (8 tools + 6 resources)
- `starter/tests/` — Automated pytest test suite
- `starter/scripts/` — Demonstration and video scripts
- `starter/datagen/` — Enterprise data & Monte Carlo event simulator

## Prerequisites

- Python 3.11+
- Docker (optional, for local PostgreSQL instance)

## Quick Start & Verification

```bash
# Run full automated test suite (44 tests passing)
pytest tests -vv

# Run the 5 narrative scenario tests (NARR-01 to NARR-05)
pytest tests/test_narratives.py -vv

# Run Phase 6 Bonus tests (What-If & Regulatory Package)
pytest tests/test_what_if.py tests/test_regulatory_package.py -vv

# Run MCP end-to-end lifecycle test
pytest tests/test_mcp_lifecycle.py -vv -s
```

## Running Demo Scripts

```bash
# Full decision lifecycle demo (< 60 seconds)
python scripts/video_demo_step1.py

# Gas Town agent crash recovery demo
python scripts/video_demo_gas_town.py
```

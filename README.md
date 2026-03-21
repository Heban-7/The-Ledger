# The Ledger

Agentic Event Store and Enterprise Audit Infrastructure for Apex Financial Services.

## Project Status

Implemented phases:

- Phase 0: `DOMAIN_NOTES.md`
- Phase 1: Event store core (PostgreSQL + in-memory), OCC, outbox, metadata/archive
- Phase 2: Domain aggregates + command handlers
- Phase 3: Async projections + daemon
- Phase 4: Upcasting + integrity chain + Gas Town reconstruction
- Phase 5: MCP server tools/resources + lifecycle tests

Core report artifacts:

- `FINAL_REPORT.md` (PDF-ready final report)
- `DOMAIN_NOTES.md`
- `DESIGN.md`

## Repository Layout

- `starter/ledger/` — implementation source code
- `starter/tests/` — automated tests
- `starter/datagen/` — schema/data generation utilities
- `FINAL_REPORT.md` — final submission report (markdown)

## Prerequisites

- Python 3.13+
- Docker (optional, for local PostgreSQL)
- [uv](https://docs.astral.sh/uv/) for environment/dependency management

## Quick Start (uv)

```bash
uv sync
uv run pytest starter/tests -q
```

If you prefer running from `starter/`:

```bash
cd starter
python -m pytest tests -q
```

## Optional: Run with PostgreSQL

```bash
docker run -d --name apex-ledger-db -e POSTGRES_PASSWORD=apex -e POSTGRES_DB=apex_ledger -p 5432:5432 postgres:16
```

Then configure environment variables (see `.env.example`).

## Key Test Commands

```bash
uv run pytest starter/tests/test_concurrency.py -vv
uv run pytest starter/tests/test_projections.py -vv
uv run pytest starter/tests/test_upcasting.py -vv
uv run pytest starter/tests/test_gas_town.py -vv
uv run pytest starter/tests/test_mcp_lifecycle.py -vv
uv run pytest starter/tests -q
```

## MCP Surface

Implemented MCP tools include:

- `submit_application`
- `start_agent_session`
- `record_credit_analysis`
- `record_fraud_screening`
- `record_compliance_check`
- `generate_decision`
- `record_human_review`
- `run_integrity_check`

Query/resource-style endpoints are provided in `starter/ledger/mcp_server.py`.

## Notes

- Main runtime package path is `starter/ledger`.
- Bonus Phase 6 (`what_if`, `regulatory package`) is not fully implemented.

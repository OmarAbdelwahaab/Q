# ADR 001: Pipeline State Database Architecture (SQLite vs. PostgreSQL)

## Status
Accepted

## Context
During Phase 0 scaffolding, two database backends were referenced in the codebase:
1. `docker-compose.yml` provisions a PostgreSQL 16 container, initialized with `pipeline/state/schema.sql` (creating `pipeline_items` and `pipeline_messages`), and `.env.example` defines `STATE_DATABASE_URL=postgresql://pipeline:pipeline@localhost:5432/pipeline`.
2. `pipeline/state/repository.py` implements `PipelineStateRepository`, which connects to a local SQLite database (`STATE_DB_PATH=./pipeline/state/pipeline.db`).

All phases up to Phase 5 have used `PipelineStateRepository` with SQLite for local execution and automated unit testing. `TASKS.md` raised an open question:
> "Decide and document: is the Postgres pipeline_items table (provisioned in docker-compose.yml/schema.sql) the target for later phases, or is SQLite (currently used by PipelineStateRepository) staying as-is for longer? STATE_DATABASE_URL is defined in .env.example but nothing reads it yet — fine for now, but worth being explicit before more phases build on top of one or the other"

## Decision

### 1. Dual-Tier Strategy by Pipeline Lifecycle
- **Phases 1–7 (Local Service Development, CLI Invocations, and Automated Testing)**:
  - **Primary Backend**: SQLite (`STATE_DB_PATH`).
  - **Rationale**: SQLite requires zero running daemons or network dependencies. Unit and integration tests can spin up fresh, isolated SQLite databases in temporary directories in milliseconds. Developers can run any CLI step (ingest, audio, recognize, align, qa_gate, render, publish) directly in their local terminal without starting Docker containers.
- **Phase 8+ (End-to-End Orchestration with n8n & Multi-Container Deployment)**:
  - **Target Backend**: PostgreSQL (`STATE_DATABASE_URL`).
  - **Rationale**: When n8n runs in Docker and orchestrates independent containerized worker stages, concurrent access to a single SQLite file across containers is prone to locking conflicts (`SQLITE_BUSY`) and shared volume latency. PostgreSQL provides ACID compliance, row-level locking, and native connection pooling for concurrent execution and monitoring dashboards.

### 2. Implementation & Migration Roadmap
- **Phases 1–7**: All pipeline services will continue to interact with the state store through the repository interface, using SQLite by default.
- **Phase 8**: During n8n workflow construction, `pipeline/state/repository.py` will be enhanced with a database engine abstraction or connection switch:
  - If `STATE_DATABASE_URL` is set, connect to PostgreSQL.
  - If `STATE_DB_PATH` is set (or in test environments), fall back to SQLite.
  - Both backends share the identical schema defined in `pipeline/state/schema.sql`.
- **Schema Parity**: Both SQLite and PostgreSQL schemas maintain identical table structures:
  - `pipeline_items (message_id, stage, status, created_at, updated_at, error)` with composite PK `(message_id, stage)`.
  - `pipeline_messages (message_id, channel_id, message_timestamp, duration_seconds, created_at, updated_at)` with PK `message_id`.

## Consequences
- No breaking changes for Phases 1–7 services or existing tests.
- Local tests remain fast, deterministic, and self-contained without Docker prerequisites.
- Clear contract established for Phase 8 orchestration to switch to PostgreSQL.

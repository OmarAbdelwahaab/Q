# Project Guidelines for Antigravity

## Phase Execution and Commit Discipline
- **Commit at Phase Boundary**: Every implementation phase defined in `TASKS.md` must be completed, tested, documented, and committed to Git at the end of that phase.
- **Never Skip or Accumulate Uncommitted Phases**: Do not proceed to downstream phases while work from prior phases is uncommitted.
- **Artifacts**: Maintain `PHASE<N>_TEST_PLAN.md` and `PHASE<N>_COMPLETION_REPORT.md` for each phase.
- **Verification Gate**: All unit and integration tests must pass cleanly before concluding a phase.

## State Store Architecture
- **Phases 1–7**: SQLite is the default local database (`STATE_DB_PATH`).
- **Phase 8+**: PostgreSQL (`STATE_DATABASE_URL`) is the shared database for multi-container orchestration (n8n).
- See `docs/decisions/001-state-database-strategy.md` for detailed rationale.

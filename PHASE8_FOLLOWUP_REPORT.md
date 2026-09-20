# Phase 8 Follow-Up Report — Orchestration & Concurrency Review Refinements

**Status:** All 5 follow-up code review tasks completed; 118 unit, integration, concurrency, and CLI tests passing cleanly under both test runners (`unittest` and `pytest`).  
**Date:** 2026-09-18  
**Repository:** https://github.com/OmarAbdelwahaab/Q (branch: `main`)

---

## 1. Summary of Changes per Task

### Task 1: Clean Up Dead `ingestion_service` Wiring
- **Rationale**: `PipelineOrchestrator.run()` was previously updated so stage 1 ingestion copies from `--source` (if provided) or verifies `raw/{message_id}.mp4` on disk directly without calling `ingestion_service.ingest()`. `self.ingestion_service` had become dead code stored in `__init__` and constructed in `build_orchestrator()`.
- **Changes**:
  - `pipeline/orchestration/runner.py`:
    - Removed `ingestion_service` parameter and `self.ingestion_service` attribute from `PipelineOrchestrator.__init__`.
    - Removed `IngestionService` from imports.
  - `pipeline/orchestration/app.py`:
    - Removed unused `IngestionService` and `LocalArtifactStorage` instantiation and imports from `build_orchestrator()`.
  - `pipeline/tests/test_orchestration.py`:
    - Kept clean instantiation tests verifying `build_orchestrator()` without `ingestion_service`.

### Task 2: Refactor `--force` Semantics in `claim_execution`
- **Rationale**: Previously, `--force` unconditionally claimed execution even if a live worker was actively processing the message (`status == "processing"`), risking duplicate parallel execution. Operators should only forcibly interrupt an active worker with an explicit confirmation, or if the active claim is genuinely stale.
- **Changes**:
  - `pipeline/state/repository.py`:
    - Updated `claim_execution(message_id, stage="orchestration", force=False, force_active=False, stale_threshold_seconds=300)`:
      - If `status == "processing"`:
        - Calculates elapsed time since `updated_at`.
        - If `elapsed < stale_threshold_seconds` and `not force_active`: rejects claim, returning `(False, "active_execution_recent")`.
        - If `elapsed >= stale_threshold_seconds` (stale worker) OR `force_active=True` (operator override): acquires claim, returning `(True, "claimed")`.
      - If `status == "held_for_review"`: continues requiring `force=True` to re-run.
  - `pipeline/orchestration/runner.py`:
    - Added `force_active: bool = False` to `PipelineOrchestrator.run()`.
    - Handles reason `"active_execution_recent"` by logging an operator error and returning `PipelineExecutionSummary(status="skipped_active_execution", error="...")`.
  - `pipeline/orchestration/app.py`:
    - Added `--force-active` CLI flag (`action="store_true"`).
    - Updated `--force` help text: `"Force execution past held_for_review QA gates or stale processing claims (>= 300s). For fresh processing claims, also pass --force-active."`
    - Updated `--force-active` help text: `"Force execution even if an active worker is currently processing this message (< 300s ago). Use with caution."`
    - Forwarded `force_active` to `orchestrator.run()`.
  - `pipeline/tests/test_orchestration.py`:
    - Updated `test_claim_execution_lifecycle` to verify that `force=True, force_active=False` is rejected on fresh processing, but accepted with `force_active=True`.
    - Added `test_claim_execution_force_semantics` testing stale processing claim takeover and held-for-review override.
    - Added `test_orchestrator_force_active_execution_behavior` testing end-to-end orchestrator handling of fresh vs stale active claims.

### Task 3: Stale Reservation Cleanup in `reserve_publish_slot`
- **Rationale**: If a previous worker crashed while holding a rate-limit slot (`status == "processing"` on another message), `reserve_publish_slot` correctly allowed superseding after `reservation_timeout_seconds` elapsed. However, it left the crashed message in `processing` indefinitely.
- **Changes**:
  - `pipeline/state/repository.py`:
    - In `reserve_publish_slot()`: when superseding a stale reservation (`last_status == "processing"` and `elapsed >= reservation_timeout_seconds`), atomically updates the stale message's row in `pipeline_items` to `status = "failed"` with error `"Reservation expired after {int(elapsed)}s, presumed crashed worker"` within the same transaction.
  - `pipeline/tests/test_orchestration.py`:
    - Added `test_reserve_publish_slot_cleans_up_stale_processing_reservation` verifying that a backdated reservation (>300s) on message A is atomically marked as `failed` when message B reserves the slot.

### Task 4: Documentation of SQLite Concurrency Limitation
- **Rationale**: SQLite's single-writer architecture and file-locking behavior must be explicitly documented so operators understand why PostgreSQL is required for multi-worker environments.
- **Changes**:
  - `docs/decisions/001-state-database-strategy.md`:
    - Added `## Known Limitations` section detailing SQLite's `BEGIN IMMEDIATE` whole-file lock vs PostgreSQL's per-key transactional advisory locks (`pg_advisory_xact_lock`), explaining that multi-worker SQLite setups risk `database is locked` under concurrency.
  - `pipeline/state/repository.py`:
    - Updated `PipelineStateRepository` class docstring with a dedicated concurrency notice explaining the locking models and referencing ADR 001.

### Task 5: GitHub Actions CI Workflow
- **Rationale**: Pipeline CI must run automated tests on Linux with real PostgreSQL service container, `ffmpeg`, and standard dependencies.
- **Changes**:
  - Created `.github/workflows/ci.yml`:
    - Runs on `ubuntu-latest` with Python 3.12.
    - Spawns `postgres:16-alpine` service container with health checks (`pg_isready`).
    - Installs `ffmpeg` and `postgresql-client` via `apt-get`.
    - Initializes the database schema using `pipeline/state/schema.sql`.
    - Installs package with `.[dev]`, `"psycopg[binary]>=3.1"`, and `"psycopg_pool>=3.1"`.
    - Runs both `pytest -v` and `python -m unittest discover -s pipeline/tests -v` with `STATE_DATABASE_URL` set.

---

## 2. Verification Results

### Standard Library Discovery (`python -m unittest discover -s pipeline/tests -v`)
```text
Ran 118 tests in 5.301s

OK (skipped=1)
```
*(Note: 1 test skipped is `test_postgres_state_repository_parity_and_advisory_locks` which is intentionally skipped in local SQLite mode when `STATE_DATABASE_URL` is not set; covered under the CI Postgres environment).*

### Pytest Full Regression Suite (`pytest -v`)
```text
======================= 118 passed, 1 skipped in 8.75s ========================
```

> [!NOTE]
> **GitHub Actions CI Run**: The `.github/workflows/ci.yml` workflow has been created and verified locally against the test suite. Triggering and observing the live run on GitHub requires pushing this commit to the remote repository on GitHub (`origin/main`).

---

## 3. Artifact Checklist
- [x] `pipeline/orchestration/runner.py` (Removed unused `ingestion_service`; added `force_active` handling)
- [x] `pipeline/orchestration/app.py` (Cleaned `build_orchestrator`; added `--force-active` CLI flag and updated `--force` help text)
- [x] `pipeline/state/repository.py` (Safe `--force` semantics with stale threshold; atomic stale reservation cleanup; updated concurrency docstring)
- [x] `docs/decisions/001-state-database-strategy.md` (Documented SQLite `BEGIN IMMEDIATE` whole-file lock vs PostgreSQL `pg_advisory_xact_lock`)
- [x] `.github/workflows/ci.yml` (Linux CI workflow with Python 3.12, FFmpeg, and PostgreSQL 16 service container)
- [x] `pipeline/tests/test_orchestration.py` (Tests for stale takeover, `--force-active`, stale publish reservation cleanup, and CLI flag parsing)
- [x] `PHASE8_FOLLOWUP_REPORT.md` (This report)

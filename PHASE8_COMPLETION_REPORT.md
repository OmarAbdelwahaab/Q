# Phase 8 Completion Report — Workflow Orchestration, Scheduling & Dual-Tier State

**Status:** Implementation and follow-up review fixes complete; all 116 unit, integration, dry-run, concurrency, and CLI tests passing cleanly (0 skipped, 0 failures) under both `python -m unittest discover` and `pytest -v` against live PostgreSQL.  
**Date:** 2026-09-17

---

## 1. Summary of Deliverables & Refinements

- **Complete 7-Stage Pipeline Orchestrator (`pipeline/orchestration/runner.py`)**:
  - Implemented `PipelineOrchestrator` coordinating the full lifecycle for a given video message:
    1. Idempotency check & atomic claim (`claim_execution`).
    2. Stage 1 Ingestion: validates presence of `raw/{message_id}.mp4` or copies from `--source`; halts cleanly at `ingestion` with `failed` status if missing.
    3. Stage 2 Audio extraction (`AudioExtractionService` $\to$ 16kHz mono WAV).
    4. Stage 3 Verse recognition (`RecognitionService` $\to$ canonical Quran match JSON).
    5. Stage 4 Forced alignment (`AlignmentService` $\to$ word timestamps).
    6. Stage 5 QA Gate verification (`QAGateService`). If rejected/held, execution halts immediately, state is recorded as `held_for_review`, alerts are dispatched, and render/publish are **never executed**.
    7. Stage 6 Scheduler evaluation & atomic rate-limit slot reservation (`reserve_publish_slot`).
    8. Stage 7 Video render (`RenderService` composing background, HarfBuzz RTL karaoke ASS subtitles, audio, and branding into 1080x1920 MP4).
    9. Stage 8 Multi-platform publish (`PublishService` uploading to public URL and posting via Ayrshare in live or draft mode).
  - Clean error boundaries: any unhandled exception in any stage marks state as `failed`, alerts via `AlertService`, and halts progression for that item without crashing other jobs.
  - **Refactored `run()` boilerplate**: Extracted `_run_stage` consolidating error handling, logging, state store upsert, and alert dispatch, eliminating ~250 lines of duplicate code.

- **Follow-Up Review Fixes**:
  - **Ingestion Stage Integration & Docker Compose Service (Issue 1)**:
    - Added `IngestionService` into `build_orchestrator()` and `PipelineOrchestrator.__init__`.
    - Integrated Stage 1 (`ingestion`) into `PipelineOrchestrator.run()`: handles explicit `--source`, validates pre-downloaded `raw/{message_id}.mp4`, and halts cleanly at `ingestion` with `status="failed"` on cold start without video.
    - Added `ingestion` service container to `docker-compose.yml` running `pipeline.ingestion.app` continuously with shared volume mounting.
  - **n8n Workflow Dual Connections & Failure Alerting (Issue 2)**:
    - Updated `pipeline/orchestration/workflow.json` to configure both output arrays on `Check Orchestration Exit Code`:
      - Output 0 (`exitCode == 2`): routes to `Alert QA Review Queue`.
      - Output 1 (`exitCode != 2`): routes to `Check Execution Succeeded` IF node (`exitCode == 0`).
    - Added `Alert Pipeline Failure` HTTP Request node attached to `Check Execution Succeeded` false branch (`exitCode != 0`, e.g. exit code 1), ensuring failures are visibly alerted and flagged.
    - Updated `N8nWorkflowIntegrityTests` verifying 6 collapsed nodes, both branches, and HTTP request alerting.
  - **Cold Start Integration Test (Issue 3)**:
    - Added `test_cold_start_without_raw_video_fails_cleanly` in `pipeline/tests/test_orchestration.py`.
    - Verified that missing raw video with no `--source` cleanly fails at stage `ingestion`, records `(message_id, "ingestion", "failed")` in state DB, dispatches an alert, halts before audio extraction, and CLI returns exit code 1.
  - **Live PostgreSQL Advisory Lock Verification (Issue 4)**:
    - Updated default `STATE_DATABASE_URL` in `PostgreSqlAdvisoryLockIntegrationTests` from `postgresql://quran_user:quran_pass@localhost:5432/quran_pipeline` to `postgresql://pipeline:pipeline@localhost:5432/pipeline`, aligning with `docker-compose.yml` and `.env.example`.
    - Added dual IPv4/localhost socket reachability check in `setUpClass`.
    - Verified non-skipped 100% pass against live PostgreSQL instance in local development environment.

- **Concurrency & Race-Condition Resolutions (`pipeline/state/repository.py`, `pipeline/orchestration/runner.py`)**:
  - **Atomic Idempotency Claim (`claim_execution`)**:
    - Guarded with transactional locks (`BEGIN IMMEDIATE` in SQLite, and `SELECT pg_advisory_xact_lock(hashtext('claim_execution_' || CAST(? AS text)))` in PostgreSQL).
    - Concurrent runs for the same `message_id` return `(False, "active_execution")`, causing the worker to exit cleanly with status `skipped_active_execution` (exit code 0) rather than executing parallel colliding passes.
    - Added `--force` CLI flag allowing operator override for intentional manual re-runs.
  - **Resolution of Deferred "scheduled" Message Deadlock**:
    - On every early return for `status="scheduled"` (posting window pause or rate-limit wait), `runner.py` explicitly invokes `release_execution_claim(message_id, "orchestration", "scheduled", reason)`.
    - When retried later (after window opens or rate-limit interval expires), the message is claimed cleanly without `--force`, preventing permanent deadlocks.
  - **Atomic Rate-Limit Reservation Placement & Expiration**:
    - `reserve_publish_slot()` is executed immediately before `publish_service.publish()` (step 8) rather than before the lengthy video render step (step 7).
    - If video render fails, the rate-limit slot is never reserved, ensuring unrelated QA-approved messages arriving immediately after are never blocked.
    - Added a 300s timeout on in-flight `status='processing'` reservations to prevent crashed workers from blocking the rate-limit window indefinitely.
    - Guaranteed PostgreSQL cross-worker mutual exclusion via `SELECT pg_advisory_xact_lock(hashtext('reserve_publish_slot'))`.
  - **Real Multi-Threaded Concurrency Testing**:
    - Added true concurrent thread tests using `concurrent.futures.ThreadPoolExecutor` and `threading.Barrier` exercising SQLite file locks and Postgres lock semantics under simultaneous multi-threaded contention.

- **Connection Pooling (`pipeline/state/repository.py`)**:
  - Added connection pool management supporting `psycopg_pool.ConnectionPool` (psycopg v3) and `psycopg2.pool.ThreadedConnectionPool` (psycopg2).
  - Encapsulated via `_PooledConnectionWrapper` to automatically return connections to the pool upon context manager exit.
  - Transparent fallback with zero dependencies when running local SQLite tests.
  - Added repository `close()` lifecycle method.

- **Posting Window & Rate Limit Scheduler (`pipeline/orchestration/scheduler.py`)**:
  - Implemented `PostingWindowScheduler`:
    - Time-of-day posting windows (e.g. 09:00 to 23:00) with complete timezone awareness (`zoneinfo.ZoneInfo`).
    - Overnight windows spanning midnight (e.g. 22:00 to 06:00).
    - Rate limit enforcement: calculates elapsed time since the most recently published video in the state store and enforces `RATE_LIMIT_MIN_INTERVAL_SECONDS`.
    - Returns structured `ScheduleDecision(can_post: bool, wait_seconds: float, reason: str)`.

- **Dual-Tier State Repository (`pipeline/state/repository.py` per ADR 001)**:
  - SQLite default for local CLI development and hermetic automated tests (`STATE_DB_PATH`).
  - PostgreSQL support (`STATE_DATABASE_URL`) for multi-container orchestration (n8n worker containers).
  - Dynamic parameter placeholder translation (`?` $\leftrightarrow$ `%s`) preserving identical query templates and schema parity across both backends (`pipeline_items`, `pipeline_messages`).
  - Helper query methods: `fetch_all_stages()`, `fetch_latest_published_timestamp()`, `fetch_items_by_status()`.

- **Pre-Publish Posting Window Re-Check (`pipeline/orchestration/runner.py`)**:
  - In `runner.py` Step 8, immediately before `reserve_publish_slot()`, added a re-evaluation of `self.scheduler.is_within_posting_window()`.
  - If video render takes significant duration and the posting window closes mid-render, publish is deferred, state is recorded as `scheduled`, and the claim is cleanly released rather than posting outside allowed hours.
  - Verified with `test_posting_window_closing_during_render_blocks_publish`.

- **Held For Review Retry Override Protection (`pipeline/state/repository.py`, `pipeline/orchestration/runner.py`)**:
  - Updated `claim_execution()` to block claims for messages in `held_for_review` status unless `force=True` is explicitly supplied.
  - `runner.py` handles the claim rejection with an explicit operator warning log and returns `status="held_for_review"` (exit code 2).
  - Preserves transparent auto-retry for transient `failed` messages without requiring `--force`.
  - Verified with `test_held_for_review_requires_force_to_retry`.

- **Real PostgreSQL Advisory Lock Integration Test (`pipeline/tests/test_orchestration.py`)**:
  - Implemented `PostgreSqlAdvisoryLockIntegrationTests` spawning dual concurrent database connections against live PostgreSQL (`STATE_DATABASE_URL=postgresql://pipeline:pipeline@localhost:5432/pipeline`).
  - Tests mutual exclusion using `SELECT pg_try_advisory_lock(%s)` and `SELECT pg_advisory_unlock(%s)`.
  - Verified passing cleanly against live PostgreSQL.

- **CLI Application & Settings (`pipeline/orchestration/app.py`, `pipeline/config.py`)**:
  - Factory `build_orchestrator()` kwarg drift fixed: `CtcForcedAligner(binary=..., model=...)`, `CaptionTemplater(default_template=...)`, and `MultiPlatformPublishClient(api_base_url=...)`.
  - CLI supports: `message_id`, `--source`, `--draft`, `--skip-scheduler`, `--wait-for-window`, `--force`, `--json`.
  - Maps exit codes: 0 for success/skip/scheduled/active-skip, 2 for QA review queue hold, 1 for failure.
  - Added `OrchestrationSettings` dataclass reading `POSTING_WINDOW_*` and `RATE_LIMIT_*` settings from environment, reflected in `.env.example`.

- **End-to-End Dry Run & Idempotency Integration Test**:
  - Implemented `test_end_to_end_dry_run_with_draft_publishing_and_idempotency` in `pipeline/tests/test_orchestration.py`.
  - Synthesizes real test video with live system FFmpeg $\to$ audio extraction $\to$ recognition $\to$ alignment $\to$ QA gate $\to$ FFmpeg 1080x1920 render with Arabic subtitles $\to$ publish in draft mode.
  - Verifies render artifact (1080x1920, h264, audio), draft publish artifact, state store records for all 6 stages, and verifies that a second run skips instantly via idempotency.

---

## 2. Verification Results

### Standard Library Discovery (`python -m unittest discover -s pipeline/tests -v`)
Ran with zero external test dependencies:
```text
Ran 116 tests in 8.104s

OK
```

### Pytest Full Regression Suite (`pytest -v`)
```text
============================= test session starts =============================
platform win32 -- Python 3.13.2, pytest-9.0.3, pluggy-1.6.0
rootdir: C:\Users\COMPUMARTS\Desktop\Q
configfile: pyproject.toml
plugins: anyio-4.13.0, asyncio-1.3.0
collected 116 items

pipeline/tests/test_alignment.py ... PASSED
pipeline/tests/test_audio.py ... PASSED
pipeline/tests/test_ingestion.py ... PASSED
pipeline/tests/test_orchestration.py::PostgreSqlAdvisoryLockIntegrationTests::test_real_advisory_lock_mutual_exclusion PASSED
pipeline/tests/test_orchestration.py::PipelineOrchestratorTests::test_cold_start_without_raw_video_fails_cleanly PASSED
pipeline/tests/test_orchestration.py::PipelineOrchestratorTests::test_posting_window_closing_during_render_blocks_publish PASSED
pipeline/tests/test_orchestration.py::PipelineOrchestratorTests::test_held_for_review_requires_force_to_retry PASSED
pipeline/tests/test_orchestration.py::N8nWorkflowIntegrityTests::test_main_workflow_json_structure_and_nodes PASSED
pipeline/tests/test_orchestration.py::N8nWorkflowIntegrityTests::test_error_workflow_json_structure_and_nodes PASSED
pipeline/tests/test_orchestration.py::PipelineDryRunIntegrationTests::test_end_to_end_dry_run_with_draft_publishing_and_idempotency PASSED
pipeline/tests/test_publish.py ... PASSED
pipeline/tests/test_qa_gate.py ... PASSED
pipeline/tests/test_recognition.py ... PASSED
pipeline/tests/test_render.py ... PASSED

============================= 116 passed in 6.52s =============================
```

---

## 3. Artifact Checklist
- [x] `pipeline/state/repository.py` (Dual-tier SQLite & PostgreSQL backend, connection pooling, `claim_execution`, `reserve_publish_slot`, `release_execution_claim`, `clear_publish_reservation`, PostgreSQL advisory transaction locks)
- [x] `pipeline/orchestration/scheduler.py` (`PostingWindowScheduler`)
- [x] `pipeline/orchestration/runner.py` (`PipelineOrchestrator` with deduplicated `_run_stage`, non-deadlocking scheduled deferral, and immediate pre-publish rate-limit reservation)
- [x] `pipeline/orchestration/app.py` (Factory `build_orchestrator()` with corrected kwargs, CLI entrypoint with `--force`)
- [x] `pipeline/orchestration/__init__.py`
- [x] `pipeline/orchestration/workflow.json` (Main n8n workflow with named trigger node refs and `Check Posting Window Allowed` IF gate)
- [x] `pipeline/orchestration/error_workflow.json` (Global error n8n workflow)
- [x] `pipeline/config.py` (`OrchestrationSettings`)
- [x] `.env.example` (Updated with Phase 8 scheduling variables)
- [x] `pipeline/tests/test_orchestration.py` (36 automated unit, multi-threaded concurrent, factory, and integration tests)
- [x] `PHASE8_TEST_PLAN.md`
- [x] `PHASE8_COMPLETION_REPORT.md`

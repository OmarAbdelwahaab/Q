# Phase 8 Completion Report — Workflow Orchestration, Scheduling & Dual-Tier State

**Status:** Implementation complete; all 106 unit, integration, dry-run, concurrency, and CLI tests passing cleanly under both `python -m unittest discover` and `pytest -v`.  
**Date:** 2026-09-13

---

## 1. Summary of Deliverables & Refinements

- **Complete 7-Stage Pipeline Orchestrator (`pipeline/orchestration/runner.py`)**:
  - Implemented `PipelineOrchestrator` coordinating the full lifecycle for a given video message:
    1. Idempotency check & atomic claim (`claim_execution`).
    2. Audio extraction (`AudioExtractionService` $\to$ 16kHz mono WAV).
    3. Verse recognition (`RecognitionService` $\to$ canonical Quran match JSON).
    4. Forced alignment (`AlignmentService` $\to$ word timestamps).
    5. QA Gate verification (`QAGateService`). If rejected/held, execution halts immediately, state is recorded as `rejected`, alerts are dispatched, and render/publish are **never executed**.
    6. Scheduler evaluation & atomic rate-limit slot reservation (`reserve_publish_slot`).
    7. Video render (`RenderService` composing background, HarfBuzz RTL karaoke ASS subtitles, audio, and branding into 1080x1920 MP4).
    8. Multi-platform publish (`PublishService` uploading to public URL and posting via Ayrshare in live or draft mode).
  - Clean error boundaries: any unhandled exception in any stage marks state as `failed`, alerts via `AlertService`, and halts progression for that item without crashing other jobs.
  - **Refactored `run()` boilerplate**: Extracted `_run_stage` consolidating error handling, logging, state store upsert, and alert dispatch, eliminating ~250 lines of duplicate code across all 6 stages.
  - **Cleaned dead imports**: Removed unused `datetime, timezone` imports from `runner.py`.

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

- **Production n8n Workflows (`pipeline/orchestration/`)**:
  - `pipeline/orchestration/workflow.json`:
    - Full n8n workflow export connecting Telegram trigger $\to$ Ingest $\to$ Audio $\to$ Recognition $\to$ Alignment $\to$ QA Gate $\to$ IF Approved $\to$ Posting Window Check $\to$ IF Allowed $\to$ Render $\to$ Publish.
    - All downstream nodes use explicit named trigger node reference `{{$('Telegram Video Trigger').item.json.message.message_id}}` rather than the prior `$json["message"]["message_id"]` expression.
    - `Posting Window & Rate Limit Check` one-liner explicitly returns exit code (`sys.exit(0 if d.can_post else 1)`), feeding into `Check Posting Window Allowed` IF node gating Render/Publish execution.
    - False branch of QA Gate routes directly to `Alert QA Review Queue` HTTP/Telegram notification.
    - Configured with `settings.errorWorkflow = "Quran Pipeline Error Handler"`.
  - `pipeline/orchestration/error_workflow.json`:
    - Global catch-all error workflow capturing failure events from any node in the pipeline.
    - Formats error context (`node`, `message_id`, `error`, `timestamp`).
    - Updates state store to `failed`.
    - Dispatches high-priority alert to monitoring webhook/Telegram.

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
Ran 112 tests in 6.396s

OK
```

### Pytest Full Regression Suite (`pytest -v`)
```text
============================= test session starts =============================
platform win32 -- Python 3.13.2, pytest-8.3.4, pluggy-1.5.0
rootdir: C:\Users\COMPUMARTS\Desktop\Q
configfile: pyproject.toml
plugins: asyncio-0.25.3
asyncio: mode=Mode.AUTO
collected 112 items

pipeline/tests/test_alignment.py::AlignmentTests::test_align_calculates_coverage_and_persists_artifact PASSED [  0%]
...
pipeline/tests/test_orchestration.py::PipelineStateRepositoryTests::test_concurrent_threads_claim_execution_mutual_exclusion PASSED [ 21%]
pipeline/tests/test_orchestration.py::PipelineStateRepositoryTests::test_concurrent_threads_reserve_publish_slot_mutual_exclusion PASSED [ 22%]
pipeline/tests/test_orchestration.py::PipelineStateRepositoryTests::test_postgres_advisory_lock_queries PASSED [ 23%]
pipeline/tests/test_orchestration.py::PipelineOrchestratorTests::test_scheduled_outcome_retry_does_not_deadlock PASSED [ 39%]
pipeline/tests/test_orchestration.py::PipelineOrchestratorTests::test_downstream_render_failure_does_not_dangle_publish_reservation PASSED [ 40%]
pipeline/tests/test_orchestration.py::N8nWorkflowIntegrityTests::test_main_workflow_json_structure_and_nodes PASSED [ 41%]
pipeline/tests/test_orchestration.py::OrchestrationCLITests::test_build_orchestrator_real_instantiation PASSED [ 41%]
pipeline/tests/test_orchestration.py::PipelineDryRunIntegrationTests::test_end_to_end_dry_run_with_draft_publishing_and_idempotency PASSED [ 45%]
...
pipeline/tests/test_render.py::TestRenderRealFFmpegIntegration::test_real_ffmpeg_end_to_end_render PASSED [100%]

============================= 112 passed in 6.48s =============================
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

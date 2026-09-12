# Phase 8 Completion Report — Workflow Orchestration, Scheduling & Dual-Tier State

**Status:** Implementation complete; all 101 unit, integration, dry-run, and CLI tests passing cleanly under both `python -m unittest discover` and `pytest -v`.  
**Date:** 2026-09-12

---

## 1. Summary of Deliverables & Refinements

- **Complete 7-Stage Pipeline Orchestrator (`pipeline/orchestration/runner.py`)**:
  - Implemented `PipelineOrchestrator` coordinating the full lifecycle for a given video message:
    1. Idempotency check (`skipped_already_published` if already completed and artifact present).
    2. Audio extraction (`AudioExtractionService` $\to$ 16kHz mono WAV).
    3. Verse recognition (`RecognitionService` $\to$ canonical Quran match JSON).
    4. Forced alignment (`AlignmentService` $\to$ word timestamps).
    5. QA Gate verification (`QAGateService`). If rejected/held, execution halts immediately, state is recorded as `rejected`, alerts are dispatched, and render/publish are **never executed**.
    6. Scheduler evaluation (`PostingWindowScheduler` checking time-of-day window and rate limits).
    7. Video render (`RenderService` composing background, HarfBuzz RTL karaoke ASS subtitles, audio, and branding into 1080x1920 MP4).
    8. Multi-platform publish (`PublishService` uploading to public URL and posting via Ayrshare in live or draft mode).
  - Clean error boundaries: any unhandled exception in any stage marks state as `failed`, alerts via `AlertService`, and halts progression for that item without crashing other jobs.

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
  - Added helper query methods for orchestration and monitoring:
    - `fetch_all_stages(message_id)`
    - `fetch_latest_published_timestamp()`
    - `fetch_items_by_status(stage, status)`

- **Production n8n Workflows (`pipeline/orchestration/`)**:
  - `pipeline/orchestration/workflow.json`:
    - Full n8n workflow export connecting Telegram trigger $\to$ Ingest $\to$ Audio $\to$ Recognition $\to$ Alignment $\to$ QA Gate $\to$ IF Approved $\to$ Scheduler $\to$ Render $\to$ Publish.
    - False branch of QA Gate routes directly to `Alert QA Review Queue` HTTP/Telegram notification.
    - Configured with `settings.errorWorkflow = "Quran Pipeline Error Handler"`.
  - `pipeline/orchestration/error_workflow.json`:
    - Global catch-all error workflow capturing failure events from any node in the pipeline.
    - Formats error context (`node`, `message_id`, `error`, `timestamp`).
    - Updates state store to `failed`.
    - Dispatches high-priority alert to monitoring webhook/Telegram.

- **CLI Application & Settings (`pipeline/orchestration/app.py`, `pipeline/config.py`)**:
  - CLI supports: `message_id`, `--source`, `--draft`, `--skip-scheduler`, `--wait-for-window`, `--json`.
  - Maps exit codes: 0 for success/skip/scheduled, 2 for QA review queue hold, 1 for failure.
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
Ran 101 tests in 3.998s

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
collected 101 items

pipeline/tests/test_alignment.py::AlignmentTests::test_align_calculates_coverage_and_persists_artifact PASSED [  0%]
...
pipeline/tests/test_orchestration.py::PipelineDryRunIntegrationTests::test_end_to_end_dry_run_with_draft_publishing_and_idempotency PASSED [ 39%]
...
pipeline/tests/test_render.py::TestRenderRealFFmpegIntegration::test_real_ffmpeg_end_to_end_render PASSED [100%]

============================= 101 passed in 4.75s =============================
```

---

## 3. Artifact Checklist
- [x] `pipeline/state/repository.py` (Dual-tier SQLite & PostgreSQL backend per ADR 001)
- [x] `pipeline/orchestration/scheduler.py` (`PostingWindowScheduler`)
- [x] `pipeline/orchestration/runner.py` (`PipelineOrchestrator`)
- [x] `pipeline/orchestration/app.py` (CLI entrypoint)
- [x] `pipeline/orchestration/__init__.py`
- [x] `pipeline/orchestration/workflow.json` (Main n8n workflow)
- [x] `pipeline/orchestration/error_workflow.json` (Global error n8n workflow)
- [x] `pipeline/config.py` (`OrchestrationSettings`)
- [x] `.env.example` (Updated with Phase 8 scheduling variables)
- [x] `pipeline/tests/test_orchestration.py` (25 automated unit & integration tests)
- [x] `PHASE8_TEST_PLAN.md`
- [x] `PHASE8_COMPLETION_REPORT.md`

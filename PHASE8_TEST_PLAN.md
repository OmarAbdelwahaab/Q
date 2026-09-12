# Phase 8 Test Plan — Workflow Orchestration, Scheduling & Dual-Tier State

## 1. Overview & Objectives
Phase 8 validates the end-to-end workflow orchestration, time-of-day posting-window and rate-limit scheduling, dual-tier state repository (SQLite for local runs/tests, PostgreSQL for multi-container orchestration per ADR 001), and production n8n workflows (SPEC §4.8):
- **Full 7-Stage Pipeline Orchestration (`PipelineOrchestrator`)**:
  - Coordinates Telegram trigger / ingestion $\to$ audio extraction $\to$ verse recognition $\to$ forced alignment $\to$ QA gate verification $\to$ scheduler check $\to$ video render $\to$ multi-platform publishing.
  - Fail-closed error handling: if any stage fails or raises an unhandled exception, execution halts immediately, state is recorded as `failed` with error diagnostics, and alerts are dispatched.
  - Strict QA Gate branching: when QA Gate holds or rejects an item, video render and multi-platform publishing are **never executed**.
- **Posting Window & Rate Limit Scheduler (`PostingWindowScheduler`)**:
  - Enforces daily time-of-day posting windows (e.g. 09:00 to 23:00) with full timezone awareness (`zoneinfo.ZoneInfo`).
  - Supports overnight windows spanning midnight (e.g. 22:00 to 06:00).
  - Enforces inter-post rate limits (e.g. 1800s minimum interval) based on the latest published item timestamp in the state store.
  - Generates actionable scheduling decisions (`ScheduleDecision(can_post, wait_seconds, reason)`).
- **Dual-Tier State Repository (`PipelineStateRepository` per ADR 001)**:
  - Supports SQLite (`STATE_DB_PATH`) for hermetic local testing and CLI runs with zero daemon prerequisites.
  - Supports PostgreSQL (`STATE_DATABASE_URL`) for multi-container orchestration with connection pooling and row-level concurrency.
  - SQL dialect placeholder abstraction (`?` for SQLite, `%s` for PostgreSQL) with schema parity across both backends.
  - Helper query methods for orchestration: `fetch_all_stages()`, `fetch_latest_published_timestamp()`, `fetch_items_by_status()`.
- **Production n8n Workflows**:
  - `workflow.json`: Main workflow connecting Telegram trigger $\to$ Ingestion $\to$ Audio $\to$ Recognition $\to$ Alignment $\to$ QA Gate $\to$ IF `exitCode == 0` (Approved) $\to$ Posting Window & Rate Limit Check $\to$ Render $\to$ Publish; False branch triggers review queue alert.
  - `error_workflow.json`: Global error workflow triggered on any node failure, formatting error context, updating the state store to `failed`, and dispatching high-priority alerts.
- **End-to-End Dry Run & Idempotency**:
  - Synthetic video through live FFmpeg audio extraction $\to$ recognition $\to$ alignment $\to$ QA gate approval $\to$ FFmpeg 1080x1920 MP4 render with HarfBuzz Arabic subtitles and branding $\to$ publish in draft mode with stub client.
  - Second execution verifies instant skip via idempotency check.
- **Portability & Zero-Dependency Test Execution**:
  - All unit and integration tests written in pure standard library `unittest.TestCase` with `asyncio.run(...)`, passing cleanly under `python -m unittest discover` and `pytest -v`.

---

## 2. Test Scope & Matrix

| Category | Test Case | Target Component | Expected Behavior |
|---|---|---|---|
| **Scheduler** | Disabled allows posting anytime | `PostingWindowScheduler` | Returns `can_post=True`, `wait_seconds=0.0` at any hour |
| **Scheduler** | Daytime window open | `PostingWindowScheduler` | Inside 09:00–23:00 returns `can_post=True`, `wait_seconds=0.0` |
| **Scheduler** | Outside window before start | `PostingWindowScheduler` | At 07:00 UTC with 09:00 start returns `can_post=False`, wait 7200s |
| **Scheduler** | Outside window after end | `PostingWindowScheduler` | At 23:30 UTC with 23:00 end returns `can_post=False`, wait until tomorrow 09:00 |
| **Scheduler** | Overnight window across midnight | `PostingWindowScheduler` | Window 22:00–06:00 open at 23:00 & 04:00, closed at 12:00 (wait 36000s) |
| **Scheduler** | Rate limit calculation | `PostingWindowScheduler` | Computes exact remaining wait time against previous publication timestamp |
| **Scheduler** | Rate limit active when window open | `PostingWindowScheduler` | Window open but recent post active returns `can_post=False` with wait time |
| **Scheduler** | Timezone conversion | `PostingWindowScheduler` | Correctly offsets UTC datetimes to `Africa/Cairo` (+3) local hours |
| **Scheduler** | Invalid parameter validation | `PostingWindowScheduler` | Raises `ValueError` for hours outside 0..23 or negative intervals |
| **State Store** | SQLite full lifecycle | `PipelineStateRepository` | Upserts metadata, stages, orders all stages, queries latest timestamp |
| **State Store** | SQL parameter translation | `PipelineStateRepository` | Preserves `?` for SQLite and translates to `%s` for PostgreSQL |
| **State Store** | PostgreSQL missing driver | `PipelineStateRepository` | Raises clear `ImportError` when neither psycopg nor psycopg2 is available |
| **State Store** | Row to dict conversion helper | `PipelineStateRepository` | Safely converts None, dicts, Row objects, and tuple rows |
| **Orchestrator** | Happy path execution | `PipelineOrchestrator` | Runs audio $\to$ recognition $\to$ alignment $\to$ QA gate $\to$ render $\to$ publish |
| **Orchestrator** | Idempotency enforcement | `PipelineOrchestrator` | Skips already published message without executing any stage |
| **Orchestrator** | QA Gate rejection halt | `PipelineOrchestrator` | Rejection stops pipeline immediately; render and publish are never called |
| **Orchestrator** | Stage failure halt & alert | `PipelineOrchestrator` | Stage failure stops downstream execution, alerts, and returns failed status |
| **Orchestrator** | Scheduler pauses when closed | `PipelineOrchestrator` | Outside window pauses before render/publish, records `scheduled` state |
| **Orchestrator** | Wait for window delay | `PipelineOrchestrator` | Waits duration via sleep callback then completes render and publish |
| **n8n Workflow** | Main workflow structure | `workflow.json` | Valid JSON, contains all 11 required nodes with correct connections and error link |
| **n8n Workflow** | Error workflow structure | `error_workflow.json` | Valid JSON, contains Error Trigger $\to$ Format $\to$ Update State $\to$ Alert |
| **CLI & Config** | Argument parsing | `pipeline.orchestration.app` | Parses `--draft`, `--skip-scheduler`, `--wait-for-window`, `--json` |
| **CLI & Config** | Environment loading | `OrchestrationSettings` | Parses posting window hours, timezone, and rate limit intervals |
| **CLI & Config** | Exit code mapping | `pipeline.orchestration.app` | Returns 0 on success/skip, 2 on review queue hold, 1 on failure |
| **End-to-End** | Dry run & idempotency | Full Pipeline | Renders real 1080x1920 MP4, publishes in draft, records state, verifies skip |

---

## 3. Execution Commands

### Run Full Test Suite via Standard Library (Zero External Dependencies)
```powershell
python -m unittest discover -s pipeline/tests -v
```

### Run Pytest Suite
```powershell
pytest -v
```

### Run CLI in Dry-Run / Draft Mode
```powershell
python -m pipeline.orchestration.app <message_id> --draft --skip-scheduler
```

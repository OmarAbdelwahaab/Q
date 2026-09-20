# Phase 9 Test Plan — Monitoring, Alerting, Concurrency Load & Hardening

## 1. Overview & Objectives
Phase 9 validates system monitoring, automated health alerting, burst concurrency load handling, and operational resilience (TASKS.md Phase 9 & SPEC.md §6):
- **Lightweight Status Reporting (`PipelineMonitor`)**:
  - Provides aggregated operational metrics: total messages processed, stage-by-stage status breakdown (`completed`, `failed`, `held_for_review`, `scheduled`, `processing`), review queue backlog count, active execution count, and latest publication timestamp.
  - Outputs human-readable Markdown format optimized for chat notifications (Telegram/Slack) and structured JSON for automated tooling.
  - Exposes on-demand CLI commands: `python -m pipeline.monitoring.app report [--json] [--send-alert] [--window-hours N]`.
- **Automated Health & Backlog Alerting**:
  - Continuously monitors state health against configurable thresholds:
    - Consecutive/repeated failure threshold (default: 3 failures).
    - Review queue backlog threshold (default: 5 items held for review).
  - Automatically dispatches alerts through `AlertService` (Telegram bot and webhook) when thresholds are breached.
  - Implements alert cooldown tracking to prevent repetitive notification spam.
  - Exposes health-check CLI command: `python -m pipeline.monitoring.app check [--send-alert] [--json]` returning exit code 0 (healthy) or 2 (threshold breached).
- **Chat-Bot Command Handler (`MonitoringBotHandler`)**:
  - Intercepts operator commands (`/status`, `/health`, `/queue`, `/retry <message_id>`).
  - Returns concise, actionable Markdown diagnostics for chat operations.
- **Burst Concurrency Load Testing (`test_load.py`)**:
  - Simulates a burst of 16 simultaneous video arrivals via `ThreadPoolExecutor` and `threading.Barrier`.
  - Verifies atomic claiming (`claim_execution`) prevents duplicate passes for identical IDs.
  - Verifies publishing rate-limiting (`reserve_publish_slot`) serializes publication slots under concurrent contention.
  - Verifies posting-window deferral defers all burst items cleanly without worker lock contention or deadlocks.
- **Operational Runbook Validation (`docs/RUNBOOK.md`)**:
  - Validates operational documentation against actual codebase CLI flags (`--force`, `--force-active`, `--platforms`), configuration variables, and troubleshooting steps.

---

## 2. Test Scope & Matrix

| Category | Test Case | Target Component | Expected Behavior |
|---|---|---|---|
| **State Metrics** | Aggregated pipeline summary | `PipelineStateRepository.get_pipeline_summary` | Counts distinct messages, calculates stage breakdown, counts active & review items |
| **State Metrics** | Windowed summary filtering | `PipelineStateRepository.get_pipeline_summary` | Restricts metrics to items updated within `window_hours` |
| **State Metrics** | Fetch recent failures | `PipelineStateRepository.fetch_recent_failures` | Returns up to N failed stage items with message ID, stage, and error details |
| **State Metrics** | Count consecutive failures | `PipelineStateRepository.count_consecutive_failures` | Counts consecutive failed runs up to limit; resets count on successful run |
| **State Metrics** | Review queue backlog count | `PipelineStateRepository.fetch_review_queue_count` | Returns exact count of items held in `qa_gate` as `held_for_review` |
| **Monitoring** | Status report generation (Text) | `PipelineMonitor.format_text_report` | Renders clean Markdown summary with stage counts, review count, and recent errors |
| **Monitoring** | Status report generation (JSON) | `PipelineMonitor.format_json_report` | Returns valid structured dictionary representation of pipeline metrics |
| **Health Check** | Healthy system verification | `PipelineMonitor.check_health` | Returns `is_healthy=True` when failures and backlog are below thresholds |
| **Health Check** | Repeated failure breach | `PipelineMonitor.check_health` | Detects $\ge N$ consecutive failures; returns `is_healthy=False` with descriptive reason |
| **Health Check** | Review queue backlog breach | `PipelineMonitor.check_health` | Detects $> M$ items held for review; returns `is_healthy=False` with descriptive reason |
| **Health Check** | Alert dispatch on breach | `PipelineMonitor.dispatch_health_alerts` | Sends alert via `AlertService` when threshold breached |
| **Health Check** | Alert cooldown suppression | `PipelineMonitor.dispatch_health_alerts` | Skips redundant alert dispatch if called again within cooldown window |
| **Chat Bot** | Command `/status` | `MonitoringBotHandler.handle_command` | Returns formatted pipeline status overview |
| **Chat Bot** | Command `/health` | `MonitoringBotHandler.handle_command` | Returns system health verdict and threshold status |
| **Chat Bot** | Command `/queue` | `MonitoringBotHandler.handle_command` | Lists message IDs currently held for review with timestamps |
| **Chat Bot** | Command `/retry <id>` | `MonitoringBotHandler.handle_command` | Provides instructions / CLI command to re-run specified message with `--force` |
| **Chat Bot** | Unknown command | `MonitoringBotHandler.handle_command` | Returns available command list and usage help |
| **Monitoring CLI**| CLI report command | `pipeline.monitoring.app` | Prints text report or JSON to stdout; exits with code 0 |
| **Monitoring CLI**| CLI check command healthy | `pipeline.monitoring.app` | Exits with code 0 when pipeline is healthy |
| **Monitoring CLI**| CLI check command breach | `pipeline.monitoring.app` | Exits with code 2 when health thresholds are breached |
| **Monitoring CLI**| CLI send-alert flag | `pipeline.monitoring.app` | Dispatches alert through configured `AlertService` |
| **Burst Load** | Concurrent burst execution | `test_burst_concurrent_video_processing` | 16 concurrent distinct videos; asserts safe claim and clean state transitions |
| **Burst Load** | Duplicate message burst | `test_burst_duplicate_message_concurrency` | 16 concurrent calls for same message ID; exactly 1 claims, 15 skipped via `active_execution` |
| **Burst Load** | Concurrent publish rate limit | `test_burst_rate_limiting_mutual_exclusion` | 16 concurrent approved items; exactly 1 reserves immediate slot, others receive rate-limit delay |
| **Burst Load** | Closed posting window burst | `test_burst_posting_window_closed_deferral` | 16 concurrent items during closed window; all defer to `scheduled` cleanly without errors |

---

## 3. Verification Commands

```bash
# 1. Targeted Monitoring Tests
python -m unittest pipeline.tests.test_monitoring -v

# 2. Concurrency Burst Load Tests
python -m unittest pipeline.tests.test_load -v

# 3. Full Standard Library Test Suite Discovery
python -m unittest discover -s pipeline/tests -v

# 4. Pytest Full Regression Suite
pytest -v

# 5. CLI Verification
python -m pipeline.monitoring.app report --json
python -m pipeline.monitoring.app check
```

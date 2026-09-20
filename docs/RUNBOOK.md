# Operations Runbook — Quran Video Repurposing Pipeline

This runbook provides actionable procedures for operators maintaining, triaging, and configuring the automated Quran video repurposing pipeline.

---

## 1. Quick Reference & System Health

### Monitoring Commands

```bash
# 1. Generate human-readable operational status report (default: 24h window)
python -m pipeline.monitoring.app report

# 2. Generate JSON status report for integration with external dashboards
python -m pipeline.monitoring.app report --json --window-hours 12

# 3. Check health against configured thresholds (exits 0 if healthy, 2 if breached)
python -m pipeline.monitoring.app check

# 4. Check health and trigger alert notifications if thresholds are breached
python -m pipeline.monitoring.app check --send-alert
```

### Chat-Bot Admin Commands (Telegram / Slack)

| Command | Description | Example |
|---|---|---|
| `/status [hours]` | Displays stage breakdown, active workers, review queue count, and recent failures | `/status 48` |
| `/health` | Evaluates system health against failure and backlog thresholds | `/health` |
| `/queue` | Lists items held in QA Gate awaiting operator inspection | `/queue` |
| `/retry <message_id>` | Generates exact re-run instructions for a specific item | `/retry 105` |
| `/help` | Displays available admin commands | `/help` |

---

## 2. Reprocessing a Failed or Stalled Item

### Diagnostic Investigation

When an item fails, the pipeline records the failure stage and error message in the state repository and emits an alert.

1. **Check error details via CLI**:
   ```bash
   python -m pipeline.monitoring.app report --json
   ```
2. **Inspect database directly** (SQLite dev / Postgres prod):
   ```sql
   -- SQLite
   sqlite3 pipeline/state/pipeline.db "SELECT stage, status, error, updated_at FROM pipeline_items WHERE message_id = 105 ORDER BY updated_at DESC;"

   -- PostgreSQL
   psql -d pipeline -c "SELECT stage, status, error, updated_at FROM pipeline_items WHERE message_id = 105 ORDER BY updated_at DESC;"
   ```
3. **Inspect artifact directories**:
   - `storage/raw/{message_id}.mp4` — Original ingested video.
   - `storage/audio/{message_id}.wav` — Extracted audio.
   - `storage/match/{message_id}.json` — Verse recognition match.
   - `storage/align/{message_id}.json` — Phoneme/word alignment timings.
   - `storage/qa_gate/review_{message_id}.json` — QA Gate evaluation diagnostics.
   - `storage/render/{message_id}.mp4` — Rendered vertical video.
   - `storage/publish/{message_id}.json` — Multi-platform publishing receipts.

### Re-running Execution

To execute the pipeline for an item:

```bash
# Standard re-run (for failed or deferred items)
python -m pipeline.orchestration.app <message_id>

# Re-run providing a new or updated raw video file
python -m pipeline.orchestration.app <message_id> --source /path/to/video.mp4

# Test run in sandbox/draft mode (does not post live)
python -m pipeline.orchestration.app <message_id> --draft
```

### Clearing Stuck Execution Claims (`--force` and `--force-active`)

The pipeline enforces atomic idempotency via `claim_execution()`. To prevent double-processing:
- A completed or published video cannot be re-run without `--force`.
- An item held in QA Gate (`held_for_review`) cannot be re-run without `--force`.
- An item marked `processing` by an active worker is locked for 300 seconds.

If a worker crashed or was killed abruptly while processing an item, the claim lock remains active until the 300-second window expires:

```bash
# Override a completed or held_for_review item:
python -m pipeline.orchestration.app <message_id> --force

# Override an actively locked item (<300s since worker crash):
python -m pipeline.orchestration.app <message_id> --force --force-active
```

---

## 3. Operating the QA Gate Review Queue

When automated verse recognition confidence or forced alignment coverage falls below configured thresholds, the pipeline halts before rendering and holds the item in `held_for_review`.

### Viewing Held Items

1. In chat, send `/queue`.
2. Via CLI:
   ```bash
   python -c "from pipeline.state.repository import PipelineStateRepository; r = PipelineStateRepository(); print(r.fetch_review_queue_items())"
   ```
3. Read the review artifact:
   ```bash
   cat storage/qa_gate/review_{message_id}.json
   ```

### Review Evaluation Criteria

Open `storage/qa_gate/review_{message_id}.json` and check:
- `match_confidence`: Score from 0.00 to 1.00 comparing transcript against Quranic corpus.
  - If score $\ge 0.85$ and the recitation is verified by ear to be correct, approve.
  - If score $< 0.85$ due to non-Quranic intro/outro speech or heavy background echo, operator discretion applies.
- `alignment_coverage`: Fraction of words aligned with phoneme audio timestamps.
  - If coverage is low because of an unrecited title or intro speech, check if the Quranic portion itself aligns accurately.

### Approving or Rejecting Held Items

- **To Approve & Continue Pipeline**:
  Run the orchestrator with `--force`. The pipeline will claim the held item, proceed through rendering, and publish:
  ```bash
  python -m pipeline.orchestration.app <message_id> --force
  ```
- **To Reject & Terminate**:
  Mark the item as permanently failed in the database:
  ```bash
  python -c "from pipeline.state.repository import PipelineStateRepository; r = PipelineStateRepository(); r.upsert_stage(<message_id>, 'qa_gate', 'failed', 'Operator rejected: poor audio quality'); r.upsert_stage(<message_id>, 'orchestration', 'failed', 'Operator rejected: poor audio quality')"
  ```

---

## 4. Adjusting QA Thresholds

Thresholds balance quality assurance against review queue volume.

### Configuration Variables (`.env`)

```ini
# Minimum verse match confidence (0.0 to 1.0, default: 0.90)
QA_MATCH_CONFIDENCE_THRESHOLD=0.90

# Minimum alignment word coverage (0.0 to 1.0, default: 0.95)
QA_ALIGNMENT_COVERAGE_THRESHOLD=0.95
```

### Tuning Guidance

- **High False-Positive Holds (Queue Backlog Growing)**:
  If clear recitations are frequently held due to dialect variations or whisper ASR acoustic nuances:
  - Lower `QA_MATCH_CONFIDENCE_THRESHOLD` from `0.90` to `0.85`.
  - Lower `QA_ALIGNMENT_COVERAGE_THRESHOLD` from `0.95` to `0.90`.
- **Quality Slippage (Inaccurate Subtitles Observed)**:
  If inaccurate words or mismatched ayahs are slipping into rendered videos:
  - Raise `QA_MATCH_CONFIDENCE_THRESHOLD` to `0.95`.
  - Raise `QA_ALIGNMENT_COVERAGE_THRESHOLD` to `0.98`.
- **Applying Changes**:
  Restart worker containers (`docker compose restart orchestrator n8n`) or reload the `.env` file in CLI sessions.

---

## 5. Adding & Removing Target Platforms

The publishing stage uses Ayrshare API to syndicate 1080x1920 vertical videos to configured social platforms.

### Configuration Variables (`.env`)

```ini
# Comma-separated list of target platform identifiers
PUBLISH_PLATFORMS=tiktok,instagram,youtube,facebook,x,telegram

# Sandbox / Draft Mode (true = test mode without public posting; false = live production)
PUBLISH_DRAFT_MODE=false

# Ayrshare API Base URL & API Key
PUBLISH_API_BASE_URL=https://app.ayrshare.com/api
PUBLISH_API_KEY=your_ayrshare_api_key_here

# Default hashtags appended to generated captions
PUBLISH_DEFAULT_HASHTAGS=#قرآن,#تلاوة,#قرآن_كريم,#Quran,#Islam
```

### Supported Platforms & Media Requirements

| Platform | Key | Max Duration | Video Constraints | Notes |
|---|---|---|---|---|
| **TikTok** | `tiktok` | 10 minutes | 9:16 (1080x1920) | Recommended: < 60s for Shorts/Reels algorithms |
| **Instagram** | `instagram` | 15 minutes | 9:16 (1080x1920) | Posts as Instagram Reel |
| **YouTube** | `youtube` | 60 seconds | 9:16 (1080x1920) | Posts as YouTube Short if $\le 60\text{s}$ |
| **Facebook** | `facebook` | 60 seconds | 9:16 (1080x1920) | Posts to connected Facebook Page |
| **X (Twitter)**| `x` | 140 seconds | 9:16 or 16:9 | Posts as native video tweet |
| **Telegram** | `telegram` | Unlimited | Any | Directly published to channel/group |

### Adding a Platform

1. Ensure the platform account is linked in your Ayrshare dashboard.
2. Add the platform key to `PUBLISH_PLATFORMS` in `.env`:
   ```ini
   PUBLISH_PLATFORMS=tiktok,instagram,youtube,facebook,x,telegram,pinterest
   ```
3. Validate publishing in draft mode:
   ```bash
   python -m pipeline.orchestration.app <message_id> --draft --force
   ```

### Removing a Platform

1. Remove the platform key from `PUBLISH_PLATFORMS` in `.env`:
   ```ini
   PUBLISH_PLATFORMS=tiktok,youtube
   ```
2. Future executions will only publish to the active list.

---

## 6. Monitoring & Alert Triage

### Alert Thresholds (`.env`)

```ini
# Consecutive failed runs before critical alert (default: 3)
MONITORING_FAILURE_THRESHOLD=3

# Maximum held items in QA review queue before backlog alert (default: 5)
MONITORING_BACKLOG_THRESHOLD=5

# Aggregation window in hours for status reports (default: 24)
MONITORING_WINDOW_HOURS=24

# Alert suppression cooldown in seconds (default: 3600 = 1 hour)
MONITORING_ALERT_COOLDOWN_SECONDS=3600
```

### Incident Triage Workflows

#### 1. Consecutive Failures Alert (`🚨 CRITICAL: Repeated Failures`)
- **Cause**: Upstream API outage (Ayrshare), corrupted model files, disk full, or missing FFmpeg dependencies.
- **Action**:
  1. Run `python -m pipeline.monitoring.app report` to inspect the failing stage and recent error messages.
  2. If failure is at `audio` or `render`: verify disk space and FFmpeg binary availability (`ffmpeg -version`).
  3. If failure is at `recognition`: check Quran corpus cache file `storage/cache/quran_corpus.json`.
  4. If failure is at `publish`: verify Ayrshare API key validity and quota.

#### 2. Review Backlog Alert (`⚠️ Growing Review Backlog`)
- **Cause**: Surge of videos with low acoustic match scores or unaligned introductory speech.
- **Action**:
  1. Run `/queue` in chat or query review items via CLI.
  2. Inspect audio of held videos. If quality is generally acceptable, consider lowering thresholds per Section 4.
  3. Batch re-run approved videos using `--force`.

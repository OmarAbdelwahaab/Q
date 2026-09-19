# Technical Requirements Document (TRD)
## Automated Quran Video Repurposing & Multi-Platform Publishing Pipeline

---

### Document Information
- **Project Name**: Automated Quran Video Repurposing & Publishing Pipeline (`OmarAbdelwahaab/Q`)
- **Document Version**: 1.0.0
- **Status**: Complete / Active
- **Target Audience**: Core Engineering, DevOps, Platform Operators, QA Engineers
- **Source Reference**: `SPEC.md`, `TASKS.md`, `GEMINI.md`, `docs/decisions/001-state-database-strategy.md`, `docs/RUNBOOK.md`

---

## 1. Executive Summary & System Objectives

### 1.1 Problem Statement
Raw Islamic prayer recordings and recitations posted to broadcast channels (e.g., Telegram) typically feature plain wide-angle video (stationary imam at a microphone, mosque interior, lower-third prayer labels) with uncaptioned Arabic recitation. Converting these recordings into modern, vertical short-form videos (9:16 aspect ratio, stylized visual canvas, word-by-word synchronized calligraphic Arabic typography, branded overlay, and multi-platform syndication) traditionally demands substantial manual video editing, Quranic verse transcription, audio timestamp alignment, and manual upload across fragmented social networks.

### 1.2 System Purpose
The **Quran Video Repurposing Pipeline** is an end-to-end autonomous media processing system that monitors an upstream Telegram source, extracts recitation audio, performs automatic speech recognition (ASR) against a canonical Quranic corpus, executes word-level forced alignment, validates text-audio correctness via an unbypassable QA Gate, renders production-quality 1080×1920 vertical videos with animated RTL karaoke subtitles, and automatically publishes to social networks with zero manual human interaction in the happy path.

### 1.3 Core Engineering Principles
1. **Quranic Text Integrity Above Throughput (Fail-Closed Architecture)**: Zero tolerance for incorrect Quranic ayahs, misattributed surahs, or dropped text. If verse recognition confidence or forced alignment coverage falls below strict thresholds, the item is held for manual review. Automation is never prioritized over theological correctness.
2. **Deterministic Idempotency**: Reprocessing an identical `message_id`—whether triggered concurrently by multi-worker webhooks, network retries, or manual execution—must never result in duplicate video renders or double-posts.
3. **Modular Subsystem Decoupling**: Each lifecycle stage (Ingest $\to$ Audio $\to$ Recognition $\to$ Alignment $\to$ QA Gate $\to$ Render $\to$ Publish $\to$ Monitor) is modeled as a standalone service with explicit input/output protocols, isolated artifact storage, and independent testability.
4. **Dual-Tier State Architecture (ADR 001)**: Lightweight, zero-daemon SQLite for local developer CLI execution and millisecond unit tests; PostgreSQL with transaction advisory locks (`pg_advisory_xact_lock`) and connection pooling for multi-container production environments.

---

## 2. End-to-End System Architecture

### 2.1 Topology & Stage Progression

```mermaid
flowchart TD
    subgraph Ingestion_Layer["1. Ingestion Layer"]
        TG["Telegram Channel"] -->|New Video Message| INGEST["IngestionService (Telethon / Bot API)"]
        INGEST -->|Write| RAW_STORE[("storage/raw/{message_id}.mp4")]
        INGEST -->|Register| STATE_INGEST["State: ingestion completed"]
    end

    subgraph Audio_Layer["2. Audio Extraction"]
        RAW_STORE --> AUDIO_SVC["AudioExtractionService (FFmpeg)"]
        AUDIO_SVC -->|16kHz Mono WAV| AUDIO_STORE[("storage/audio/{message_id}.wav")]
        AUDIO_SVC -->|Register| STATE_AUDIO["State: audio completed"]
    end

    subgraph Recognition_Layer["3. Recognition & Verse Matching"]
        AUDIO_STORE --> ASR["Whisper Large-v3 ASR"]
        ASR -->|Raw Arabic Transcript| MATCHER["QuranMatcher (Levenshtein / n-gram)"]
        CORPUS[("Tanzil Uthmani Quran Corpus")] --> MATCHER
        MATCHER -->|Match Metadata| MATCH_STORE[("storage/match/{message_id}.json")]
        MATCHER -->|Register| STATE_MATCH["State: recognition completed"]
    end

    subgraph Alignment_Layer["4. Forced Alignment"]
        AUDIO_STORE --> ALIGNER["CtcForcedAligner (Wav2Vec2 / MMS Arabic)"]
        MATCH_STORE --> ALIGNER
        ALIGNER -->|Word Timestamps ms| ALIGN_STORE[("storage/align/{message_id}.json")]
        ALIGNER -->|Register| STATE_ALIGN["State: alignment completed"]
    end

    subgraph QA_Gate_Layer["5. QA Gate Verification"]
        MATCH_STORE --> QA_GATE["QAGateService"]
        ALIGN_STORE --> QA_GATE
        QA_GATE -->|Evaluate Thresholds| QA_DECISION{Scores >= Thresholds?}
        QA_DECISION -- "Fail / Low Confidence" --> HOLD["State: qa_gate held_for_review"]
        HOLD --> ALERT_HOLD["Alert QA Review Queue (Telegram / Webhook)"]
        HOLD --> HALT["Halt Execution (Exit Code 2)"]
    end

    subgraph Scheduler_Layer["6. Scheduling & Rate Limiting"]
        QA_DECISION -- "Pass / Approved" --> SCHEDULER["PostingWindowScheduler"]
        SCHEDULER -->|Check Time Window| WIN_CHECK{In Posting Window?}
        WIN_CHECK -- "No" --> DEFER["State: orchestration scheduled"]
        DEFER --> RELEASE_CLAIM["Release Claim & Defer"]
        WIN_CHECK -- "Yes" --> RATE_CHECK{Rate Limit Cooldown?}
        RATE_CHECK -- "Active" --> DEFER
        RATE_CHECK -- "Clear" --> RESERVE["reserve_publish_slot()"]
    end

    subgraph Render_Layer["7. Video Rendering"]
        RESERVE --> RENDER_SVC["RenderService"]
        BG_POOL[("Asset Pool: Backgrounds")] --> RENDER_SVC
        BRAND[("Branding: Logo & Handle")] --> RENDER_SVC
        ALIGN_STORE -->|Karaoke Dialogue Events| SUB_GEN["KaraokeSubtitleGenerator (HarfBuzz + ASS)"]
        SUB_GEN --> RENDER_SVC
        RENDER_SVC -->|FFmpeg Complex Filtergraph| RENDER_STORE[("storage/render/{message_id}.mp4")]
        RENDER_SVC -->|Register| STATE_RENDER["State: render completed"]
    end

    subgraph Publish_Layer["8. Multi-Platform Syndication"]
        RENDER_STORE --> PUB_SVC["PublishService"]
        UPLOADER["MediaUploader (S3 / MinIO)"] --> PUB_SVC
        TEMPLATER["CaptionTemplater"] --> PUB_SVC
        PUB_SVC -->|Ayrshare REST API| AYRSHARE["Ayrshare Multi-Platform Gateway"]
        AYRSHARE --> TIKTOK["TikTok"]
        AYRSHARE --> IG["Instagram Reels"]
        AYRSHARE --> YT["YouTube Shorts"]
        AYRSHARE --> FB["Facebook"]
        AYRSHARE --> X["X (Twitter)"]
        PUB_SVC --> PUB_STORE[("storage/publish/{message_id}.json")]
        PUB_SVC --> STATE_PUB["State: publish completed"]
        STATE_PUB --> STATE_ORCH["State: orchestration completed"]
    end

    subgraph Monitoring_Layer["9. Monitoring & Admin"]
        STATE_STORE[("State Database: SQLite / PostgreSQL")] -.-> MONITOR["PipelineMonitor"]
        MONITOR --> BOT["MonitoringBotHandler (/status, /health, /queue, /retry)"]
        MONITOR --> MON_CLI["CLI: python -m pipeline.monitoring.app"]
        MONITOR --> HEALTH_ALERT["Automated Alerting (Repeated Failures / Backlog Surge)"]
    end
```

---

## 3. Subsystem Specifications

### 3.1 Ingestion & Source Acquisition Subsystem (`pipeline.ingestion`)
- **Purpose**: Detect, download, and persist raw video assets from upstream Telegram channels.
- **Components**:
  - `TelethonIngestionListener`: Standalone daemon using Telethon MTProto client (`API_ID`, `API_HASH`, `TELEGRAM_CHANNEL_ID`). Runs event loop listening for `events.NewMessage`.
  - `TelegramBotIngestionListener`: Alternate listener utilizing Telegram Bot API webhooks or long-polling.
  - `IngestionService`: Validates incoming video formats, enforces size constraints, writes to `storage/raw/{message_id}.mp4`, captures metadata (`channel_id`, `message_timestamp`, `duration_seconds`), and records entries in `pipeline_messages` and `pipeline_items`.
- **Failure Protocol**: 3 exponential retries on connection or download interruption; on terminal failure, marks `(message_id, "ingestion", "failed")` and dispatches alerts without blocking subsequent channel messages.

### 3.2 Audio Extraction Subsystem (`pipeline.audio`)
- **Purpose**: Demux and convert raw video audio into standard acoustic formats required by speech recognition and forced alignment engines.
- **Tooling**: FFmpeg binary (`FFMPEG_BINARY`, default `ffmpeg`).
- **Technical Constraints**:
  - Target Format: 16-bit signed PCM WAV (`pcm_s16le`).
  - Sampling Frequency: Fixed 16,000 Hz (`-ar 16000`).
  - Channel Count: Mono (`-ac 1`).
  - Minimum Duration Gate: `MINIMUM_AUDIO_DURATION_SECONDS` (default: 3.0s). Short audio fragments or silent files trigger immediate stage failure.
- **Output Artifact**: `storage/audio/{message_id}.wav`.

### 3.3 Verse Recognition & Text Matching Subsystem (`pipeline.recognition`)
- **Purpose**: Accurately identify the Quranic Surah and Ayah range being recited, replacing imprecise speech-to-text outputs with canonical diacritized text.
- **Workflow**:
  1. **Acoustic Transcription**: Audio is processed by Whisper ASR (`large-v3` or equivalent).
  2. **Canonical Corpus**: Complete local cache of the Quran in Uthmani script (`storage/cache/quran_corpus.json`), sourced from Tanzil or Quran.com API.
  3. **Arabic Text Normalization**: Both raw ASR transcript and corpus text are normalized for matching:
     - Diacritics (Tashkeel) stripped: Fatha, Damma, Kasra, Sukun, Shadda, Tanwin.
     - Letter unification: Alif variants (`أ`, `إ`, `آ`, `ٱ`) $\to$ `ا`, Taa Marbuta (`ة`) $\to$ `ه`, Yaa/Alif Maqsura (`ى`) $\to$ `ي`.
  4. **Sliding-Window Matching (`QuranMatcher`)**:
     - Evaluates contiguous word n-grams across 114 Surahs and 6,236 Ayahs.
     - Computes normalized Levenshtein similarity / token overlap score ($0.0 \le \text{confidence} \le 1.0$).
     - Resolves multi-ayah recitation sequences (`ayah_start` through `ayah_end`).
- **Output Artifact**: `storage/match/{message_id}.json`:
  ```json
  {
    "surah": 1,
    "ayah_start": 1,
    "ayah_end": 7,
    "canonical_text": "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ ...",
    "match_confidence": 0.965,
    "transcribed_text": "بسم الله الرحمن الرحيم ..."
  }
  ```

### 3.4 Forced Alignment Subsystem (`pipeline.alignment`)
- **Purpose**: Establish millisecond-level start and end timestamps for every word in the canonical text.
- **Tooling**: `ctc-forced-aligner` backed by Wav2Vec2 / Meta MMS Arabic acoustic model (`facebook/mms-1b-all` / `mms-1b-arabic`).
- **Algorithm**:
  - Aligns audio features against the normalized phonetic sequence of the matched canonical Quranic text.
  - Generates ordered word intervals: `[{"word": "بِسْمِ", "start_ms": 120, "end_ms": 480}, ...]`.
  - Calculates alignment coverage: $\text{coverage} = \frac{\text{aligned words}}{\text{total canonical words}}$.
  - Detects recitation pauses ($> 400\text{ms}$) to establish natural breath chunking for video subtitle linebreaks.
- **Output Artifact**: `storage/align/{message_id}.json`.

### 3.5 Quality Assurance (QA) Gate Subsystem (`pipeline.qa_gate`)
- **Purpose**: Ensure zero erroneous Quranic content is ever published.
- **Threshold Gating Engine (`QAGateService`)**:
  - `QA_MATCH_CONFIDENCE_THRESHOLD`: Minimum acceptable ASR-to-corpus similarity (default: `0.90`).
  - `QA_ALIGNMENT_COVERAGE_THRESHOLD`: Minimum acceptable word alignment ratio (default: `0.95`).
- **Gate Evaluation**:
  $$\text{Approved} \iff (\text{match\_confidence} \ge \text{threshold}_{\text{match}}) \land (\text{alignment\_coverage} \ge \text{threshold}_{\text{align}})$$
- **Enforcement Rules**:
  - **Pass**: State transitions to `(message_id, "qa_gate", "approved")`; proceeds immediately to scheduling and rendering.
  - **Fail / Hold**:
    - State recorded as `(message_id, "qa_gate", "held_for_review")` and `(message_id, "orchestration", "held_for_review")`.
    - Review artifact written to `storage/qa_gate/review_{message_id}.json`.
    - Critical hold alert dispatched to operators via Telegram / Slack webhook.
    - Pipeline runner halts immediately with **exit code 2**. Rendering and publishing are **never** executed.
    - Idempotency claim blocks automatic retries without operator `--force`.

### 3.6 Video Rendering & Styling Subsystem (`pipeline.render`)
- **Purpose**: Transform raw recitation audio and alignment timestamps into 1080×1920 vertical video matching the reference "Quran edit" aesthetic.
- **Canvas Technical Specification**:
  - Dimensions: 1080 × 1920 px (9:16 portrait aspect ratio).
  - Video Codec: H.264 (`libx264`), Profile High, Level 4.2.
  - Pixel Format: `yuv420p` (maximum mobile hardware decoding compatibility).
  - Framerate: 30 fps constant.
  - Audio Codec: AAC-LC (`aac`), 192 kbps, stereo/mono preserving original recitation dynamics.
- **Visual Composition Elements**:
  1. **Background Canvas**:
     - Asset pool of curated architectural, calligraphic, and lantern loops under `assets/backgrounds/`.
     - Selection strategies: `round_robin` (sequential cycling), `random` (uniform selection), or `keyed` (deterministic modulo on `surah_number`).
     - Procedural Fallback: If pool is empty, renders a procedural 1080×1920 dark radial gradient with subtle vignette.
     - Visual Filtering: Scaling with crop/fill (`scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920`), slight darkening blur for subtitle contrast.
  2. **Calligraphic RTL Typography (`KaraokeSubtitleGenerator`)**:
     - Generates Advanced SubStation Alpha (`.ass`) karaoke subtitles.
     - Text Shaping: Native HarfBuzz (`uharfbuzz`) Arabic complex script text shaping, ensuring proper contextual ligatures and cursive joining in RTL.
     - Karaoke Animation: Synchronized word reveal utilizing `{\k<centiseconds>}` tags mapped directly from `align/{message_id}.json`.
     - Styling: Centered placement, primary white font with soft gold outline and drop-shadow, non-obscuring margin offsets.
  3. **Branding Watermark**:
     - High-resolution seal/logo image (`BRANDING_LOGO_PATH`) positioned in top-right or lower-third.
     - Text handle (`BRANDING_HANDLE`, e.g. `@quran_recitations`) in dedicated font.
- **Output Artifact**: `storage/render/{message_id}.mp4`.

### 3.7 Syndication & Publishing Subsystem (`pipeline.publish`)
- **Purpose**: Fan-out syndication of rendered vertical videos to connected social media platforms via Ayrshare API.
- **Target Destinations**: TikTok, Instagram Reels, YouTube Shorts, Facebook Pages, X (Twitter), Telegram Channels.
- **Features**:
  - `MediaUploader`: Uploads rendered MP4 to public object storage (S3 / MinIO) or presigned CDN URL.
  - `CaptionTemplater`: Generates platform-compliant captions with Surah name, Ayah reference, and hashtags (`PUBLISH_DEFAULT_HASHTAGS`).
  - Character Truncation Guard: Truncates captions cleanly at word boundaries per platform limit (X: 280 chars, Telegram: 1024 chars, Instagram: 2200 chars).
  - Draft / Sandbox Mode (`PUBLISH_DRAFT_MODE=true`): Simulates and records full payload generation without posting live.
  - Per-Platform Error Isolation: Partial failures (e.g. TikTok succeeds, Instagram rejects) record partial status, alert operators, and log individual platform post IDs.
- **Output Artifact**: `storage/publish/{message_id}.json`.

### 3.8 Scheduling & Rate Limiting Subsystem (`pipeline.orchestration.scheduler`)
- **Purpose**: Enforce allowed daily posting windows and prevent social network API bans caused by burst video posting.
- **Time-of-Day Window**:
  - Defined by `POSTING_WINDOW_START_HOUR` (e.g. 09) and `POSTING_WINDOW_END_HOUR` (e.g. 23).
  - Timezone-aware using `POSTING_WINDOW_TIMEZONE` (`zoneinfo.ZoneInfo`).
  - Supports overnight spans (e.g. 22:00 to 06:00).
- **Inter-Post Rate Limiting**:
  - Enforces minimum spacing between publications (`RATE_LIMIT_MIN_INTERVAL_SECONDS`, default: 1800s / 30m).
  - Queries `fetch_latest_published_timestamp()` from the state database.
- **Two-Phase Enforcement & Race Prevention**:
  1. *Phase 1 (Pre-Execution)*: Initial posting window check. If closed, defers item immediately with status `scheduled`.
  2. *Phase 2 (Pre-Publish Guard)*: Re-checks window immediately before `reserve_publish_slot()`. If a lengthy video render crossed the window boundary, publish is deferred, state updated to `scheduled`, and the lock released.

### 3.9 Observability, Monitoring & Health Subsystem (`pipeline.monitoring`)
- **Purpose**: System health tracking, failure alerting, and review queue monitoring without external third-party APM services.
- **Components**:
  - `PipelineMonitor`: Computes metrics snapshots (`MonitoringReport`), formats Markdown text for chat and JSON for dashboards.
  - `HealthCheckResult`: Evaluates state against `MONITORING_FAILURE_THRESHOLD` (default: 3 consecutive failed runs) and `MONITORING_BACKLOG_THRESHOLD` (default: 5 items held in review queue).
  - Alert Cooldown Engine: Tracks alert keys and suppresses duplicate notification spam within `MONITORING_ALERT_COOLDOWN_SECONDS` (default: 3600s).
  - `MonitoringBotHandler`: Interprets chat-bot commands (`/status`, `/health`, `/queue`, `/retry <id>`, `/help`).
  - Monitoring CLI (`pipeline.monitoring.app`): Exposes `report` and `check` subcommands with distinct exit codes (`0` = healthy, `2` = threshold breached, `1` = execution failure).

---

## 4. Data Architecture & Persistence Specifications

### 4.1 Dual-Tier Database Strategy (ADR 001)

| Environment | Engine | Driver / Pool | Primary Use Case | Concurrency Mechanism |
|---|---|---|---|---|
| **Development & CLI** | SQLite 3 (`STATE_DB_PATH`) | `sqlite3` standard library | Local CLI runs, offline development, fast unit tests | `BEGIN IMMEDIATE` file write lock |
| **Production & Cluster**| PostgreSQL 16 (`STATE_DATABASE_URL`) | `psycopg` (v3) + `psycopg_pool.ConnectionPool` | Multi-container n8n orchestration, concurrent workers | `pg_advisory_xact_lock` per message ID |

### 4.2 Database Precedence Rule
To ensure deterministic testing in containerized and CI environments where `STATE_DATABASE_URL` is globally exported, explicit SQLite database paths take strict precedence:
```python
state_db_path = os.getenv("STATE_DB_PATH")

if state_db_path:
    # Explicit SQLite database requested (CLI testing / isolated runner)
    # Ignore inherited STATE_DATABASE_URL for this invocation
    state_repo = PipelineStateRepository(database_path=Path(state_db_path).resolve())
elif mon_settings.state_database_url:
    # Use configured PostgreSQL connection pool
    state_repo = PipelineStateRepository(database_url=mon_settings.state_database_url)
else:
    # Fall back to default local SQLite path
    state_repo = PipelineStateRepository(database_path=mon_settings.state_db_path)
```

### 4.3 Relational Schema (`pipeline/state/schema.sql`)

```sql
-- Tracks discrete stage outcomes per message
CREATE TABLE IF NOT EXISTS pipeline_items (
    message_id BIGINT NOT NULL,
    stage VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error TEXT,
    PRIMARY KEY (message_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_items_status
    ON pipeline_items (status);

CREATE INDEX IF NOT EXISTS idx_pipeline_items_updated_at
    ON pipeline_items (updated_at);

-- Immutable source message metadata captured during ingestion
CREATE TABLE IF NOT EXISTS pipeline_messages (
    message_id BIGINT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_seconds REAL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 4.4 Stage & Status Enumerations

| Field | Allowed Values |
|---|---|
| `stage` | `ingestion`, `audio`, `recognition`, `alignment`, `qa_gate`, `render`, `publish`, `orchestration` |
| `status` | `pending`, `processing`, `completed`, `failed`, `held_for_review`, `scheduled`, `skipped_active_execution`, `skipped_already_published` |

### 4.5 File System & Storage Directory Hierarchy

```
storage/
├── raw/            # Ingested MP4 source recordings: {message_id}.mp4
├── audio/          # 16kHz mono WAV extracts: {message_id}.wav
├── match/          # Quranic verse match metadata: {message_id}.json
├── align/          # Word-level phoneme timestamps: {message_id}.json
├── qa_gate/        # QA review diagnostics: review_{message_id}.json
├── render/         # Rendered 1080x1920 MP4 outputs: {message_id}.mp4
├── publish/        # Social syndication receipts & post IDs: {message_id}.json
└── cache/          # Static canonical Quran corpus: quran_corpus.json
```

---

## 5. Concurrency, Race Prevention & Idempotency

### 5.1 Atomic Execution Claims (`claim_execution`)
To prevent race conditions when multiple worker containers or webhook events process the same video simultaneously:
- **SQLite Engine**: Encloses claim checks in `BEGIN IMMEDIATE` transactions, obtaining an exclusive file write lock.
- **PostgreSQL Engine**: Issues a transaction-scoped advisory lock before querying:
  ```sql
  SELECT pg_advisory_xact_lock(hashtext('claim_execution_' || CAST(%s AS text)));
  ```
- **Claim Logic**:
  1. If stage `orchestration` is `completed` and output exists $\to$ Return `(False, "already_completed")` $\to$ Worker skips cleanly (`exit code 0`).
  2. If stage `orchestration` is `processing` and was updated $< 300\text{s}$ ago $\to$ Return `(False, "active_execution_recent")` $\to$ Worker skips (`exit code 0`).
  3. If stage `qa_gate` is `held_for_review` $\to$ Return `(False, "held_for_review")` $\to$ Worker blocks without `--force` (`exit code 2`).
  4. Otherwise $\to$ Upsert `status="processing"` and grant claim $\to$ Return `(True, "claimed")`.

### 5.2 Active Lock Recovery & Crash Resilience
If an orchestrator worker crashes or is terminated by the host during processing:
- The `processing` lock automatically becomes stale after 300 seconds (5 minutes).
- Subsequent workers can reclaim the item without manual database intervention.
- Operators can force immediate reclamation via CLI using `--force --force-active`.

### 5.3 Publishing Rate-Limit Mutual Exclusion (`reserve_publish_slot`)
- Serializes slot reservations across distributed workers using PostgreSQL advisory lock `hashtext('reserve_publish_slot')` or SQLite `BEGIN IMMEDIATE`.
- Compares `CURRENT_TIMESTAMP - latest_published_at` against `RATE_LIMIT_MIN_INTERVAL_SECONDS`.
- If interval is not satisfied, returns `(False, wait_seconds, reason)` without publishing.

---

## 6. Interface & Integration Specifications

### 6.1 CLI Application Interfaces

#### Orchestrator CLI (`pipeline.orchestration.app`)
```bash
python -m pipeline.orchestration.app <message_id> [OPTIONS]
```
| Flag | Type | Description |
|---|---|---|
| `message_id` | `int` (Positional) | Unique Telegram video message identifier |
| `--source` | `Path` | Explicit local file path to source video (bypasses Telegram download) |
| `--draft` | `flag` | Execute publishing in sandbox/draft mode without posting live |
| `--force` | `flag` | Override completed or held_for_review status to force re-run |
| `--force-active`| `flag` | Override active processing lock (<300s since worker start) |
| `--json` | `flag` | Output execution summary as structured JSON to stdout |

**Exit Codes**:
- `0`: Success / Scheduled / Skipped (Idempotent success)
- `1`: Unhandled error / Stage failure
- `2`: Held by QA Gate review queue

#### Monitoring CLI (`pipeline.monitoring.app`)
```bash
# Operational Status Report
python -m pipeline.monitoring.app report [--window-hours N] [--json] [--send-alert]

# Health Threshold Evaluation
python -m pipeline.monitoring.app check [--failure-threshold N] [--backlog-threshold N] [--json] [--send-alert]
```
**Exit Codes (`check` command)**:
- `0`: System healthy (all metrics within thresholds)
- `2`: System unhealthy (consecutive failures or review backlog exceeded)
- `1`: Execution failure

### 6.2 Telegram Chat-Bot Admin Interface (`MonitoringBotHandler`)

| Command | Arguments | Expected Response |
|---|---|---|
| `/status` | `[hours]` | Markdown report displaying total messages, active jobs, stage breakdown, review queue count, and recent failures. |
| `/health` | None | Real-time health verdict: `✅ HEALTHY` or `🚨 ATTENTION REQUIRED` with breach explanations. |
| `/queue` | None | List of message IDs currently held in QA Gate awaiting operator inspection. |
| `/retry` | `<message_id>` | Formatted command for operator re-processing: `python -m pipeline.orchestration.app <id> --force`. |
| `/help` | None | List of available commands and usage instructions. |

### 6.3 Third-Party External API Contracts

#### Ayrshare REST API (`POST /api/post`)
- **Authentication**: Header `Authorization: Bearer {PUBLISH_API_KEY}`
- **Request Body**:
  ```json
  {
    "post": "Surah Al-Fatiha (1:1-7)\n\n#Quran #Islam",
    "platforms": ["tiktok", "instagram", "youtube", "facebook", "twitter"],
    "mediaUrls": ["https://cdn.example.com/render/105.mp4"],
    "isVideo": true
  }
  ```
- **Response Schema**:
  ```json
  {
    "status": "success",
    "id": "ayr_top_level_id",
    "postIds": [
      {"platform": "tiktok", "id": "tt_123", "status": "success"},
      {"platform": "instagram", "id": "ig_456", "status": "success"}
    ]
  }
  ```

---

## 7. Configuration Specification & Environment Matrix

All system settings are loaded via environment variables or `.env` files into strongly typed dataclasses (`pipeline.config`).

| Environment Variable | Type | Default | Description |
|---|---|---|---|
| `STATE_DB_PATH` | `Path` | `./pipeline/state/pipeline.db` | Path to local SQLite database (Overrides Postgres when set) |
| `STATE_DATABASE_URL` | `str` | `None` | PostgreSQL connection string (`postgresql://user:pass@host:5432/db`) |
| `PIPELINE_STORAGE_ROOT` | `Path` | `./storage` | Base filesystem directory for raw, audio, and render outputs |
| `TELEGRAM_API_ID` | `int` | `None` | Telegram MTProto client API ID |
| `TELEGRAM_API_HASH` | `str` | `None` | Telegram MTProto client API Hash |
| `TELEGRAM_BOT_TOKEN` | `str` | `None` | Telegram Bot API token for notifications and bot commands |
| `TELEGRAM_CHANNEL_ID` | `str` | `None` | Monitored source Telegram channel ID |
| `WHISPER_MODEL` | `str` | `large-v3` | Model checkpoint identifier for Arabic speech transcription |
| `QA_MATCH_CONFIDENCE_THRESHOLD` | `float` | `0.90` | Minimum normalized Levenshtein match score to pass QA Gate |
| `QA_ALIGNMENT_COVERAGE_THRESHOLD`| `float` | `0.95` | Minimum aligned word ratio to pass QA Gate |
| `BACKGROUND_SELECTION_STRATEGY` | `str` | `round_robin` | Selection strategy: `round_robin`, `random`, or `keyed` |
| `BRANDING_HANDLE` | `str` | `None` | Overlay text watermark handle (e.g. `@QuranEdits`) |
| `BRANDING_LOGO_PATH` | `Path` | `None` | Path to transparent PNG/JPG branding seal |
| `PUBLISH_PLATFORMS` | `str` | `tiktok,instagram,youtube,facebook,x,telegram` | Comma-delimited list of target platform keys |
| `PUBLISH_DRAFT_MODE` | `bool` | `false` | When `true`, simulates posting without live broadcast |
| `PUBLISH_API_KEY` | `str` | `None` | Ayrshare API secret key |
| `POSTING_WINDOW_ENABLED` | `bool` | `false` | Enforce allowed daily hours for public posting |
| `POSTING_WINDOW_START_HOUR` | `int` | `9` | Hour of day (0-23) posting window opens |
| `POSTING_WINDOW_END_HOUR` | `int` | `23` | Hour of day (0-23) posting window closes |
| `POSTING_WINDOW_TIMEZONE` | `str` | `UTC` | Timezone name for posting window evaluation |
| `RATE_LIMIT_MIN_INTERVAL_SECONDS`| `int`| `1800` | Minimum elapsed seconds between consecutive posts |
| `MONITORING_FAILURE_THRESHOLD` | `int` | `3` | Consecutive failed runs triggering critical alert |
| `MONITORING_BACKLOG_THRESHOLD` | `int` | `5` | QA review backlog items triggering alert |
| `MONITORING_WINDOW_HOURS` | `int` | `24` | Aggregation window in hours for status reports |
| `MONITORING_ALERT_COOLDOWN_SECONDS`| `int` | `3600` | Minimum seconds between identical health alerts |
| `ALERT_WEBHOOK_URL` | `str` | `None` | HTTP Webhook URL for operational incident alerts |
| `LOG_LEVEL` | `str` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 8. Non-Functional Requirements (NFRs)

### 8.1 Performance & Latency Budgets
- **Processing Latency**: An average 30–60 second recitation clip must complete the end-to-end pipeline (Audio $\to$ Alignment $\to$ Render) within $\le 90$ seconds on GPU hardware or $\le 180$ seconds on standard 4-core CPU instances.
- **Rendering Throughput**: Single-pass FFmpeg filtergraph execution must maintain $\ge 1.5\times$ real-time encoding speed for 1080×1920 H.264 video.

### 8.2 Reliability, Availability & Error Isolation
- **Fault Containment**: A failure in any single message (e.g. corrupt audio, unaligned words, network timeouts) must cleanly terminate execution for that `message_id`, record diagnostic errors in the state repository, dispatch an alert, and exit cleanly without causing container crashes or impacting subsequent jobs.
- **Fail-Closed Operations**: Under any condition of ambiguity (missing corpus, low audio SNR, speech recognition uncertainty), the pipeline fails to the QA Gate review queue rather than guessing text.

### 8.3 Concurrency Load Capacity
- System verified under burst conditions of 16 concurrent video arrivals (`pipeline/tests/test_load.py`):
  - 16 distinct messages process in parallel with 100% data integrity and zero SQLite/PostgreSQL locking deadlocks.
  - 16 duplicate threads for the same message ID permit exactly 1 worker to proceed while 15 workers cleanly skip (`skipped_active_execution`).
  - 16 simultaneous publication requests serialize cleanly through the rate-limit reservation lock.

### 8.4 Security & Data Privacy
- **Credentials Isolation**: Zero API tokens, database credentials, or Telegram session keys stored in source code. All secrets injected via environment variables.
- **Stateless Storage**: Processing nodes rely exclusively on mounted object storage and database connections, enabling instant container teardown and recreation.

---

## 9. Quality Assurance & Testing Architecture

### 9.1 Test Pyramid & Coverage

```
               ▲
              / \
             /   \      E2E Dry-Run & Burst Load Tests (16 threads)
            /     \     [pipeline/tests/test_load.py]
           /───────\
          /         \   Integration & Component Suites
         /           \  [test_orchestration.py, test_publish.py, test_render.py]
        /─────────────\
       /               \ Unit Tests (State, Matcher, Aligner, Subtitles, Mon)
      /─────────────────\ [150 Automated Tests - 100% Passing]
```

### 9.2 Verification Commands

```bash
# 1. Run targeted monitoring & precedence tests
python -m unittest pipeline.tests.test_monitoring -v

# 2. Run multi-threaded concurrency burst load suite
python -m unittest pipeline.tests.test_load -v

# 3. Full Standard Library Test Suite Discovery
python -m unittest discover -s pipeline/tests -v

# 4. Full Pytest Regression Suite
pytest -v

# 5. CLI Verification
python -m pipeline.monitoring.app report --json
python -m pipeline.monitoring.app check
```

### 9.3 CI/CD Pipeline (`.github/workflows/ci.yml`)
- Automated on every `push` and `pull_request` to `main`.
- Provisions a live PostgreSQL 16 service container.
- Installs FFmpeg, PostgreSQL client libraries, and Python dependencies.
- Initializes database schema using `pipeline/state/schema.sql`.
- Executes both standard library `unittest` discovery and `pytest` test suites.

---

## 10. Operations & Deployment Topology

### 10.1 Container Architecture (`docker-compose.yml`)

```yaml
services:
  postgres:     # PostgreSQL 16 with state schema initialization
  minio:        # S3-compatible local object storage
  minio-init:   # Automated bucket provisioner (raw, audio, render, etc.)
  n8n:          # Visual workflow orchestrator & webhook scheduler
  ingestion:    # Continuous Telethon Telegram channel listener
```

### 10.2 Incident Management Quick Reference
- **Repeated Failures Alert (`🚨 CRITICAL: Repeated Failures`)**:
  - Run `python -m pipeline.monitoring.app report` to identify failing stage.
  - Verify FFmpeg binaries, Ayrshare API quotas, or corpus cache integrity.
- **Review Backlog Surge Alert (`⚠️ Growing Review Backlog`)**:
  - Inspect held items via `/queue` or `storage/qa_gate/review_{message_id}.json`.
  - Re-process approved items with operator override: `python -m pipeline.orchestration.app <id> --force`.
  - If reciter acoustic variation is high, adjust `QA_MATCH_CONFIDENCE_THRESHOLD` in `.env`.

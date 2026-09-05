# SPEC — Automated Quran Video Repurposing & Publishing Pipeline

## 1. Purpose

Convert raw prayer/recitation recordings posted to a Telegram channel into short-form,
vertically-formatted "Quran edit" style videos — ayah text revealed word-by-word in sync
with the recitation, over a stylized background — and publish them automatically across
multiple social platforms with no human step in the normal path.

Reference transformation: plain broadcast-style prayer recording (imam at mic, mosque
interior, date/prayer-name lower-third) → stylized short-form edit (dim architectural
background, ornate lantern/plaque imagery, animated calligraphic ayah text, branding
handle).

## 2. Goals & non-goals

**Goals**
- End-to-end automation: new Telegram post → published video, zero manual steps in the
  happy path.
- Word/breath-level text sync matching the reference style.
- An automated accuracy gate before anything with Quran text goes out — this is a
  correctness requirement, not a nice-to-have.
- Modular stages, each independently testable and replaceable.

**Non-goals (v1)**
- Translated/multi-language subtitle tracks (candidate for v2).
- Reciter diarization / multi-speaker handling.
- True live-stream processing — this is near-real-time, per finished upload.
- End-user-facing dashboard or app. Admin visibility is via logs + chat alerts only.

## 3. High-level architecture

```
Telegram channel
      │  new video message
      ▼
Ingest & extract        (download video, pull audio)
      ▼
Recognize & align       (ASR → verse match → word-level timestamps)
      ▼
QA gate                 (confidence check vs canonical Quran text)
   pass │        │ fail
      ▼         ▼
   Render     Hold & alert (review queue, notify admin, no publish)
      ▼
   Publish     (one API call → every connected platform)
```

Orchestrated by n8n, with each stage above implemented as a callable service/step.

## 4. Components

### 4.1 Ingestion service
- **Trigger**: new video message in the monitored Telegram channel.
- **Tooling**: Telethon or Pyrogram session listener (works against any channel you can
  view, no admin rights needed), or Telegram Bot API if the bot can be added to the
  channel.
- **Output**: `raw/{message_id}.mp4` in object storage, plus a state row.
- **Metadata captured**: `message_id`, `timestamp`, `channel_id`, `duration`.
- **Failure handling**: retry download 3× with backoff; on repeated failure, log, alert,
  and skip — never blocks the next item.

### 4.2 Audio extraction
- **Tool**: ffmpeg.
- Extract mono 16kHz WAV for downstream ASR.
- **Output**: `audio/{message_id}.wav`.

### 4.3 Verse recognition & text matching
- **ASR**: Whisper large-v3 (or comparable) transcribes the Arabic audio to a rough
  transcript.
- **Reference corpus**: canonical diacritized Quran text (Tanzil Uthmani text, or the
  quran.com API), cached locally.
- **Matching**: fuzzy/edit-distance match of the rough transcript against the corpus to
  identify the exact surah + ayah range, and to substitute the ASR's rough output with
  the correct canonical text.
- **Output**: `match/{message_id}.json`:
  ```json
  { "surah": 2, "ayah_start": 255, "ayah_end": 255, "canonical_text": "...", "match_confidence": 0.94 }
  ```

### 4.4 Word-level forced alignment
- **Primary tool**: `ctc-forced-aligner` (wav2vec2/MMS Arabic model) — forced-aligns
  arbitrary audio against given text; robust choice for live, non-studio recitation.
- **Fallback**: `quran-align`, better suited to clean, verse-by-verse studio recordings —
  evaluate during implementation whether it's worth maintaining both paths.
- **Output**: `align/{message_id}.json`, an ordered list of
  `{ "word": "...", "start_ms": 0, "end_ms": 0 }`.

### 4.5 QA gate — mandatory, not optional
- **Inputs**: `match_confidence` (4.3) and an alignment coverage/confidence score (4.4).
- **Threshold** (config, tune against a regression set): e.g.
  `match_confidence >= 0.90 AND alignment_coverage >= 0.95`.
- **Pass** → proceed to render.
- **Fail** → write to `review_queue/{message_id}`, send an alert (Telegram/Slack) with
  the recognized text, the canonical text, and both scores. The item is **not**
  auto-published.
- This gate exists specifically so full automation doesn't come at the cost of
  publishing incorrect Quran text — it should never be bypassed for throughput.

### 4.6 Render / style transformation
- **Background assets**: a small, pre-approved loop library (architectural interiors,
  lantern/calligraphy plates) stored under `assets/backgrounds/`. Selection is
  round-robin, random, or keyed by surah/time-of-day (config).
- **Text overlay**: word-by-word reveal animation driven by `align/{message_id}.json`.
  Two implementation options — pick one for v1:
  - **ffmpeg + generated `.ass` karaoke subtitles**: fast, fully scriptable, minimal
    infra. Recommended default for v1.
  - **Remotion** (programmatic React-based render): more typographic control over the
    reveal animation, higher render cost/time. Candidate for v2 polish.
- **Branding overlay**: fixed handle/logo watermark, position configurable.
- **Output spec**: 1080×1920 (9:16), H.264 MP4, clipped/split if source exceeds platform
  duration limits, original recitation audio untouched.
- **Output**: `render/{message_id}.mp4`.

### 4.7 Publishing
- **Tool**: a single multi-platform posting API (one call fans out to every connected
  platform) rather than integrating each platform's upload flow separately.
- **Platforms** (config list): TikTok, Instagram Reels, YouTube Shorts, Facebook,
  Telegram (optional repost), X.
- **Metadata**: caption template (surah name + ayah range, branding/hashtags), optional
  custom thumbnail frame.
- **Scheduling**: immediate by default; optional posting-window config (e.g. avoid
  overnight hours).
- **Output**: `publish/{message_id}.json` — per-platform post IDs/status.

### 4.8 Orchestration
- **Tool**: n8n (self-hosted, Docker).
- **Workflow**: Telegram trigger → ingestion → audio → recognition → alignment →
  QA gate → render → publish.
- **Error workflow**: any node failure triggers an alert and marks the item failed in
  the state store without blocking subsequent items.

### 4.9 State & storage
- Lightweight state store (SQLite for MVP, Postgres if concurrency grows) tracking each
  `message_id` through every stage, with timestamps and status.
- Object storage for raw video, audio, and render outputs (S3-compatible; MinIO works
  for a fully self-hosted setup).

## 5. Config & secrets

| Item | Notes |
|---|---|
| Telegram credentials | `api_id`/`api_hash` (Telethon) or bot token |
| Publishing API key | platform connections configured on the provider side |
| ASR model config | local GPU vs. hosted inference endpoint |
| QA gate thresholds | tunable per regression-set results |
| Background asset pool | path/bucket + selection strategy |
| Branding assets | logo, handle text, font file |

## 6. Non-functional requirements

- **Idempotency**: reprocessing the same `message_id` must never double-publish.
- **Observability**: structured logs per stage; chat-based alerts for failures and QA
  holds; a pipeline dashboard is a nice-to-have, not required for v1.
- **Compute**: ASR + forced alignment benefit from GPU. Given likely volume (a handful
  of videos per day, not a firehose), on-demand/serverless GPU is probably more
  cost-effective than an always-on box — decide during implementation based on actual
  channel posting frequency.
- **Latency target**: new video → published within 15–30 minutes is acceptable for
  non-live content (config).

## 7. Suggested directory structure

```
/pipeline
  /ingestion
  /audio
  /recognition
  /alignment
  /qa_gate
  /render
  /publish
  /orchestration      (n8n workflow export)
  /assets
    /backgrounds
    /fonts
    /branding
  /state
  /config
  /tests
```

## 8. Testing strategy

- Unit tests per module, with mocked ASR/alignment outputs.
- One end-to-end integration test: known-good sample video through the full pipeline,
  asserting the QA gate passes and the render output matches expected duration/format.
- A small regression set of past videos with manually-verified correct ayah ranges, used
  to tune the QA-gate thresholds before go-live.

## 9. Open questions to resolve during implementation

- Single destination account set, or per-page/per-brand config if this expands to
  multiple pages?
- Final caption/hashtag template copy and branding text.
- Confirm licensing/rights for the background asset loop library itself.
- Which specific background/typography variant to standardize on for v1 (vs. A/B
  testing style variants later).

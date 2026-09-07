# TASKS — Implementation Plan

Reference: `SPEC.md`. Each phase lists section references so implementation details are
looked up rather than re-derived.

## Phase 0 — Project scaffolding
- [x] Initialize repo with directory structure (SPEC §7)
- [x] Create `.env.example` covering all secrets (SPEC §5)
- [x] Create state-store schema: `pipeline_items(message_id, stage, status, created_at, updated_at, error)`
- [x] Set up structured (JSON) logging shared across all modules
- [x] `docker-compose.yml` for local dev: n8n + state DB + object storage (MinIO)

## Phase 1 — Ingestion (SPEC §4.1)
- [x] Implement Telegram listener (Telethon/Pyrogram session or Bot API webhook) watching the target channel
- [x] On new video message: download to `raw/{message_id}.mp4`, insert state row
- [x] Retry/backoff (3 attempts) on download failure; alert + skip on exhaustion
- [x] Unit test: mock incoming Telegram message, assert file saved and state updated

## Phase 2 — Audio extraction (SPEC §4.2)
- [x] ffmpeg wrapper: extract mono 16kHz WAV from raw video
- [x] Handle edge cases: missing/short audio track, corrupted file
- [x] Unit test with a sample video fixture

## Phase 3 — Verse recognition & matching (SPEC §4.3)
- [x] Integrate ASR (Whisper large-v3 or chosen equivalent) for Arabic transcription
- [x] Pull and cache canonical Quran text corpus (Tanzil/quran.com API)
- [x] Implement fuzzy match: transcript → canonical ayah range + confidence score
- [x] Emit `match/{message_id}.json` per the SPEC §4.3 schema
- [ ] Unit tests against known audio/expected-ayah fixtures; track match accuracy on the regression set (requires the manually verified audio regression set and ASR sandbox credentials)

## Phase 4 — Word-level alignment (SPEC §4.4)
- [x] Integrate `ctc-forced-aligner` (Arabic wav2vec2/MMS model) as the primary aligner
- [x] Evaluate `quran-align` as a fallback for clean, studio-style audio; decide whether to maintain both paths (not maintained in v1)
- [x] Emit `align/{message_id}.json` per the SPEC §4.4 schema
- [x] Compute an alignment coverage/confidence score
- [ ] Confirm ctc-forced-aligner output file location against a real run
- [ ] Unit tests: timestamps monotonic and within audio duration, against the regression set (requires real alignment runtime and verified audio fixtures)

## Phase 5 — QA gate (SPEC §4.5)
- [ ] Implement the combined threshold check (`match_confidence` + `alignment coverage`), thresholds config-driven
- [ ] Pass path: mark state `approved`, trigger render
- [ ] Fail path: write to `review_queue/{message_id}`, send an alert with recognized text, canonical text, and both scores
- [ ] Unit tests forcing pass and fail scenarios; verify correct branching and that fail never reaches publish

## Phase 6 — Render (SPEC §4.6)
- [ ] Build background asset pool loader (round-robin/random/keyed selection, config-driven)
- [ ] Implement the word-by-word text overlay renderer:
  - [ ] v1: generate `.ass` karaoke subtitles from the alignment JSON, burn in via ffmpeg
  - [ ] v2 candidate: Remotion composition consuming the same alignment JSON, if higher typographic fidelity is wanted later
- [ ] Apply the branding watermark overlay
- [ ] Output 1080×1920 H.264 MP4 to `render/{message_id}.mp4`; verify duration/format against target platform limits
- [ ] Integration test: render a sample end-to-end and visually spot-check output

## Phase 7 — Publishing (SPEC §4.7)
- [ ] Integrate the multi-platform posting API client
- [ ] Build caption/hashtag templating (surah name, ayah range, branding)
- [ ] Configure target platform list (TikTok, IG Reels, YouTube Shorts, Facebook, X, Telegram repost)
- [ ] Store per-platform response/status in `publish/{message_id}.json`
- [ ] Implement idempotency check — skip if `message_id` already published
- [ ] Validate in the provider's sandbox/draft mode before enabling live posting

## Phase 8 — Orchestration (SPEC §4.8)
- [ ] Build the n8n workflow: Telegram trigger → ingestion → audio → recognition → alignment → QA gate → render → publish
- [ ] Configure the n8n error workflow to alert on failure at any node
- [ ] Add posting-window/rate-limit config if needed to avoid platform spam flags
- [ ] Dry run end-to-end on one real channel video, with publish left in draft mode

## Phase 9 — Monitoring & hardening
- [ ] Add a lightweight status report (chat-bot command or periodic summary)
- [ ] Alert on repeated failures or a growing QA-gate review-queue backlog
- [ ] Load-test with a burst of several videos in quick succession (concurrency handling)
- [ ] Write a short runbook: reprocessing a failed item, adjusting QA thresholds, adding/removing target platforms

## Phase 10 — Go-live
- [ ] Switch publishing from sandbox/draft to live
- [ ] Monitor the first several auto-published videos closely
- [ ] Tune QA thresholds and render style based on real output

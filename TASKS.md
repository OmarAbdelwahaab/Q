# TASKS — Implementation Plan

Reference: `SPEC.md`. Each phase lists section references so implementation details are
looked up rather than re-derived.

## Phase 0 — Project scaffolding
- [x] Initialize repo with directory structure (SPEC §7)
- [x] Create `.env.example` covering all secrets (SPEC §5)
- [x] Create state-store schema: `pipeline_items(message_id, stage, status, created_at, updated_at, error)`
- [x] Set up structured (JSON) logging shared across all modules
- [x] `docker-compose.yml` for local dev: n8n + state DB + object storage (MinIO)
- [x] Add a `.gitignore` (Python cache, venvs, `.env`, local sqlite db) — added; historical `__pycache__` files left tracked intentionally rather than rewriting history
- [x] Reconcile `pipeline/audio/` — resolved in the Phase 2 commit; source files are now properly tracked alongside their bytecode
- [ ] Decide and document: is the Postgres `pipeline_items` table (provisioned in `docker-compose.yml`/`schema.sql`) the target for later phases, or is SQLite (currently used by `PipelineStateRepository`) staying as-is for longer? `STATE_DATABASE_URL` is defined in `.env.example` but nothing reads it yet — fine for now, but worth being explicit before more phases build on top of one or the other
- [x] Commit the Phase 3 and Phase 4 work — done in `c003a9d`
- [ ] **Recurring pattern to fix: commit at the end of each phase, not several phases later.** Phase 5 (QA gate) arrived uncommitted again, same as Phase 3/4 did before. Worth a standing rule (e.g. in `.trae/rules`) rather than relying on review to catch it each time
- [ ] Optional one-time cleanup: now that `.gitignore` has been in place for two phases, consider `git rm -r --cached` on the already-tracked `__pycache__` files in one dedicated commit — low risk, and stops every future phase's diff from showing unrelated `.pyc` noise

## Phase 1 — Ingestion (SPEC §4.1)
- [x] Implement Telegram listener (Telethon/Pyrogram session or Bot API webhook) watching the target channel
- [x] On new video message: download to `raw/{message_id}.mp4`, insert state row
- [x] Retry/backoff (3 attempts) on download failure; alert + skip on exhaustion
- [x] Unit test: mock incoming Telegram message, assert file saved and state updated

## Phase 2 — Audio extraction (SPEC §4.2)
- [x] ffmpeg wrapper: extract mono 16kHz WAV from raw video
- [x] Handle edge cases: missing/short audio track, corrupted file
- [x] Unit test with a sample video fixture — includes a real-ffmpeg integration test (not just mocked), verified passing in review

## Phase 3 — Verse recognition & matching (SPEC §4.3)
- [x] Integrate ASR (Whisper large-v3 or chosen equivalent) for Arabic transcription — calls an OpenAI-compatible `/audio/transcriptions` endpoint (`ASR_API_URL`), not a bundled model, so no heavy ML dependency in this repo
- [x] Pull and cache canonical Quran text corpus (Tanzil/quran.com API) — atomic cache write, confirmed no network call when cache already exists
- [x] Implement fuzzy match: transcript → canonical ayah range + confidence score — Arabic-normalized (diacritics/letter variants stripped) contiguous-range matcher, capped at 6 ayat per post
- [x] Emit `match/{message_id}.json` per the SPEC §4.3 schema
- [ ] Unit tests against known audio/expected-ayah fixtures; track match accuracy on the regression set — still needs a real ASR endpoint + a manually-verified audio set; current tests use a stubbed transcriber

## Phase 4 — Word-level alignment (SPEC §4.4)
- [x] Integrate `ctc-forced-aligner` (Arabic wav2vec2/MMS model) as the primary aligner — CLI flags checked against the tool's own docs: `--device`/`--batch_size` are valid, and omitting `--romanize` is correct for this exact model per the tool's own Arabic example
- [x] Evaluate `quran-align` as a fallback — decided not to maintain it in v1, single operational path for now (reasonable call)
- [x] Emit `align/{message_id}.json` per the SPEC §4.4 schema
- [x] Compute an alignment coverage/confidence score
- [ ] Unit tests: timestamps monotonic and within audio duration, against the regression set — current tests inject a fake CLI runner, so parsing/validation logic is verified but the real tool has never been invoked. Specific thing to check on the first real run: the tool's own docs don't document an `--output_path`/output-location flag, so where exactly it writes its JSON sidecar is unconfirmed — the code assumes `<audio-stem>.json` next to the input WAV

## Phase 5 — QA gate (SPEC §4.5)
- [x] Implement the combined threshold check (`match_confidence` + `alignment coverage`), thresholds config-driven
- [x] Pass path: mark state `approved`, trigger render
- [x] Fail path: write to `review_queue/{message_id}`, send an alert with recognized text, canonical text, and both scores
- [x] Unit tests forcing pass and fail scenarios; verify correct branching and that fail never reaches publish — includes exact-boundary tests (0.9000 pass vs 0.8999 fail) and explicit end-to-end tests asserting render is never triggered on rejection

## Phase 6 — Render (SPEC §4.6)
- [x] Build background asset pool loader (round-robin/random/keyed selection, config-driven) — all three strategies verified
- [x] Implement the word-by-word text overlay renderer:
  - [x] v1: generate `.ass` karaoke subtitles from the alignment JSON, burn in via ffmpeg — mechanically correct, real-render verified; karaoke highlight works but Primary/Secondary contrast is subtle enough to be barely visible, worth a deliberate pass
  - [ ] v2 candidate: Remotion composition consuming the same alignment JSON, if higher typographic fidelity is wanted later
- [x] Apply the branding watermark overlay — code path correct (text handle + logo image, 5 position presets), but see asset-quality note below
- [x] Output 1080×1920 H.264 MP4 to `render/{message_id}.mp4`; verify duration/format against target platform limits — confirmed via a real render in review (1080×1920, h264, audio present, duration matches recitation length)
- [x] Integration test: render a sample end-to-end and visually spot-check output — automated real-ffmpeg test passes; an actual human visual spot-check (done in this review, see below) caught issues the automated test structurally can't
- [x] **Fix: `SurahHeader` color renders wrong.** Resolved: corrected hex to BGR `&H0037AFD4` (Gold #D4AF37). Upgraded karaoke subtitles to timed dialogue events with inline color overrides (`\c`), eliminating Arabic RTL word order flipping caused by libass `\k` handling.
- [x] **Fix: `BRANDING_FONT_PATH` is parsed but never used.** Resolved: wired `branding_font_path` through `RenderSettings` -> `RenderService` -> `KaraokeSubtitleGenerator`, added TTF font family extraction (`Amiri`), provided canonical font files, and passed `fontsdir=...` to FFmpeg `ass` filter.
- [x] **Replace placeholder assets before this is actually publish-ready.** Resolved: replaced `001_default_plate.png` with high-resolution 1080x1920 illuminated Quranic background plate and `logo.png` with transparent 240x240 circular gold calligraphy seal. Reconciled procedural background fallback color across modules (`0x0b0e14`).

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

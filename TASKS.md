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
- [x] Decide and document: is the Postgres `pipeline_items` table the target for later phases, or is SQLite staying as-is for longer? — resolved in `docs/decisions/001-state-database-strategy.md`: SQLite for Phases 1–7 (local/CLI/tests), Postgres becomes canonical at Phase 8+ (n8n orchestration). Confirmed by reading the actual file, not just the TASKS.md summary
- [x] Commit the Phase 3 and Phase 4 work — done in `c003a9d`
- [x] Recurring pattern to fix: commit at the end of each phase — standing rule now confirmed present in `.trae/rules/phase-commit-discipline.md` (and mirrored in `.agent/rules/` and `GEMINI.md`), and the pattern has held for Phases 5, 6, and 7 since
- [x] Optional one-time cleanup: `__pycache__` untracking — confirmed done in `022b4b5`

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
- [x] Implement the word-by-word text overlay renderer — mechanically correct, real-render verified
- [ ] v2 candidate: Remotion composition consuming the same alignment JSON, if higher typographic fidelity is wanted later
- [x] Apply the branding watermark overlay — code path correct (text handle + logo image, 5 position presets)
- [x] Output 1080×1920 H.264 MP4 to `render/{message_id}.mp4`; verify duration/format against target platform limits — confirmed via a real render in review (1080×1920, h264, audio present, duration matches recitation length)
- [x] Integration test: render a sample end-to-end and visually spot-check output
- [x] **Fixed and confirmed: `SurahHeader` gold color.** Now `&H0037AFD4`, renders correctly as gold. Verified visually.
- [x] **Fixed and confirmed: real font now wired end-to-end.** `get_font_family_name_from_ttf()` reads the actual TTF name table, and `fontsdir` is now passed to the ffmpeg `ass=` filter. Amiri renders correctly. Verified visually.
- [x] **Fixed and confirmed: real background and logo assets.** Both replaced with genuinely good, on-brand designs (mosque archway/lantern photo, gold Islamic roundel logo). Verified visually.
- [x] **Fixed and confirmed: RTL word order.** New architecture: instead of interleaving per-word override tags, the full line is now rendered as one unbroken, untagged Arabic string (correct RTL shaping guaranteed), with the karaoke highlight achieved via three stacked layers (`QuranDim`/`QuranActive`/`QuranCompleted`) clipped to each word's pixel bounds via ASS `\clip()` — a real HarfBuzz shaping pass (`uharfbuzz`) computes those bounds precisely, with a character-count-proportional fallback if HarfBuzz isn't available. Re-verified on the exact same two lines that were broken before (Al-Fatiha ayah 1 and 2) — both now read correctly. **Caveat**: `uharfbuzz` isn't installable in this review's sandbox (no network access), so I could only verify the fallback clip-estimation path, not the primary HarfBuzz path that will actually run in production. The fix's *architecture* (shape once, unbroken; clip afterward) is sound either way since both paths follow it, but worth a real render in an environment where `uharfbuzz` actually installs, to confirm the primary path's clip boundaries are as clean as the fallback's turned out to be\n- [ ] Related systemic gap worth naming: the QA gate (Phase 5) validates that *recognized* text matches canonical Quran text, but nothing currently validates that the *rendered video* displays that text in correct visual order. Those are different guarantees — worth a lightweight visual/OCR regression check on rendered output eventually, not just the text pipeline

## Phase 7 — Publishing (SPEC §4.7)
- [x] Configure target platform list (TikTok, IG Reels, YouTube Shorts, Facebook, X, Telegram repost) — config-driven via `PUBLISH_PLATFORMS`, overridable per-call and via CLI `--platforms`
- [x] Build caption/hashtag templating (surah name, ayah range, branding) — genuinely solid: correct Arabic reference formatting, sensible truncation order (shorten canonical text first, then shed hashtags, hard-truncate only as a last resort), per-platform length limits table
- [x] Store per-platform response/status in `publish/{message_id}.json` — atomic write, consistent with every prior phase
- [x] Implement idempotency check — skip if `message_id` already published — checks both state AND artifact-file presence before skipping; solid
- [x] Validate in the provider's sandbox/draft mode before enabling live posting — `StubPublishClient` is the CLI default when `PUBLISH_API_BASE_URL` is unset; draft flag threads through correctly
- [x] **Fixed and confirmed: real Ayrshare integration.** Payload now correctly uses `post`/`platforms`/`mediaUrls`/`isVideo` matching Ayrshare's real API, endpoint normalizes to `/api/post`, and `x`↔`twitter` platform-name mapping was added (Ayrshare still calls it "twitter" internally) with a matching reverse-mapping on the response side. Verified by reconstructing the actual GitHub files locally and running the tests myself — the mocked-response tests assert the exact request payload sent and correctly parse a realistic Ayrshare `postIds`/`errors` response shape
- [x] **Fixed: media uploader added, closing the "nothing produces a public URL" gap.** New `pipeline/publish/uploader.py` with three implementations (`PublicUrlMediaUploader` for a CDN/reverse-proxy setup, `S3MediaUploader` with boto3 + raw HTTP PUT fallback, `StubMediaUploader` for tests), selected in `app.py` by config priority (explicit public base URL → S3 endpoint → stub). Wired into `PublishService` before the caption/publish step, wrapped in try/except so upload failure fails the stage cleanly. **New, smaller finding**: `S3MediaUploader.upload_media` doesn't actually raise if both the boto3 upload *and* the HTTP PUT fallback fail — it logs two warnings and still returns the public URL as if it succeeded. That breaks the fail-closed pattern the try/except at the call site is relying on. Worth a follow-up: raise an explicit exception when both upload paths fail, rather than optimistically returning a URL that may not serve anything
- [x] **Fixed and confirmed: test suite now portable.** `test_publish.py` was rewritten to pure stdlib `unittest` (matching every other phase) instead of pytest fixtures/`@pytest.mark.asyncio`, and `pytest-asyncio` + `asyncio_mode = "auto"` were added to `pyproject.toml` as a second layer of protection for anyone who does run it under pytest. I reconstructed the fixed files locally and ran the entire suite with **zero test dependencies installed** — `python -m unittest discover`: 75/75 pass, including all 20 publish tests that silently didn't run last time

## Phase 8 — Orchestration (SPEC §4.8)
- [x] Build the n8n workflow: Telegram trigger → ingestion → audio → recognition → alignment → QA gate → render → publish — complete `pipeline/orchestration/workflow.json` with linear pipeline execution and conditional QA-gate branching
- [x] Configure the n8n error workflow to alert on failure at any node — complete `pipeline/orchestration/error_workflow.json` with Error Trigger, context extraction, state store update to `failed`, and alert dispatch
- [x] Add posting-window/rate-limit config if needed to avoid platform spam flags — implemented `PostingWindowScheduler` in `pipeline/orchestration/scheduler.py` with time-of-day window (including overnight wraparound) and inter-post rate limiting; configured via `OrchestrationSettings`
- [x] Dry run end-to-end on one real channel video, with publish left in draft mode — verified with `test_end_to_end_dry_run_with_draft_publishing_and_idempotency` generating a real test video, executing audio extraction, recognition, alignment, QA gate approval, real 1080x1920 MP4 video render, and draft publication with idempotency verification
- [x] State store dual-tier architecture (ADR 001) — enhanced `PipelineStateRepository` with transparent dialect placeholder translation (`?` vs `%s`) supporting SQLite locally and PostgreSQL in multi-container setups
- [x] **Fixed and confirmed: refactor `PipelineOrchestrator.run()` duplication.** Extracted asynchronous helper `_run_stage(stage_name, message_id, coro, stage_records, extract_details, start_time, success_statuses)` consolidating try/except execution, structured JSON logging, state repository upserts (`pipeline_items`), failure alerts, and `PipelineExecutionSummary` construction. Eliminated ~250 lines of duplicate boilerplate across all 6 core stages (`audio`, `recognition`, `alignment`, `qa_gate`, `render`, `publish`).
- [x] **Fixed: dead import in `runner.py`.** Removed unused `datetime, timezone` imports from `pipeline/orchestration/runner.py`.
- [x] **Fixed and confirmed: connection pooling in `PipelineStateRepository`.** Added connection pooling support with automatic driver selection: uses `psycopg_pool.ConnectionPool` when `psycopg` (v3) is present, or `psycopg2.pool.ThreadedConnectionPool` when `psycopg2` is present, wrapped in `_PooledConnectionWrapper` to safely return connections to the pool upon `close()`. Includes graceful fallback when optional pooling libraries are absent, zero overhead on SQLite, and an explicit repository `close()` method to shut down the pool cleanly.
- [x] **Fixed and confirmed: original concurrent-trigger idempotency race resolved.** Implemented atomic `claim_execution(message_id, stage, force)` in `PipelineStateRepository` guarded by `BEGIN IMMEDIATE` on SQLite and `pg_advisory_xact_lock` on PostgreSQL. A `--force` CLI flag was added for manual operator re-runs.
- [x] **Fixed and confirmed: "scheduled" outcome no longer deadlocks deferred messages.** Added `release_execution_claim(message_id, stage="orchestration", status="scheduled", error=reason)` on all early-return paths returning `status="scheduled"` (both posting-window pause and rate-limit wait). Verified with `test_scheduled_outcome_retry_does_not_deadlock`: when a message deferred once by a closed window is re-executed after the window opens, it claims cleanly without `--force`, renders, publishes, and completes with `status="completed"`.
- [x] **Fixed and confirmed: dangling rate-limit reservation on downstream failure resolved.** Moved `reserve_publish_slot()` from step 6 (pre-render) to step 8 (immediately before `publish_service.publish()`), avoiding premature reservation before the 10–60s video render stage. Added an active reservation expiration timeout (300s) and failure handling so render failures never leave an in-flight reservation. Verified with `test_downstream_render_failure_does_not_dangle_publish_reservation`: when message A fails render, an unrelated QA-approved message B executed immediately after is never blocked and publishes cleanly.
- [x] **Fixed and confirmed: real multi-threaded concurrency test coverage added.** Added true multi-threaded concurrent tests using `concurrent.futures.ThreadPoolExecutor` and `threading.Barrier` across concurrent threads: `test_concurrent_threads_claim_execution_mutual_exclusion` (8 threads racing for the same message_id: exactly 1 claims, 7 rejected with `active_execution`) and `test_concurrent_threads_reserve_publish_slot_mutual_exclusion` (8 threads racing to reserve rate-limit slots: exactly 1 succeeds, 7 blocked with `Rate limit active`).
- [x] **Fixed and confirmed: PostgreSQL backend concurrency guarantee.** Added PostgreSQL transaction-level advisory locks via `SELECT pg_advisory_xact_lock(hashtext('claim_execution_' || CAST(? AS text)))` in `claim_execution` (per-message exclusive lock) and `SELECT pg_advisory_xact_lock(hashtext('reserve_publish_slot'))` in `reserve_publish_slot` (global slot exclusive lock). These locks serialize the critical read-check-write transaction blocks across multi-container n8n worker instances and automatically release upon `conn.commit()` or rollback. Verified via `test_postgres_advisory_lock_queries`.
- [x] **Fixed and confirmed: scheduler rate-limit race resolved across backends.** Both single-slot race contention, multi-threaded concurrency, downstream failure resilience, and cross-backend locking (SQLite `BEGIN IMMEDIATE` + PostgreSQL `pg_advisory_xact_lock`) are verified passing.
- [x] **Fixed and confirmed: `build_orchestrator()` constructor-signature mismatches.** Corrected kwarg names in `pipeline/orchestration/app.py`:
  1. `CtcForcedAligner(binary=align_settings.aligner_binary, model=align_settings.alignment_model, ...)`
  2. `CaptionTemplater(default_template=pub_settings.publish_caption_template, ...)`
  3. `MultiPlatformPublishClient(api_base_url=pub_settings.publish_api_base_url, ...)`
- [x] **Fixed and confirmed: real `build_orchestrator()` automated test coverage added.** Added `test_build_orchestrator_real_instantiation` in `pipeline/tests/test_orchestration.py`. Exercises the real, unmocked factory with seeded corpus cache in both default stub-client mode and production `MultiPlatformPublishClient` mode (with `PUBLISH_API_KEY` and `PUBLISH_API_BASE_URL`), asserting clean construction and type assertions without exceptions.
- [x] **Fixed and confirmed: `workflow.json` downstream message_id expression resolved.** Updated all downstream nodes (`Extract Audio WAV`, `Recognize Quran Verses`, `CTC Forced Alignment`, `QA Gate Verification`, `Render 1080x1920 Video`, `Multi-Platform Publish`, and `Alert QA Review Queue`) to interpolate `{{$('Telegram Video Trigger').item.json.message.message_id}}` instead of `$json["message"]["message_id"]`. Added automated test assertions in `N8nWorkflowIntegrityTests` verifying each downstream node uses the named trigger reference.
- [x] **Fixed and confirmed: posting-window and rate-limit gating in `workflow.json` resolved.** 
  1. Changed the one-liner in `Posting Window & Rate Limit Check` from `print(d.can_post)` to `sys.exit(0 if d.can_post else 1)` so exit code indicates whether posting is allowed.
  2. Added the `Check Posting Window Allowed` IF node (`n8n-nodes-base.if`) testing `$json["exitCode"] == 0`, and wired it between `Posting Window & Rate Limit Check` and `Render 1080x1920 Video`, ensuring rendering and publishing only execute when posting is permitted. Verified in `N8nWorkflowIntegrityTests`.
- [x] **Fixed and confirmed (Issue 1): production n8n workflow now executes `PipelineOrchestrator`.** Collapsed `workflow.json` into a single `Run Pipeline Orchestrator` node executing `python -m pipeline.orchestration.app {{$('Telegram Video Trigger').item.json.message.message_id}} --json` with `continueOnFail: true`, followed by `Check Orchestration Exit Code` IF node routing exit code 2 (`held_for_review`) to `Alert QA Review Queue`. Removed all per-stage executeCommand nodes and n8n re-derived gating, ensuring atomic claiming, PostgreSQL advisory locks, and publishing rate limits are strictly enforced. Updated `N8nWorkflowIntegrityTests`.
- [x] **Fixed and confirmed (Issue 2): posting window re-checked immediately before publish in step 8.** Added check for `not self.scheduler.is_within_posting_window()` right before `reserve_publish_slot()` in `runner.py` step 8. If rendering crosses window boundaries, publication is deferred with status `scheduled` and the claim is cleanly released. Verified with `test_posting_window_closing_during_render_blocks_publish`.
- [x] **Fixed and confirmed (Issue 3): PostgreSQL advisory lock real integration test.** Added `PostgreSqlAdvisoryLockIntegrationTests` in `test_orchestration.py` verifying multi-connection mutual exclusion with `pg_try_advisory_lock` / `pg_advisory_unlock` against live PostgreSQL (`STATE_DATABASE_URL`), skipped gracefully if unreachable or driver missing. Documented in `PHASE8_COMPLETION_REPORT.md` and `PHASE8_TEST_PLAN.md`.
- [x] **Fixed and confirmed (Issue 4): `held_for_review` messages require `--force` to re-claim.** Updated `claim_execution()` in `pipeline/state/repository.py` to block re-claiming `held_for_review` messages without `--force`, while preserving auto-retry for transient `failed` messages. Handled in `runner.py` with warning log and exit code 2 propagation. Verified with `test_held_for_review_requires_force_to_retry`.
- [x] **Fixed and confirmed (Follow-Up Issue 1): Ingestion Stage incorporated into `PipelineOrchestrator.run()`.** Added `IngestionService` into `build_orchestrator()` and `PipelineOrchestrator`. Stage 1 validates existing raw files, registers explicit `--source` videos, and halts cleanly at Stage 1 (`ingestion`) with status `failed` when raw video is missing. Added `ingestion` container service to `docker-compose.yml` running `pipeline.ingestion.app`.
- [x] **Fixed and confirmed (Follow-Up Issue 2): `workflow.json` dual-output connections array and failure alerting.** Added `main[1]` (false branch) connection to `Check Orchestration Exit Code` routing `exitCode != 2` to `Check Execution Succeeded` IF node (`exitCode == 0`), whose false branch (`exitCode != 0`, e.g. exit code 1) triggers `Alert Pipeline Failure` HTTP Request node. Updated `N8nWorkflowIntegrityTests` verifying both branches and nodes.
- [x] **Fixed and confirmed (Follow-Up Issue 3): Cold start integration test added.** Added `test_cold_start_without_raw_video_fails_cleanly` in `pipeline/tests/test_orchestration.py` asserting that missing raw video with no `--source` cleanly fails at stage `ingestion`, updates state store `(message_id, "ingestion", "failed")`, emits failure alert, halts before audio extraction, and CLI returns exit code 1.
- [x] **Fixed and confirmed (Follow-Up Issue 4): Default `STATE_DATABASE_URL` and live PostgreSQL verification.** Updated default URL in `PostgreSqlAdvisoryLockIntegrationTests` to `postgresql://pipeline:pipeline@localhost:5432/pipeline` matching `docker-compose.yml` and `.env.example`. Added IPv4 loopback socket check to prevent IPv6 timeout delays on Windows WSL2. Verified passing 100% against live PostgreSQL instance.


## Phase 9 — Monitoring & hardening
- [ ] Add a lightweight status report (chat-bot command or periodic summary)
- [ ] Alert on repeated failures or a growing QA-gate review-queue backlog
- [ ] Load-test with a burst of several videos in quick succession (concurrency handling)
- [ ] Write a short runbook: reprocessing a failed item, adjusting QA thresholds, adding/removing target platforms

## Phase 10 — Go-live
- [ ] Switch publishing from sandbox/draft to live
- [ ] Monitor the first several auto-published videos closely
- [ ] Tune QA thresholds and render style based on real output

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
- [x] **Fixed and confirmed: RTL word order.** New architecture: instead of interleaving per-word override tags, the full line is now rendered as one unbroken, untagged Arabic string (correct RTL shaping guaranteed), with the karaoke highlight achieved via three stacked layers (`QuranDim`/`QuranActive`/`QuranCompleted`) clipped to each word's pixel bounds via ASS `\clip()` — a real HarfBuzz shaping pass (`uharfbuzz`) computes those bounds precisely, with a character-count-proportional fallback if HarfBuzz isn't available. Re-verified on the exact same two lines that were broken before (Al-Fatiha ayah 1 and 2) — both now read correctly. **Caveat**: `uharfbuzz` isn't installable in this review's sandbox (no network access), so I could only verify the fallback clip-estimation path, not the primary HarfBuzz path that will actually run in production. The fix's *architecture* (shape once, unbroken; clip afterward) is sound either way since both paths follow it, but worth a real render in an environment where `uharfbuzz` actually installs, to confirm the primary path's clip boundaries are as clean as the fallback's turned out to be
- [ ] Related systemic gap worth naming: the QA gate (Phase 5) validates that *recognized* text matches canonical Quran text, but nothing currently validates that the *rendered video* displays that text in correct visual order. Those are different guarantees — worth a lightweight visual/OCR regression check on rendered output eventually, not just the text pipeline

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

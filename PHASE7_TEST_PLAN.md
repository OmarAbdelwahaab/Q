# Phase 7 Test Plan — Multi-Platform Publishing (Ayrshare & Media Uploader)

## 1. Overview & Objectives
Phase 7 validates the automated multi-platform publishing stage (SPEC §4.7):
- Caption & hashtag templating with Arabic Quranic reference formatting (`سورة {surah_name} • الآية {ayah}` or `الآيات {start}-{end}`), canonical diacritized text excerpt, branding handle, and curated hashtags.
- Platform-specific character constraints (e.g. 280 characters on X/Twitter, 1024 characters on Telegram media caption) with graceful text truncation and hashtag preservation.
- Public media uploading (`MediaUploader`):
  - Resolving / uploading rendered local MP4 files to publicly accessible HTTP(S) URLs required by social publishing providers.
  - Supporting CDN/reverse proxy base URLs (`PublicUrlMediaUploader`), S3/MinIO buckets (`S3MediaUploader`), and hermetic test stubs (`StubMediaUploader`).
- Ayrshare multi-platform publishing client (`MultiPlatformPublishClient`):
  - Targets official Ayrshare endpoint `POST /api/post` (singular).
  - Serializes required fields (`post`, `platforms`, `mediaUrls`, `isVideo=True`, `title`, `draft`).
  - Automatic platform mapping (`x` $\leftrightarrow$ `twitter`).
  - Resilient retry on transient network failures and HTTP 429/50x errors.
  - Comprehensive response parser handling `postIds` array and platform error entries.
  - In-memory stub client (`StubPublishClient`) for hermetic local testing and offline execution.
- Strict idempotency enforcement: preventing duplicate posts by checking `pipeline_items` state and `publish/{message_id}.json` before executing any external API call.
- Draft / sandbox mode validation: supporting `PUBLISH_DRAFT_MODE=true` or `--draft` flag to stage posts safely without live distribution.
- Test runner compatibility: written in pure standard library `unittest.TestCase` with `asyncio.run(...)` for 100% pass rate in network-isolated sandboxes under `python -m unittest discover`, with `pytest-asyncio` support for `pytest -v`.

---

## 2. Test Scope & Matrix

| Category | Test Case | Target Component | Expected Behavior |
|---|---|---|---|
| **Caption Templater** | Single ayah reference | `CaptionTemplater` | Formats `سورة الفاتحة • الآية 1` |
| **Caption Templater** | Ayah range reference | `CaptionTemplater` | Formats `سورة البقرة • الآيات 255-257` |
| **Caption Templater** | Default structure | `CaptionTemplater` | Combines reference, verse text, handle, and hashtags |
| **Caption Templater** | Missing handle | `CaptionTemplater` | Gracefully omits handle line without extra empty rows |
| **Caption Templater** | Custom template & tags | `CaptionTemplater` | Correctly interpolates custom tokens and custom hashtags |
| **Caption Templater** | X / Twitter 280-char cap | `CaptionTemplater` | Truncates canonical text with `...` keeping header and tags $\le 280$ chars |
| **Caption Templater** | Telegram 1024-char cap | `CaptionTemplater` | Enforces 1024-character media caption boundary |
| **Media Uploader** | Public URL resolution | `PublicUrlMediaUploader` | Formats public media URL with base CDN prefix |
| **Media Uploader** | S3 URL construction | `S3MediaUploader` | Uploads/constructs public URL with endpoint or bucket |
| **Media Uploader** | Stub history recording | `StubMediaUploader` | Records uploads and returns deterministic simulated URL |
| **Ayrshare Client** | Endpoint resolution | `MultiPlatformPublishClient` | Resolves official endpoint `/api/post` (singular) |
| **Ayrshare Client** | Payload serialization | `MultiPlatformPublishClient` | Sends `post`, `platforms` (with `twitter`), `mediaUrls`, `isVideo` |
| **Ayrshare Client** | Response parsing | `MultiPlatformPublishClient` | Parses `postIds` array, mapping `twitter` $\to$ `x` |
| **Ayrshare Client** | Error list parsing | `MultiPlatformPublishClient` | Maps Ayrshare `errors` array to platform failure statuses |
| **Ayrshare Client** | HTTP 429 retry backoff | `MultiPlatformPublishClient` | Retries on rate limit (429) with backoff and succeeds |
| **Stub Client** | Stub recording | `StubPublishClient` | Records requests and maps draft status to `draft_created` |
| **Stub Client** | Stub partial failure | `StubPublishClient` | Accurately returns `overall_status="partial"` with failing platform error |
| **Stub Client** | Stub full failure | `StubPublishClient` | Accurately returns `overall_status="failed"` when all platforms fail |
| **Publish Service** | Happy path with uploader | `PublishService` | Uploads media, publishes, writes `publish/{id}.json`, marks state `completed` |
| **Publish Service** | Idempotency gate | `PublishService` | Skips duplicate call, returns `skipped_already_published`, 0 client/upload calls |
| **Publish Service** | Draft / sandbox mode | `PublishService` | Stages in draft mode, records `draft_mode=True` in artifact |
| **Publish Service** | Missing video failure | `PublishService` | Fails closed, marks state `failed`, dispatches failure alert |
| **Publish Service** | Missing match metadata | `PublishService` | Fails closed, marks state `failed`, dispatches failure alert |
| **Publish Service** | Partial platform failure | `PublishService` | Writes artifact, sets state `completed` with error details, alerts |
| **CLI & Config** | Environment settings | `PublishSettings` | Correctly loads env vars, media base URL, and draft flags |
| **CLI & Config** | App CLI execution | `pipeline.publish.app` | CLI runs end-to-end with stub fallback, idempotency exits code 0 |

---

## 3. Execution Commands

### Standard Library Discovery (Zero External Dependencies)
```powershell
python -m unittest discover -s pipeline/tests -v
```

### Pytest Regression Suite
```powershell
pytest -v
```

### CLI Simulation in Draft Mode
```powershell
python -m pipeline.publish.app <message_id> --draft
```

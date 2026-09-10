# Phase 7 Test Plan — Multi-Platform Publishing

## 1. Overview & Objectives
Phase 7 validates the automated multi-platform publishing stage (SPEC §4.7):
- Caption & hashtag templating with Arabic Quranic reference formatting (`سورة {surah_name} • الآية {ayah}` or `الآيات {start}-{end}`), canonical diacritized text excerpt, branding handle, and curated hashtags.
- Platform-specific character constraints (e.g. 280 characters on X/Twitter, 1024 characters on Telegram media caption) with graceful text truncation and hashtag preservation.
- Multi-platform publishing client:
  - Unified HTTP client (`MultiPlatformPublishClient`) with auth headers, request payload serialization, response parsing across common schemas, and exponential backoff retry on HTTP 429 and 50x errors.
  - In-memory stub client (`StubPublishClient`) for hermetic local testing and offline execution.
- Strict idempotency enforcement: preventing duplicate posts by checking `pipeline_items` state and `publish/{message_id}.json` before executing any external API call.
- Draft / sandbox mode validation: supporting `PUBLISH_DRAFT_MODE=true` or `--draft` flag to stage posts safely without live distribution.
- Atomic artifact emission: writing `publish/{message_id}.json` with per-platform status, post IDs, and URLs.
- State store integration (`pipeline_items` stage `publish`) and operational failure alerting via `CompositeAlertService`.

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
| **Client** | Stub client recording | `StubPublishClient` | Records requests and maps draft status to `draft_created` |
| **Client** | Stub partial failure | `StubPublishClient` | Accurately returns `overall_status="partial"` with failing platform error |
| **Client** | Stub full failure | `StubPublishClient` | Accurately returns `overall_status="failed"` when all platforms fail |
| **Client** | HTTP response parsing | `MultiPlatformPublishClient` | Parses dict and list responses into `PlatformPublishResult` items |
| **Client** | HTTP 429 retry backoff | `MultiPlatformPublishClient` | Retries on rate limit (429) with backoff and succeeds |
| **Publish Service** | Happy path execution | `PublishService` | Publishes, writes `publish/{id}.json`, marks state `completed` |
| **Publish Service** | Idempotency gate | `PublishService` | Skips duplicate call, returns `skipped_already_published`, 0 client calls |
| **Publish Service** | Draft / sandbox mode | `PublishService` | Strafts in sandbox mode, records `draft_mode=True` in artifact |
| **Publish Service** | Missing video failure | `PublishService` | Fails closed, marks state `failed`, dispatches failure alert |
| **Publish Service** | Missing match metadata | `PublishService` | Fails closed, marks state `failed`, dispatches failure alert |
| **Publish Service** | Partial platform failure | `PublishService` | Writes artifact, sets state `completed` with error details, alerts |
| **CLI & Config** | Environment settings | `PublishSettings` | Correctly loads env vars, platform tuples, and draft flags |
| **CLI & Config** | App CLI execution | `pipeline.publish.app` | CLI runs end-to-end with stub fallback, idempotency exits code 0 |

---

## 3. Execution Commands

### Unit & Publishing Suite
```powershell
pytest pipeline/tests/test_publish.py -v
```

### Full Regression Suite
```powershell
pytest -v
```

### CLI Simulation in Draft Mode
```powershell
python -m pipeline.publish.app <message_id> --draft
```

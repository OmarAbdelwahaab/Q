# Phase 5 Test Plan — QA Gate

## Acceptance criteria

Given `match/{message_id}.json` (with `match_confidence`, `canonical_text`, and `recognized_text`) and `align/{message_id}.json` (with `alignment_coverage` and word-level timings), the QA gate must evaluate both scores against configurable thresholds (`QA_MATCH_CONFIDENCE_THRESHOLD`, default `0.90`; `QA_ALIGNMENT_COVERAGE_THRESHOLD`, default `0.95`).

- **Pass Path**: When `match_confidence >= match_confidence_threshold` AND `alignment_coverage >= alignment_coverage_threshold`, state is updated to `approved`, the render trigger is invoked, and no hold alert or review queue item is generated.
- **Fail / Hold Path**: When either score fails its threshold, state is updated to `rejected`, a complete diagnostic quarantine record is written to `review_queue/{message_id}.json`, a rich alert is dispatched containing recognized text, canonical text, both scores, and failure reasons, and render/publish execution is strictly blocked.
- **Operational Failures**: Missing, unreadable, or malformed artifacts update state to `failed`, dispatch an operational failure alert, and halt downstream execution.

Run local automated tests:

```powershell
python -m unittest discover -s pipeline/tests -v
```

## Unit tests

| ID | Scenario | Assertions |
| --- | --- | --- |
| QA-01 | Combined scores pass | `status="approved"`, `approved=True`, state row updated to `approved`, render trigger invoked, no review queue file, no alert. |
| QA-02 | Low match confidence rejection | `match_confidence < threshold` results in `status="rejected"`, state row `rejected`, review queue artifact written atomically, hold alert dispatched, render trigger NOT called. |
| QA-03 | Low alignment coverage rejection | `alignment_coverage < threshold` results in `status="rejected"`, state row `rejected`, review queue artifact written, hold alert dispatched, render trigger NOT called. |
| QA-04 | Dual score failure | Both scores below thresholds; review queue artifact and alert include both rejection reasons. |
| QA-05 | Exact boundary equality | Exact threshold values (`0.9000`, `0.9500`) pass; values strictly below (`0.8999`) fail. |
| QA-06 | Missing match artifact | Fails closed: state row marked `failed`, operational alert dispatched, no review queue file written, render NOT invoked. |
| QA-07 | Missing alignment artifact | Fails closed: state row marked `failed`, operational alert dispatched, no review queue file written, render NOT invoked. |
| QA-08 | Corrupted artifact JSON | Fails closed gracefully on invalid JSON: state row marked `failed`, operational alert dispatched, render NOT invoked. |
| QA-09 | Config-driven thresholds | Custom thresholds configured via `QAGateSettings` / environment variables are respected. |
| QA-10 | Threshold validation | Out-of-bounds threshold values (e.g. `> 1.0` or `< 0.0`) raise `ValueError` upon configuration load. |

## Integration tests

| ID | Scenario | Preconditions | Assertions |
| --- | --- | --- | --- |
| QI-01 | CLI Invocation | Valid match and align artifacts exist under `PIPELINE_STORAGE_ROOT` | Run `python -m pipeline.qa_gate.app MESSAGE_ID`; exits code 0 on approval, code 1 on rejection/failure. |
| QI-02 | Review Queue Persistence | Rejected message evaluated via CLI or service | Output `review_queue/{message_id}.json` is formatted with full metadata (`surah`, `ayah_start`, `ayah_end`, `canonical_text`, `recognized_text`, scores, timestamps). |
| QI-03 | Composite Alert Dispatch | Webhook and Telegram bot configured | Validates payload delivery to external webhook JSON and Telegram bot endpoints with both Arabic texts and score metrics. |

## End-to-end test

1. Process a verified recitation through ingestion, extraction, recognition, and alignment.
2. Run `python -m pipeline.qa_gate.app MESSAGE_ID`.
3. Verify that the QA gate transitions state to `approved` and passes execution to the Phase 6 render trigger.
4. Process a degraded sample (e.g., mismatched audio or low-confidence recitation):
   - Assert state is recorded as `rejected`.
   - Assert `review_queue/{message_id}.json` exists and matches the quarantine schema.
   - Assert hold alert contains full diagnostic Arabic text and score breakdowns.
   - Verify that neither render nor publish is ever triggered.

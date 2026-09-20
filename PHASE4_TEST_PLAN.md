# Phase 4 Test Plan — Word-Level Forced Alignment

## Acceptance criteria

Given `audio/{message_id}.wav` and `match/{message_id}.json`, output `align/{message_id}.json` containing ordered `{word, start_ms, end_ms}` entries and `alignment_coverage`. Timings must be positive, monotonic, and within the WAV duration. Any invalid output, unavailable aligner, or unreadable input must fail the stage, alert, and publish no final alignment artifact.

Run local automated tests:

```powershell
python -m unittest discover -s pipeline/tests -v
```

## Unit tests

| ID | Scenario | Assertions |
| --- | --- | --- |
| AL-01 | Valid CTC JSON | Uses Arabic/word CLI arguments; converts seconds to milliseconds. |
| AL-02 | Timing regression | Non-monotonic, zero-length, or out-of-duration timings are rejected. |
| AL-03 | Partial result | Artifact holds ordered words and coverage = aligned canonical words / all canonical words. |
| AL-04 | Model failure | State is failed, one alert is issued, no final artifact exists. |

## Integration tests

| ID | Scenario | Preconditions | Assertions |
| --- | --- | --- |
| AI-01 | Primary aligner | Install `ctc-forced-aligner`, FFmpeg, model access, Arabic worker | Run the documented Arabic wav2vec2 command against a verified WAV; verify JSON sidecar and pipeline artifact. |
| AI-02 | Device performance | Same fixture on CPU and configured GPU | Record latency, peak memory, and GPU batch size; use GPU only when it meets the 15–30 minute pipeline target. |
| AI-03 | Regression set | ≥20 verified audio/range fixtures | Verify word order/timestamps by listening review; record coverage distribution/outliers for Phase 5 tuning. |

## End-to-end test

1. Run a known recitation through ingestion, extraction, and recognition with a verified match.
2. Run `python -m pipeline.alignment.app MESSAGE_ID` on the real alignment worker.
3. Verify canonical word order, timing bounds, coverage, completed state, and one artifact.
4. Force missing runtime and malformed CTC JSON; verify failure/alert/no artifact and a later valid message succeeds.

## Fallback decision

`quran-align` is not maintained in v1. The primary CTC tool supports Arabic-native models, word granularity, and JSON output; a second runtime adds unverified operational complexity. Reconsider only if regression results identify a clean studio-recording cohort where CTC fails the Phase 5 target.

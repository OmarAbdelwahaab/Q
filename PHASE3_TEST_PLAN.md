# Phase 3 Test Plan — Verse Recognition and Matching

## Acceptance criteria

For every valid `audio/{message_id}.wav`, recognition must obtain an Arabic transcript, match a contiguous canonical Uthmani ayah range, and atomically write `match/{message_id}.json`. The artifact must include `surah`, `ayah_start`, `ayah_end`, `canonical_text`, and `match_confidence`. A transcription, corpus, or matching failure must leave no completed artifact, mark `recognition` failed, alert, and allow later messages to continue.

Run automated tests:

```powershell
python -m unittest discover -s pipeline/tests -v
```

## Unit tests

| ID | Scenario | Expected result |
| --- | --- | --- |
| RC-01 | Diacritized canonical text vs. unvocalized ASR | Normalization identifies the expected contiguous ayah range with a high score. |
| RC-02 | Successful injected transcription | Complete artifact, completed state, no alert. |
| RC-03 | ASR endpoint failure | Failed state, one alert, no final artifact. |
| RC-04 | Existing corpus cache | Cache is read with no HTTP request. |

## Integration tests

| ID | Scenario | Preconditions | Assertions |
| --- | --- | --- |
| RI-01 | Quran.com bootstrap | Empty disposable cache, outbound access | All 114 chapters cache once; next run uses no HTTP. |
| RI-02 | Hosted Whisper-compatible ASR | Sandbox endpoint, verified Arabic WAV | Request contains `file`, `model`, `language=ar`; returned text creates a match. |
| RI-03 | Regression set | ≥20 manually verified WAV/range pairs | Record exact-range accuracy and score distribution before choosing QA thresholds. |

## End-to-end test

1. Complete Phase 1 ingestion and Phase 2 extraction for a known recitation in disposable storage.
2. Set `ASR_API_URL`, `ASR_API_KEY`, and an empty `QURAN_CORPUS_CACHE_PATH`; run `python -m pipeline.recognition.app MESSAGE_ID`.
3. Verify the cache, match artifact, completed state row, and manually verified range.
4. Repeat with an unreachable endpoint; verify failed state, alert, no match artifact, then a later valid message succeeds.

## Performance/release gate

Time corpus bootstrap separately from jobs: normal jobs use the local cache and make one ASR call. Measure regression-set median/p95 recognition time against the 15–30 minute end-to-end target. Retain message ID, expected/output range, confidence, and pass/fail; Phase 5 thresholds may only be selected after reviewing false matches.

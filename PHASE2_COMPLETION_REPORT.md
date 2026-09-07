# Phase 2 Completion Report — Audio Extraction

**Status:** complete for the Phase 2 scope in `TASKS.md` and SPEC §4.2.

**Date:** 2026-09-07

## Delivered

- Added `pipeline.audio.FFmpegAudioExtractor`, a shell-free integration with the
  third-party `ffprobe` and `ffmpeg` executables. It probes the first audio stream,
  extracts only that stream, and emits 16 kHz mono 16-bit PCM WAV.
- Added `AudioExtractionService` and a runnable entry point:
  `python -m pipeline.audio.app MESSAGE_ID`. It reads `raw/{message_id}.mp4` and
  writes `audio/{message_id}.wav` beneath `PIPELINE_STORAGE_ROOT` by default.
- Added `FFMPEG_BINARY`, `FFPROBE_BINARY`, and `AUDIO_MIN_DURATION_SECONDS`
  configuration variables.
- Added explicit handling for missing audio streams, short clips, corrupt media,
  missing executables, and invalid output. Failures update state, alert, and leave no
  final artifact behind.
- Added atomic output publication using a temporary `.partial.wav`.
- Added performance-conscious command construction: probe before decode, disable video
  processing (`-vn`), map one audio stream, and let ffmpeg select efficient threading.
- Updated the implementation plan and added the detailed [Phase 2 test plan](PHASE2_TEST_PLAN.md).

## Phase 1 handoff review

| Requirement | Review result | Evidence |
| --- | --- | --- |
| Listen for Telegram video posts | Ready | `TelethonIngestionListener` filters video messages and forwards normalized events. |
| Save raw input as `raw/{message_id}.mp4` | Ready | `IngestionService` and its automated test verify the output path. |
| Capture ID, timestamp, channel, duration | Ready after transition update | `pipeline_messages` now records all four source attributes and the test asserts them. |
| Retry 3 times, then alert and continue | Ready | Bounded retries/backoff, failed state, alert dispatch, and test coverage. |
| State and structured logging | Ready | SQLite state repository and structured logging are used by the listener/service. |

Phase 1 is ready to hand off to audio. A live Telegram credential smoke test remains a
deployment check and was not run against a real channel in this workspace.

## Validation performed

Executed on 2026-09-07:

```text
python -m unittest discover -s pipeline/tests -v
Ran 9 tests — OK
```

This includes six audio unit/error-path tests, a real ffmpeg/ffprobe MP4 → WAV
integration test, and two Phase 1 ingestion tests. `python -m pytest -q` was attempted,
but pytest is not installed in the current interpreter; that does not block the
standard-library unittest suite above.

## Transition readiness

Phase 2 has no Quran-text or publishing path, so it cannot bypass the mandatory QA gate
planned for Phase 5. Its WAV artifact/state contract is ready for Phase 3 recognition.
Before starting Phase 3, complete deployment checks AI-02 through AI-04 in the test plan
and provision the chosen Arabic ASR and canonical corpus implementation.

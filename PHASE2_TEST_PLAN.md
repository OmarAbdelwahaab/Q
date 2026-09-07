# Phase 2 Test Plan — Audio Extraction

## Purpose and acceptance criteria

This plan verifies SPEC §4.2. For every completed ingestion, the audio stage must
produce `audio/{message_id}.wav` as a 16 kHz, mono, 16-bit PCM WAV. It must mark the
`audio` state row `completed` only after validating the artifact. Media without an
audio track, media shorter than the configured minimum, corrupt media, or a missing
media tool must be marked `failed`, alerted, and must not produce a final audio
artifact.

Run the automated suite with:

```powershell
python -m unittest discover -s pipeline/tests -v
```

`pytest` can run the same tests when the optional development dependency is installed.

## Unit tests

| ID | Scenario | Setup | Assertions |
| --- | --- | --- | --- |
| AU-01 | Valid input | A `.mp4`-named sample fixture and deterministic ffprobe/ffmpeg double | WAV is saved at `audio/{id}.wav`; state is `completed`; output is mono, 16 kHz, 16-bit PCM; no alert. |
| AU-02 | Missing audio stream | ffprobe reports no selected audio stream | State is `failed`; alert; no output file; ffmpeg is not invoked. |
| AU-03 | Short audio | ffprobe reports duration below `AUDIO_MIN_DURATION_SECONDS` | State is `failed`; alert; extraction is skipped. |
| AU-04 | Decoder/corrupt input failure | ffmpeg returns a non-zero status | State is `failed`; decoder detail is retained; alert; no final output. |
| AU-05 | Tool unavailable | Invalid ffmpeg/ffprobe executable | Service reports a failed state and alert without an unhandled exception. |
| AU-06 | Output validation | Runner writes non-WAV, stereo, non-16 kHz, or zero-frame output | Partial output is rejected and removed; no completed state. |

All six unit cases are automated in `pipeline/tests/test_audio.py`.

## Integration tests

| ID | Scenario | Preconditions | Assertions |
| --- | --- | --- | --- |
| AI-01 | Real ffmpeg conversion | `ffmpeg` and `ffprobe` on `PATH` | Generate a one-second MP4, extract it, then inspect the WAV: 16,000 Hz, one channel, duration ≥0.9 s. |
| AI-02 | Production-like raw object | Downloaded Telegram MP4 in `raw/{id}.mp4` | Run `python -m pipeline.audio.app {id}` and validate output key and SQLite stage row. |
| AI-03 | Alert transport | Test webhook or Telegram sandbox credentials | Force a missing-track error and assert one event/message with ID and error. |
| AI-04 | Reprocessing | Run AI-02 twice with the same message ID | Final key is atomically replaced; one current state row exists; no partial WAV. |

AI-01 is automated and skipped only when local ffmpeg tools are unavailable. AI-02 to
AI-04 should run in the deployment environment before orchestration is enabled.

## End-to-end tests

The Phase 2 end-to-end boundary is **ingestion completion → audio completion**;
recognition and publishing deliberately remain out of scope until later phases.

1. Use a disposable Telegram test channel with a short MP4 containing clear audio.
2. Start the ingestion listener with a temporary storage root and SQLite database.
3. Post the MP4 and wait for `raw/{message_id}.mp4` and the `ingestion` stage to be complete.
4. Invoke `python -m pipeline.audio.app {message_id}`.
5. Verify the WAV properties, duration correspondence, and an `audio` state of `completed`.
6. Repeat with a video-only and deliberately truncated MP4. Verify `failed`, exactly
   one alert per run, no final WAV, and that another valid message can still complete.

No Quran text is recognized or published in this phase, so these tests carry no QA-gate
or live-publishing risk.

## Performance and reliability checks

- Confirm the generated command uses `-map 0:a:0` and `-vn`; no video decode is performed.
- Confirm automatic ffmpeg thread selection (`-threads 0`) is present.
- Time AI-02 for representative 1, 5, and 15 minute videos; flag any result that
  threatens the 15–30 minute end-to-end target in SPEC §6.
- Run AI-02 concurrently for five message IDs. Verify unique artifacts and independent state rows.
- Interrupt extraction and rerun it. Verify no partial artifact becomes visible downstream.

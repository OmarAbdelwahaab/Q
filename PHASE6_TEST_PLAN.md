# Phase 6 Test Plan — Render / Style Transformation

## 1. Overview & Objectives
Phase 6 validates the automated composition and rendering of Quran recitation edit videos:
- Background asset selection (`round_robin`, `random`, `keyed`, with procedural fallback).
- Advanced SubStation Alpha (`.ass`) karaoke subtitle generation with word-level highlight timing (`{\k<cs>}`) and line wrapping for 9:16 vertical viewports.
- Video composition via FFmpeg: 1080×1920 (9:16) resolution, H.264 video encoding, AAC untouched recitation audio muxing, subtitle burn-in, branding watermark overlay, and platform duration constraints.
- State store tracking (`pipeline_items` stage `render`) and operational alert dispatching upon failure.

---

## 2. Test Scope & Matrix

| Category | Test Case | Target Component | Expected Behavior |
|---|---|---|---|
| **Background Pool** | Empty directory fallback | `BackgroundAssetPool` | Returns `None` triggering procedural dark canvas fallback |
| **Background Pool** | Round-robin cycling | `BackgroundAssetPool` | Cycles through discovered assets sequentially and wraps |
| **Background Pool** | Keyed by surah pattern | `BackgroundAssetPool` | Matches surah pattern (e.g. `002_...`) or modulo fallback |
| **Subtitle Generator** | Timestamp formatting | `ms_to_ass_time` | Converts ms to `H:MM:SS.cs` format |
| **Subtitle Generator** | Karaoke tag generation | `KaraokeSubtitleGenerator` | Computes centiseconds and produces `{\k<cs>}<word>` |
| **Subtitle Generator** | Visual line chunking | `KaraokeSubtitleGenerator` | Breaks lines at word count threshold or recitation pauses |
| **Subtitle Generator** | Surah header formatting | `KaraokeSubtitleGenerator` | Generates Arabic surah name banner with ayah range |
| **Subtitle Generator** | Atomic file write | `write_ass_file` | Writes to `.tmp.ass` before renaming |
| **FFmpeg Wrapper** | Windows path escaping | `escape_ffmpeg_filter_path` | Escapes Windows drive colons (`C\:`) and converts slashes |
| **FFmpeg Wrapper** | Media probe | `probe_media` | Extracts width, height, video codec, duration, and audio |
| **FFmpeg Wrapper** | Command construction | `FFmpegRenderer` | Validates filtergraph with ASS burn-in and branding overlays |
| **FFmpeg Wrapper** | Resolution validation | `FFmpegRenderer` | Rejects non-1080x1920 outputs with `RenderError` |
| **Render Service** | Happy path execution | `RenderService` | Renders output, marks stage `completed`, records duration |
| **Render Service** | Missing audio failure | `RenderService` | Fails closed, marks stage `failed`, dispatches failure alert |
| **Render Service** | Missing alignment failure | `RenderService` | Fails closed, marks stage `failed`, dispatches failure alert |
| **Render Service** | `RenderTrigger` compliance | `trigger_render` | Fulfills async trigger contract for QA gate integration |
| **Real FFmpeg Integration** | End-to-end composition | Live `ffmpeg` & `ffprobe` | Generates valid 1080x1920 H.264 MP4 with audio and burned-in subtitles |

---

## 3. Execution Commands

### Unit & Mock Suite
```powershell
pytest pipeline/tests/test_render.py -v -k "not RealFFmpeg"
```

### Real FFmpeg Integration Suite
```powershell
pytest pipeline/tests/test_render.py -v -k "RealFFmpeg"
```

### Full Regression Suite
```powershell
pytest -v
```

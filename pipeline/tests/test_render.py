from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from typing import Sequence

from pipeline.render.background import BackgroundAssetPool
from pipeline.render.ffmpeg import (
    FFmpegRenderer,
    RenderError,
    VideoProbeResult,
    escape_ffmpeg_filter_path,
)
from pipeline.render.service import RenderService
from pipeline.render.subtitles import (
    AlignedWordItem,
    KaraokeSubtitleGenerator,
    ms_to_ass_time,
)
from pipeline.state.repository import PipelineStateRepository


def write_test_wav(path: Path, seconds: int = 2) -> None:
    """Write a synthetic 16kHz mono 16-bit PCM WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 16000 * seconds)


class RecordingAlertService:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_render_failure(self, message: str) -> None:
        self.messages.append(message)


class StubMediaRunner:
    """Mock runner for FFmpeg and ffprobe CLI execution."""

    def __init__(
        self,
        probe_result: dict[str, object] | None = None,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self.probe_result = probe_result or {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "duration": "2.0"},
                {"codec_type": "audio", "codec_name": "aac", "duration": "2.0"},
            ],
            "format": {"duration": "2.0"},
        }
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(command))
        cmd_str = command[0]

        if "ffprobe" in cmd_str:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.probe_result), ""
            )

        # ffmpeg stub: simulate creating output file
        if self.returncode == 0:
            out_file = Path(command[-1])
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b"dummy_mp4_video_content")

        return subprocess.CompletedProcess(
            command, self.returncode, "", self.stderr
        )


class TestBackgroundAssetPool(unittest.TestCase):
    def test_empty_asset_pool_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pool = BackgroundAssetPool(Path(temp_dir))
            self.assertEqual(pool.list_assets(), [])
            self.assertIsNone(pool.select(surah=1, message_id=101))

    def test_round_robin_selection_cycles_through_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)
            (dir_path / "bg_a.mp4").write_text("a", encoding="utf-8")
            (dir_path / "bg_b.jpg").write_text("b", encoding="utf-8")
            pool = BackgroundAssetPool(dir_path, strategy="round_robin")

            # Sequential select
            first = pool.select()
            second = pool.select()
            third = pool.select()

            self.assertEqual(first.name, "bg_a.mp4")
            self.assertEqual(second.name, "bg_b.jpg")
            self.assertEqual(third.name, "bg_a.mp4")

    def test_keyed_selection_matches_surah_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)
            (dir_path / "general.mp4").write_text("gen", encoding="utf-8")
            (dir_path / "002_baqarah.mp4").write_text("baqarah", encoding="utf-8")
            pool = BackgroundAssetPool(dir_path, strategy="keyed")

            selected = pool.select(surah=2)
            self.assertIsNotNone(selected)
            self.assertEqual(selected.name, "002_baqarah.mp4")

    def test_keyed_selection_deterministic_modulo_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)
            (dir_path / "asset1.mp4").write_text("1", encoding="utf-8")
            (dir_path / "asset2.mp4").write_text("2", encoding="utf-8")
            pool = BackgroundAssetPool(dir_path, strategy="keyed")

            # Surah 1 -> index (1 - 1) % 2 = 0
            self.assertEqual(pool.select(surah=1).name, "asset1.mp4")
            # Surah 2 -> index (2 - 1) % 2 = 1
            self.assertEqual(pool.select(surah=2).name, "asset2.mp4")


class TestKaraokeSubtitleGenerator(unittest.TestCase):
    def test_ms_to_ass_time_formatting(self) -> None:
        self.assertEqual(ms_to_ass_time(0), "0:00:00.00")
        self.assertEqual(ms_to_ass_time(1250), "0:00:01.25")
        self.assertEqual(ms_to_ass_time(65430), "0:01:05.43")

    def test_generate_bidi_karaoke_dialogue_events(self) -> None:
        generator = KaraokeSubtitleGenerator(font_name="Amiri", font_size=72)
        words = [
            {"word": "بِسْمِ", "start_ms": 100, "end_ms": 600},
            {"word": "اللَّهِ", "start_ms": 650, "end_ms": 1250},
        ]
        ass_script = generator.generate(words, surah=1, ayah_start=1, ayah_end=1)

        self.assertIn("PlayResX: 1080", ass_script)
        self.assertIn("PlayResY: 1920", ass_script)
        self.assertIn("Style: QuranText,Amiri,72", ass_script)
        self.assertIn("Style: QuranDim,Amiri,72", ass_script)
        self.assertIn("Style: QuranActive,Amiri,72", ass_script)
        self.assertIn("Style: QuranCompleted,Amiri,72", ass_script)
        self.assertIn("سورة الفاتحة • الآية 1", ass_script)
        # Check BGR gold for SurahHeader and active highlight (&H0037AFD4)
        self.assertIn("&H0037AFD4", ass_script)
        # Check that unbroken Arabic line is rendered without interleaved tags
        self.assertIn(r"{\an5\pos(540,960)}بِسْمِ اللَّهِ", ass_script)
        # Check clip-based highlight reveals
        self.assertIn(r"\clip(", ass_script)
        self.assertIn("QuranActive", ass_script)
        self.assertIn("QuranCompleted", ass_script)

    def test_get_font_family_name_from_ttf(self) -> None:
        from pipeline.render.subtitles import get_font_family_name_from_ttf

        # Existing Amiri font file
        amiri_path = Path("pipeline/assets/fonts/Amiri-Regular.ttf")
        if amiri_path.is_file():
            name = get_font_family_name_from_ttf(amiri_path)
            self.assertEqual(name, "Amiri")

        # Non-existent file falls back to stem or default
        self.assertEqual(
            get_font_family_name_from_ttf(Path("nonexistent/CustomFont.ttf")),
            "Traditional Arabic",
        )

    def test_chunk_words_on_threshold_or_pause(self) -> None:
        generator = KaraokeSubtitleGenerator(words_per_line=2, pause_threshold_ms=400)
        words = [
            AlignedWordItem("word1", 0, 300),
            AlignedWordItem("word2", 300, 600),
            # Line break due to words_per_line limit (2)
            AlignedWordItem("word3", 600, 900),
            # Line break due to long pause (500ms > 400ms)
            AlignedWordItem("word4", 1400, 1800),
        ]
        chunks = generator._chunk_words(words)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([w.word for w in chunks[0]], ["word1", "word2"])
        self.assertEqual([w.word for w in chunks[1]], ["word3"])
        self.assertEqual([w.word for w in chunks[2]], ["word4"])

    def test_write_ass_file_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "subtitles.ass"
            generator = KaraokeSubtitleGenerator()
            generator.write_ass_file(
                output_file,
                words=[{"word": "الحمد", "start_ms": 0, "end_ms": 500}],
                surah=1,
            )
            self.assertTrue(output_file.is_file())
            content = output_file.read_text(encoding="utf-8")
            self.assertIn("Dialogue:", content)
            self.assertIn("QuranActive", content)
            self.assertIn(r"\clip(", content)


class TestFFmpegRenderer(unittest.TestCase):
    def test_escape_ffmpeg_filter_path(self) -> None:
        path = Path("C:/videos/test.ass")
        escaped = escape_ffmpeg_filter_path(path)
        self.assertNotIn("C:", escaped)
        self.assertIn(r"C\:", escaped)
        self.assertNotIn("\\\\", escaped)

    def test_probe_media_parses_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_file = Path(temp_dir) / "dummy.mp4"
            media_file.write_bytes(b"dummy")
            runner = StubMediaRunner()
            renderer = FFmpegRenderer(runner=runner)
            probe = renderer.probe_media(media_file)
            self.assertEqual(probe.width, 1080)
            self.assertEqual(probe.height, 1920)
            self.assertEqual(probe.video_codec, "h264")
            self.assertEqual(probe.duration_seconds, 2.0)
            self.assertTrue(probe.has_audio)


    def test_renderer_builds_ffmpeg_command_with_subtitles_and_branding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)
            audio = dir_path / "audio.wav"; audio.write_bytes(b"wav")
            ass = dir_path / "subs.ass"; ass.write_bytes(b"ass")
            logo = dir_path / "logo.png"; logo.write_bytes(b"png")
            out = dir_path / "out.mp4"

            runner = StubMediaRunner()
            renderer = FFmpegRenderer(runner=runner)

            fonts_dir = dir_path / "fonts"
            fonts_dir.mkdir()
            result = renderer.render(
                audio_path=audio,
                ass_path=ass,
                output_path=out,
                branding_logo_path=logo,
                branding_handle="@testbrand",
                branding_position="top_right",
                max_duration_seconds=60.0,
                fonts_dir=fonts_dir,
            )

            self.assertEqual(result, out)
            self.assertTrue(out.is_file())

            # Verify FFmpeg invocation args
            last_command = runner.commands[0]
            cmd_joined = " ".join(last_command)
            self.assertIn("ffmpeg", cmd_joined)
            self.assertIn("libx264", cmd_joined)
            self.assertIn("yuv420p", cmd_joined)
            self.assertIn("aac", cmd_joined)
            self.assertIn("subs.ass", cmd_joined)
            self.assertIn("fontsdir=", cmd_joined)
            self.assertIn("@testbrand", cmd_joined)
            self.assertIn("-t 60.0", cmd_joined)


    def test_renderer_raises_on_resolution_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir)
            audio = dir_path / "audio.wav"; audio.write_bytes(b"wav")
            ass = dir_path / "subs.ass"; ass.write_bytes(b"ass")
            out = dir_path / "out.mp4"

            # Stub runner returning 720x1280 instead of 1080x1920
            runner = StubMediaRunner(
                probe_result={
                    "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280, "duration": "1.0"}],
                    "format": {"duration": "1.0"},
                }
            )
            renderer = FFmpegRenderer(runner=runner)

            with self.assertRaisesRegex(RenderError, "resolution mismatch"):
                renderer.render(audio, ass, out)


class TestRenderService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_repo = PipelineStateRepository(self.root / "state.db")
        self.alerts = RecordingAlertService()
        self.background_pool = BackgroundAssetPool(self.root / "backgrounds")
        self.runner = StubMediaRunner()
        self.renderer = FFmpegRenderer(runner=self.runner)
        self.service = RenderService(
            storage_root=self.root,
            state_repository=self.state_repo,
            alert_service=self.alerts,
            background_pool=self.background_pool,
            renderer=self.renderer,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_service_completes_and_updates_state(self) -> None:
        message_id = 601
        # Setup inputs
        audio = self.root / "audio" / f"{message_id}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True); audio.write_bytes(b"wav")

        align_file = self.root / "align" / f"{message_id}.json"
        align_file.parent.mkdir(parents=True, exist_ok=True)
        align_file.write_text(
            json.dumps({"words": [{"word": "بسم", "start_ms": 0, "end_ms": 500}]}),
            encoding="utf-8",
        )

        match_file = self.root / "match" / f"{message_id}.json"
        match_file.parent.mkdir(parents=True, exist_ok=True)
        match_file.write_text(
            json.dumps({"surah": 1, "ayah_start": 1, "ayah_end": 1}),
            encoding="utf-8",
        )

        result = asyncio.run(self.service.render(message_id))

        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.output_path)
        self.assertEqual(result.duration_seconds, 2.0)
        self.assertEqual(self.state_repo.fetch_stage(message_id, "render")["status"], "completed")
        self.assertEqual(self.alerts.messages, [])

    def test_render_service_handles_missing_audio(self) -> None:
        message_id = 602
        align_file = self.root / "align" / f"{message_id}.json"
        align_file.parent.mkdir(parents=True, exist_ok=True)
        align_file.write_text(json.dumps({"words": [{"word": "آية", "start_ms": 0, "end_ms": 300}]}), encoding="utf-8")

        result = asyncio.run(self.service.render(message_id))

        self.assertEqual(result.status, "failed")
        self.assertIn("Audio input missing", result.error or "")
        self.assertEqual(self.state_repo.fetch_stage(message_id, "render")["status"], "failed")
        self.assertEqual(len(self.alerts.messages), 1)

    def test_render_service_handles_missing_alignment(self) -> None:
        message_id = 603
        audio = self.root / "audio" / f"{message_id}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True); audio.write_bytes(b"wav")

        result = asyncio.run(self.service.render(message_id))

        self.assertEqual(result.status, "failed")
        self.assertIn("Alignment artifact missing", result.error or "")
        self.assertEqual(self.state_repo.fetch_stage(message_id, "render")["status"], "failed")
        self.assertEqual(len(self.alerts.messages), 1)

    def test_trigger_render_protocol_delegation(self) -> None:
        message_id = 604
        audio = self.root / "audio" / f"{message_id}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True); audio.write_bytes(b"wav")
        align = self.root / "align" / f"{message_id}.json"
        align.parent.mkdir(parents=True, exist_ok=True)
        align.write_text(json.dumps({"words": [{"word": "الله", "start_ms": 0, "end_ms": 400}]}), encoding="utf-8")

        asyncio.run(self.service.trigger_render(message_id))
        self.assertEqual(self.state_repo.fetch_stage(message_id, "render")["status"], "completed")


class TestRenderRealFFmpegIntegration(unittest.TestCase):
    """End-to-end integration test executing real system ffmpeg and ffprobe."""

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "Real ffmpeg or ffprobe binary not found in system PATH",
    )
    def test_real_ffmpeg_end_to_end_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            message_id = 701

            # 1. Create real synthetic audio WAV (1.5 seconds)
            audio_path = root / "audio" / f"{message_id}.wav"
            write_test_wav(audio_path, seconds=2)

            # 2. Create sample background image (1080x1920) via ffmpeg
            bg_path = root / "backgrounds" / "bg_plate.png"
            bg_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1a202c:s=1080x1920", "-frames:v", "1", str(bg_path)],
                check=True,
                capture_output=True,
            )

            # 3. Create alignment artifact
            align_path = root / "align" / f"{message_id}.json"
            align_path.parent.mkdir(parents=True, exist_ok=True)
            align_path.write_text(
                json.dumps({
                    "words": [
                        {"word": "بِسْمِ", "start_ms": 100, "end_ms": 700},
                        {"word": "اللَّهِ", "start_ms": 700, "end_ms": 1400},
                    ],
                    "alignment_coverage": 1.0,
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            # 4. Create match artifact
            match_path = root / "match" / f"{message_id}.json"
            match_path.parent.mkdir(parents=True, exist_ok=True)
            match_path.write_text(
                json.dumps({
                    "surah": 1,
                    "ayah_start": 1,
                    "ayah_end": 1,
                    "canonical_text": "بِسْمِ اللَّهِ",
                    "match_confidence": 0.99,
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            # 5. Execute RenderService with live system FFmpeg
            state_repo = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlertService()
            pool = BackgroundAssetPool(root / "backgrounds", strategy="round_robin")
            renderer = FFmpegRenderer()  # uses live ffmpeg and ffprobe

            font_path = Path("pipeline/assets/fonts/arabic-display.ttf").resolve()
            logo_path = Path("pipeline/assets/branding/logo.png").resolve()

            service = RenderService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                background_pool=pool,
                renderer=renderer,
                branding_logo_path=logo_path if logo_path.is_file() else None,
                branding_font_path=font_path if font_path.is_file() else None,
                branding_handle="@QuranHub",
            )

            result = asyncio.run(service.render(message_id))


            # 6. Verify result and outputs
            self.assertEqual(result.status, "completed")
            self.assertIsNotNone(result.output_path)
            self.assertTrue(result.output_path.is_file())
            self.assertGreater(result.output_path.stat().st_size, 1000)

            # 7. Probe rendered MP4 using real ffprobe
            probe = renderer.probe_media(result.output_path)
            self.assertEqual(probe.width, 1080)
            self.assertEqual(probe.height, 1920)
            self.assertEqual(probe.video_codec, "h264")
            self.assertTrue(probe.has_audio)
            self.assertGreaterEqual(probe.duration_seconds, 1.5)
            self.assertEqual(state_repo.fetch_stage(message_id, "render")["status"], "completed")


if __name__ == "__main__":
    unittest.main()

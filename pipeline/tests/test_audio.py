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

from pipeline.audio.ffmpeg import FFmpegAudioExtractor
from pipeline.audio.service import AudioExtractionService
from pipeline.state.repository import PipelineStateRepository


class RecordingAlertService:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_audio_extraction_failure(self, message: str) -> None:
        self.messages.append(message)


class StubMediaRunner:
    """A deterministic ffprobe/ffmpeg double; the input fixture is named .mp4."""

    def __init__(
        self,
        probe_payload: dict[str, object],
        extraction_return_code: int = 0,
        write_valid_wav: bool = True,
    ) -> None:
        self.probe_payload = probe_payload
        self.extraction_return_code = extraction_return_code
        self.write_valid_wav = write_valid_wav
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(command))
        if "ffprobe" in command[0]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.probe_payload), "")

        output = Path(command[-1])
        if self.extraction_return_code == 0:
            output.parent.mkdir(parents=True, exist_ok=True)
            if not self.write_valid_wav:
                output.write_bytes(b"not a WAV")
            else:
                with wave.open(str(output), "wb") as generated:
                    generated.setnchannels(1)
                    generated.setsampwidth(2)
                    generated.setframerate(16000)
                    generated.writeframes(b"\x00\x00" * 16000)
        return subprocess.CompletedProcess(
            command, self.extraction_return_code, "", "decoder failure" if self.extraction_return_code else ""
        )


class AudioExtractionTests(unittest.TestCase):
    def _service(self, root: Path, runner: StubMediaRunner, minimum: float = 0.25):
        alerts = RecordingAlertService()
        repository = PipelineStateRepository(root / "pipeline.db")
        service = AudioExtractionService(
            storage_root=root,
            state_repository=repository,
            alert_service=alerts,
            extractor=FFmpegAudioExtractor(minimum_duration_seconds=minimum, runner=runner),
        )
        return service, repository, alerts

    def test_extracts_normalized_wav_from_sample_video_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fixtures" / "sample-video.mp4"
            source.parent.mkdir()
            source.write_bytes(b"fixture media handled by stub")
            runner = StubMediaRunner({"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}})
            service, repository, alerts = self._service(root, runner)

            result = asyncio.run(service.extract(301, source))

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.storage_path, root / "audio" / "301.wav")
            assert result.details is not None
            self.assertEqual(result.details.sample_rate, 16000)
            self.assertEqual(result.details.channels, 1)
            self.assertEqual(result.details.duration_seconds, 1)
            self.assertEqual(repository.fetch_stage(301, "audio")["status"], "completed")
            self.assertEqual(alerts.messages, [])
            ffmpeg_command = runner.commands[1]
            self.assertIn("-vn", ffmpeg_command)
            self.assertIn("-threads", ffmpeg_command)

    def test_missing_audio_track_is_failed_and_alerted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw" / "302.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video fixture")
            runner = StubMediaRunner({"streams": [], "format": {"duration": "3"}})
            service, repository, alerts = self._service(root, runner)

            result = asyncio.run(service.extract(302, source))

            self.assertEqual(result.status, "failed")
            self.assertIn("no audio track", result.error or "")
            self.assertEqual(repository.fetch_stage(302, "audio")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)
            self.assertFalse((root / "audio" / "302.wav").exists())

    def test_short_audio_is_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw" / "303.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video fixture")
            runner = StubMediaRunner({"streams": [{"codec_type": "audio"}], "format": {"duration": "0.1"}})
            service, _, alerts = self._service(root, runner, minimum=0.25)

            result = asyncio.run(service.extract(303, source))

            self.assertEqual(result.status, "failed")
            self.assertIn("minimum", result.error or "")
            self.assertEqual(len(runner.commands), 1)
            self.assertEqual(len(alerts.messages), 1)

    def test_corrupt_media_is_failed_and_alerted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw" / "304.mp4"
            source.parent.mkdir()
            source.write_bytes(b"not an mp4")
            runner = StubMediaRunner({"streams": [{"codec_type": "audio"}]}, extraction_return_code=1)
            service, repository, alerts = self._service(root, runner)

            result = asyncio.run(service.extract(304, source))

            self.assertEqual(result.status, "failed")
            self.assertIn("ffmpeg could not extract", result.error or "")
            self.assertEqual(repository.fetch_stage(304, "audio")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)

    def test_unavailable_media_tool_is_failed_and_alerted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw" / "305.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video fixture")
            alerts = RecordingAlertService()
            repository = PipelineStateRepository(root / "pipeline.db")
            service = AudioExtractionService(
                storage_root=root,
                state_repository=repository,
                alert_service=alerts,
                extractor=FFmpegAudioExtractor(ffprobe_binary="missing-ffprobe-for-test"),
            )

            result = asyncio.run(service.extract(305, source))

            self.assertEqual(result.status, "failed")
            self.assertIn("Media tool not found", result.error or "")
            self.assertEqual(repository.fetch_stage(305, "audio")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)

    def test_invalid_ffmpeg_output_is_rejected_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw" / "306.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video fixture")
            runner = StubMediaRunner(
                {"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}},
                write_valid_wav=False,
            )
            service, repository, alerts = self._service(root, runner)

            result = asyncio.run(service.extract(306, source))

            self.assertEqual(result.status, "failed")
            self.assertIn("invalid WAV", result.error or "")
            self.assertEqual(repository.fetch_stage(306, "audio")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)
            self.assertFalse((root / "audio" / "306.wav").exists())
            self.assertFalse((root / "audio" / "306.partial.wav").exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class FFmpegIntegrationTests(unittest.TestCase):
    def test_real_video_fixture_is_normalized_to_16khz_mono_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fixture.mp4"
            created = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            details = FFmpegAudioExtractor().extract(source, root / "audio.wav")

            self.assertEqual(details.sample_rate, 16000)
            self.assertEqual(details.channels, 1)
            self.assertGreaterEqual(details.duration_seconds, 0.9)

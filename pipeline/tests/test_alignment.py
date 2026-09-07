from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path

from pipeline.alignment.ctc import AlignedWord, AlignmentError, CtcForcedAligner
from pipeline.alignment.service import AlignmentService
from pipeline.state.repository import PipelineStateRepository


def write_wav(path: Path, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * seconds)


class RecordingAlerts:
    def __init__(self) -> None: self.messages: list[str] = []
    async def send_alignment_failure(self, message: str) -> None: self.messages.append(message)


class StubAligner:
    def __init__(self, value: list[AlignedWord] | Exception) -> None: self.value = value
    def align(self, _: Path, __: str) -> list[AlignedWord]:
        if isinstance(self.value, Exception): raise self.value
        return self.value


class AlignmentTests(unittest.TestCase):
    def test_ctc_adapter_uses_arabic_word_command_and_parses_monotonic_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"; write_wav(audio)
            commands: list[list[str]] = []
            def runner(command):
                commands.append(list(command))
                Path(command[2]).with_suffix(".json").write_text(json.dumps([{"start": 0, "end": .5}, {"start": .5, "end": 1}]), encoding="utf-8")
                from subprocess import CompletedProcess
                return CompletedProcess(command, 0, "", "")
            words = CtcForcedAligner(runner=runner).align(audio, "بسم الله")
            self.assertEqual([word.word for word in words], ["بسم", "الله"])
            self.assertEqual(words[-1].end_ms, 1000)
            self.assertIn("ara", commands[0]); self.assertIn("word", commands[0])

    def test_ctc_adapter_rejects_non_monotonic_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"; write_wav(audio)
            def runner(command):
                Path(command[2]).with_suffix(".json").write_text(json.dumps([{"start": .5, "end": 1}, {"start": .4, "end": 1.2}]), encoding="utf-8")
                from subprocess import CompletedProcess
                return CompletedProcess(command, 0, "", "")
            with self.assertRaises(AlignmentError): CtcForcedAligner(runner=runner).align(audio, "بسم الله")

    def test_ctc_adapter_names_checked_locations_when_output_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"; write_wav(audio)
            def runner(command):
                from subprocess import CompletedProcess
                return CompletedProcess(command, 0, "", "")
            with self.assertRaisesRegex(AlignmentError, "searched:"):
                CtcForcedAligner(runner=runner).align(audio, "بسم الله")

    def test_service_writes_artifact_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_wav(root / "audio" / "501.wav")
            (root / "match").mkdir(); (root / "match" / "501.json").write_text(json.dumps({"canonical_text": "بسم الله الرحمن"}), encoding="utf-8")
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            service = AlignmentService(root, state, alerts, StubAligner([AlignedWord("بسم", 0, 400), AlignedWord("الله", 400, 900)]))
            result = asyncio.run(service.align(501))
            self.assertEqual(result.status, "completed"); self.assertEqual(result.coverage, 0.6667)
            artifact = json.loads((root / "align" / "501.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["alignment_coverage"], 0.6667)
            self.assertEqual(state.fetch_stage(501, "alignment")["status"], "completed")

    def test_service_failure_is_alerted_and_has_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_wav(root / "audio" / "502.wav")
            (root / "match").mkdir(); (root / "match" / "502.json").write_text(json.dumps({"canonical_text": "بسم الله"}), encoding="utf-8")
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            result = asyncio.run(AlignmentService(root, state, alerts, StubAligner(AlignmentError("model unavailable"))).align(502))
            self.assertEqual(result.status, "failed"); self.assertEqual(len(alerts.messages), 1)
            self.assertFalse((root / "align" / "502.json").exists())

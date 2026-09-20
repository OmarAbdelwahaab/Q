from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.recognition.corpus import CanonicalVerse, QuranCorpus
from pipeline.recognition.asr import TranscriptionError
from pipeline.recognition.matcher import QuranMatcher, normalize_arabic
from pipeline.recognition.service import RecognitionService
from pipeline.state.repository import PipelineStateRepository


class StubTranscriber:
    def __init__(self, text: str | Exception) -> None:
        self.text = text

    def transcribe(self, _: Path) -> str:
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


class RecordingAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_recognition_failure(self, message: str) -> None:
        self.messages.append(message)


class RecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = QuranCorpus([
            CanonicalVerse(1, 1, "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"),
            CanonicalVerse(1, 2, "الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ"),
            CanonicalVerse(1, 3, "الرَّحْمَٰنِ الرَّحِيمِ"),
            CanonicalVerse(2, 1, "الم"),
        ])

    def test_normalization_and_contiguous_range_match(self) -> None:
        match = QuranMatcher(self.corpus).match("بسم الله الرحمن الرحيم الحمد لله رب العالمين")
        self.assertEqual((match.surah, match.ayah_start, match.ayah_end), (1, 1, 2))
        self.assertGreaterEqual(match.confidence, 0.95)
        self.assertEqual(normalize_arabic("الرَّحْمَٰنِ"), "الرحمن")

    def test_completed_recognition_writes_spec_match_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio" / "401.wav"
            audio.parent.mkdir(); audio.write_bytes(b"fixture")
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            service = RecognitionService(root, state, alerts, StubTranscriber("بسم الله الرحمن الرحيم"), QuranMatcher(self.corpus))
            result = asyncio.run(service.recognize(401))
            self.assertEqual(result.status, "completed")
            artifact = json.loads((root / "match" / "401.json").read_text(encoding="utf-8"))
            self.assertEqual({"surah", "ayah_start", "ayah_end", "canonical_text", "match_confidence", "recognized_text"}, set(artifact))
            self.assertEqual(artifact["surah"], 1)
            self.assertEqual(state.fetch_stage(401, "recognition")["status"], "completed")
            self.assertEqual(alerts.messages, [])

    def test_asr_failure_marks_state_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            service = RecognitionService(root, state, alerts, StubTranscriber(TranscriptionError("endpoint unavailable")), QuranMatcher(self.corpus))
            result = asyncio.run(service.recognize(402))
            self.assertEqual(result.status, "failed")
            self.assertIn("endpoint unavailable", result.error or "")
            self.assertEqual(state.fetch_stage(402, "recognition")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)

    def test_cached_corpus_is_used_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "quran.json"
            cache.write_text(json.dumps([{"surah": 1, "ayah": 1, "text": "بِسْمِ اللَّهِ"}], ensure_ascii=False), encoding="utf-8")
            corpus = QuranCorpus.load_or_fetch(cache, "https://invalid.example")
            self.assertEqual(corpus.verses[0].text, "بِسْمِ اللَّهِ")

    def test_bundled_corpus_is_used_when_cache_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cold_boot_cache.json"
            self.assertFalse(cache.exists())
            corpus = QuranCorpus.load_or_fetch(cache, "https://invalid.example-must-not-call")
            self.assertEqual(len(corpus.verses), 6236)
            self.assertTrue(cache.exists())
            self.assertEqual(corpus.verses[0].surah, 1)
            self.assertEqual(corpus.verses[0].ayah, 1)

    def test_whisper_transcriber_requires_api_key_for_cloud(self) -> None:
        from pipeline.recognition.asr import WhisperTranscriber

        transcriber = WhisperTranscriber("https://api.groq.com/openai/v1/audio/transcriptions", None, "whisper-large-v3")
        with tempfile.TemporaryDirectory() as directory:
            fake_audio = Path(directory) / "audio.wav"
            fake_audio.write_bytes(b"dummy audio")
            with self.assertRaises(TranscriptionError) as ctx:
                transcriber.transcribe(fake_audio)
            self.assertIn("ASR_API_KEY is not configured", str(ctx.exception))

    def test_whisper_transcriber_formats_http_error_with_body(self) -> None:
        import io
        import urllib.error
        from unittest.mock import patch
        from pipeline.recognition.asr import WhisperTranscriber

        transcriber = WhisperTranscriber("https://api.groq.com/openai/v1/audio/transcriptions", "gsk_test123", "whisper-large-v3")
        with tempfile.TemporaryDirectory() as directory:
            fake_audio = Path(directory) / "audio.wav"
            fake_audio.write_bytes(b"dummy audio")

            err_body = io.BytesIO(b'{"error": {"message": "Invalid API Key provided"}}')
            http_err = urllib.error.HTTPError(
                url="https://api.groq.com/openai/v1/audio/transcriptions",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=err_body,
            )

            with patch("urllib.request.urlopen", side_effect=http_err):
                with self.assertRaises(TranscriptionError) as ctx:
                    transcriber.transcribe(fake_audio)
                self.assertIn("401", str(ctx.exception))
                self.assertIn("Invalid API Key provided", str(ctx.exception))
                self.assertIn("Verify that ASR_API_KEY is correctly set", str(ctx.exception))

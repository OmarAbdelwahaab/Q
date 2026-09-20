"""Phase 3 orchestration: WAV -> transcript -> canonical Quran match artifact."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from pipeline.logging import get_logger
from pipeline.recognition.asr import TranscriptionError
from pipeline.recognition.matcher import QuranMatcher, VerseMatch


class StateRepository(Protocol):
    def upsert_stage(self, message_id: int, stage: str, status: str, error: str | None = None) -> None: ...


class AlertService(Protocol):
    async def send_recognition_failure(self, message: str) -> None: ...


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> str: ...


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    message_id: int
    status: str
    storage_path: Path | None
    match: VerseMatch | None
    error: str | None = None


class RecognitionService:
    stage_name = "recognition"

    def __init__(self, storage_root: Path, state_repository: StateRepository, alert_service: AlertService, transcriber: Transcriber, matcher: QuranMatcher) -> None:
        self.storage_root, self.state_repository = storage_root, state_repository
        self.alert_service, self.transcriber, self.matcher = alert_service, transcriber, matcher
        self.logger = get_logger(__name__, service="recognition")

    async def recognize(self, message_id: int, audio_path: Path | None = None) -> RecognitionResult:
        source = audio_path or self.storage_root / "audio" / f"{message_id}.wav"
        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")
        try:
            transcript = self.transcriber.transcribe(source)
            match = self.matcher.match(transcript)
            destination = self.storage_root / "match" / f"{message_id}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".partial.json")
            temporary.write_text(json.dumps({"surah": match.surah, "ayah_start": match.ayah_start, "ayah_end": match.ayah_end, "canonical_text": match.canonical_text, "match_confidence": match.confidence, "recognized_text": transcript}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(destination)
        except (TranscriptionError, ValueError, OSError) as exc:
            error = str(exc)
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error("Verse recognition failed", extra={"message_id": message_id, "error": error})
            await self.alert_service.send_recognition_failure(f"Pipeline verse recognition failed for Telegram message {message_id}. Error: {error}")
            return RecognitionResult(message_id, "failed", None, None, error)
        self.state_repository.upsert_stage(message_id, self.stage_name, "completed")
        self.logger.info("Verse recognized", extra={"message_id": message_id, "surah": match.surah, "ayah_start": match.ayah_start, "ayah_end": match.ayah_end, "match_confidence": match.confidence})
        return RecognitionResult(message_id, "completed", destination, match)

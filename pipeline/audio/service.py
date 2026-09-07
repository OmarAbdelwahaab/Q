"""Application service for Phase 2 audio extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.audio.ffmpeg import AudioExtractionError, AudioDetails, FFmpegAudioExtractor
from pipeline.logging import get_logger


class StateRepository(Protocol):
    def upsert_stage(
        self, message_id: int, stage: str, status: str, error: str | None = None
    ) -> None:
        """Persist a pipeline stage status."""


class AlertService(Protocol):
    async def send_audio_extraction_failure(self, message: str) -> None:
        """Send a failure alert."""


@dataclass(frozen=True, slots=True)
class AudioExtractionResult:
    message_id: int
    status: str
    storage_path: Path | None
    details: AudioDetails | None
    error: str | None = None


class AudioExtractionService:
    """Convert an ingested raw video into the canonical ASR WAV artifact."""

    stage_name = "audio"

    def __init__(
        self,
        storage_root: Path,
        state_repository: StateRepository,
        alert_service: AlertService,
        extractor: FFmpegAudioExtractor,
    ) -> None:
        self.storage_root = storage_root
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.extractor = extractor
        self.logger = get_logger(__name__, service="audio")

    async def extract(self, message_id: int, source_path: Path | None = None) -> AudioExtractionResult:
        source = source_path or self.storage_root / "raw" / f"{message_id}.mp4"
        destination = self.storage_root / "audio" / f"{message_id}.wav"
        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")

        try:
            details = self.extractor.extract(source, destination)
        except AudioExtractionError as exc:
            error = str(exc)
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error(
                "Audio extraction failed",
                extra={"message_id": message_id, "source_path": str(source), "error": error},
            )
            await self.alert_service.send_audio_extraction_failure(
                f"Pipeline audio extraction failed for Telegram message {message_id}. Error: {error}"
            )
            return AudioExtractionResult(message_id, "failed", None, None, error)

        self.state_repository.upsert_stage(message_id, self.stage_name, "completed")
        self.logger.info(
            "Audio extracted",
            extra={
                "message_id": message_id,
                "source_path": str(source),
                "storage_path": str(destination),
                "duration_seconds": details.duration_seconds,
                "sample_rate": details.sample_rate,
                "channels": details.channels,
            },
        )
        return AudioExtractionResult(message_id, "completed", destination, details)

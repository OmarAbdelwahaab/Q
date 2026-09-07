"""Phase 4 orchestration: canonical match + WAV -> word timing artifact."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from pipeline.alignment.ctc import AlignedWord, AlignmentError
from pipeline.logging import get_logger


class StateRepository(Protocol):
    def upsert_stage(self, message_id: int, stage: str, status: str, error: str | None = None) -> None: ...


class AlertService(Protocol):
    async def send_alignment_failure(self, message: str) -> None: ...


class Aligner(Protocol):
    def align(self, audio_path: Path, canonical_text: str) -> list[AlignedWord]: ...


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    message_id: int
    status: str
    storage_path: Path | None
    coverage: float | None
    error: str | None = None


class AlignmentService:
    stage_name = "alignment"

    def __init__(self, storage_root: Path, state_repository: StateRepository, alert_service: AlertService, aligner: Aligner) -> None:
        self.storage_root, self.state_repository = storage_root, state_repository
        self.alert_service, self.aligner = alert_service, aligner
        self.logger = get_logger(__name__, service="alignment")

    async def align(self, message_id: int, audio_path: Path | None = None, match_path: Path | None = None) -> AlignmentResult:
        source = audio_path or self.storage_root / "audio" / f"{message_id}.wav"
        match_artifact = match_path or self.storage_root / "match" / f"{message_id}.json"
        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")
        try:
            match = json.loads(match_artifact.read_text(encoding="utf-8"))
            canonical_text = match["canonical_text"]
            expected_words = canonical_text.split()
            words = self.aligner.align(source, canonical_text)
            coverage = round(len(words) / len(expected_words), 4)
            destination = self.storage_root / "align" / f"{message_id}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".partial.json")
            temporary.write_text(json.dumps({"words": [asdict(word) for word in words], "alignment_coverage": coverage}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(destination)
        except (AlignmentError, KeyError, OSError, json.JSONDecodeError) as exc:
            error = str(exc)
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error("Word alignment failed", extra={"message_id": message_id, "error": error})
            await self.alert_service.send_alignment_failure(f"Pipeline word alignment failed for Telegram message {message_id}. Error: {error}")
            return AlignmentResult(message_id, "failed", None, None, error)
        self.state_repository.upsert_stage(message_id, self.stage_name, "completed")
        self.logger.info("Words aligned", extra={"message_id": message_id, "word_count": len(words), "alignment_coverage": coverage})
        return AlignmentResult(message_id, "completed", destination, coverage)

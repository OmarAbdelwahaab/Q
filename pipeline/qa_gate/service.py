"""Phase 5 orchestration: QA gate verifying match confidence & alignment coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pipeline.logging import get_logger


class StateRepository(Protocol):
    def upsert_stage(
        self, message_id: int, stage: str, status: str, error: str | None = None
    ) -> None: ...


class AlertService(Protocol):
    async def send_qa_gate_hold(
        self, message: str, payload: dict[str, object] | None = None
    ) -> None: ...

    async def send_qa_gate_failure(self, message: str) -> None: ...


class RenderTrigger(Protocol):
    async def trigger_render(self, message_id: int) -> None: ...


class NoOpRenderTrigger:
    """Default render trigger for Phase 5 prior to Phase 6 implementation."""

    async def trigger_render(self, message_id: int) -> None:
        pass


@dataclass(frozen=True, slots=True)
class QAGateResult:
    message_id: int
    status: str  # "approved", "rejected", or "failed"
    approved: bool
    match_confidence: float | None = None
    alignment_coverage: float | None = None
    review_path: Path | None = None
    reasons: tuple[str, ...] = ()
    error: str | None = None


class QAGateService:
    stage_name = "qa_gate"

    def __init__(
        self,
        storage_root: Path,
        state_repository: StateRepository,
        alert_service: AlertService,
        match_confidence_threshold: float = 0.90,
        alignment_coverage_threshold: float = 0.95,
        render_trigger: RenderTrigger | None = None,
    ) -> None:
        self.storage_root = storage_root
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.match_confidence_threshold = match_confidence_threshold
        self.alignment_coverage_threshold = alignment_coverage_threshold
        self.render_trigger = render_trigger or NoOpRenderTrigger()
        self.logger = get_logger(__name__, service="qa_gate")

    async def evaluate(
        self,
        message_id: int,
        match_path: Path | None = None,
        align_path: Path | None = None,
    ) -> QAGateResult:
        match_file = match_path or self.storage_root / "match" / f"{message_id}.json"
        align_file = align_path or self.storage_root / "align" / f"{message_id}.json"

        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")

        if not match_file.exists():
            error = f"Match artifact missing: {match_file}"
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error("QA gate evaluation failed", extra={"message_id": message_id, "error": error})
            await self.alert_service.send_qa_gate_failure(
                f"QA gate failed for Telegram message {message_id}: {error}"
            )
            return QAGateResult(message_id, "failed", False, error=error)

        if not align_file.exists():
            error = f"Alignment artifact missing: {align_file}"
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error("QA gate evaluation failed", extra={"message_id": message_id, "error": error})
            await self.alert_service.send_qa_gate_failure(
                f"QA gate failed for Telegram message {message_id}: {error}"
            )
            return QAGateResult(message_id, "failed", False, error=error)

        try:
            match_data = json.loads(match_file.read_text(encoding="utf-8"))
            align_data = json.loads(align_file.read_text(encoding="utf-8"))

            match_confidence = float(match_data["match_confidence"])
            alignment_coverage = float(align_data["alignment_coverage"])
            surah = match_data.get("surah")
            ayah_start = match_data.get("ayah_start")
            ayah_end = match_data.get("ayah_end")
            canonical_text = match_data.get("canonical_text", "")
            recognized_text = match_data.get("recognized_text", "")
        except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
            error = f"Malformed artifact data: {exc}"
            self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
            self.logger.error("QA gate evaluation failed", extra={"message_id": message_id, "error": error})
            await self.alert_service.send_qa_gate_failure(
                f"QA gate failed for Telegram message {message_id}: {error}"
            )
            return QAGateResult(message_id, "failed", False, error=error)

        reasons: list[str] = []
        if match_confidence < self.match_confidence_threshold:
            reasons.append(
                f"match_confidence {match_confidence:.4f} < {self.match_confidence_threshold:.4f}"
            )
        if alignment_coverage < self.alignment_coverage_threshold:
            reasons.append(
                f"alignment_coverage {alignment_coverage:.4f} < {self.alignment_coverage_threshold:.4f}"
            )

        if not reasons:
            self.state_repository.upsert_stage(message_id, self.stage_name, "approved")
            self.logger.info(
                "QA gate approved item",
                extra={
                    "message_id": message_id,
                    "match_confidence": match_confidence,
                    "alignment_coverage": alignment_coverage,
                    "surah": surah,
                    "ayah_start": ayah_start,
                    "ayah_end": ayah_end,
                },
            )
            await self.render_trigger.trigger_render(message_id)
            return QAGateResult(
                message_id=message_id,
                status="approved",
                approved=True,
                match_confidence=match_confidence,
                alignment_coverage=alignment_coverage,
            )

        # Fail path: hold in review queue and alert
        rejection_desc = "; ".join(reasons)
        error_msg = f"QA threshold check failed: {rejection_desc}"
        self.state_repository.upsert_stage(message_id, self.stage_name, "rejected", error_msg)

        destination = self.storage_root / "review_queue" / f"{message_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        review_payload: dict[str, object] = {
            "message_id": message_id,
            "status": "held_for_review",
            "reasons": reasons,
            "match_confidence": match_confidence,
            "match_confidence_threshold": self.match_confidence_threshold,
            "alignment_coverage": alignment_coverage,
            "alignment_coverage_threshold": self.alignment_coverage_threshold,
            "surah": surah,
            "ayah_start": ayah_start,
            "ayah_end": ayah_end,
            "canonical_text": canonical_text,
            "recognized_text": recognized_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        temporary = destination.with_suffix(".partial.json")
        temporary.write_text(
            json.dumps(review_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(destination)

        alert_text = (
            f"QA Gate Held Telegram Message {message_id} For Review\n"
            f"Surah: {surah}, Ayat: {ayah_start}-{ayah_end}\n"
            f"Match Confidence: {match_confidence:.4f} (min {self.match_confidence_threshold:.4f})\n"
            f"Alignment Coverage: {alignment_coverage:.4f} (min {self.alignment_coverage_threshold:.4f})\n"
            f"Reasons: {rejection_desc}\n"
            f"Recognized text: {recognized_text}\n"
            f"Canonical text: {canonical_text}"
        )

        self.logger.warning(
            "QA gate held item for review",
            extra={
                "message_id": message_id,
                "reasons": reasons,
                "match_confidence": match_confidence,
                "alignment_coverage": alignment_coverage,
                "review_path": str(destination),
            },
        )
        await self.alert_service.send_qa_gate_hold(alert_text, payload=review_payload)

        return QAGateResult(
            message_id=message_id,
            status="rejected",
            approved=False,
            match_confidence=match_confidence,
            alignment_coverage=alignment_coverage,
            review_path=destination,
            reasons=tuple(reasons),
            error=error_msg,
        )

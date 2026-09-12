"""End-to-end pipeline orchestrator coordinating all stages with fail-closed gating."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from pipeline.alignment.service import AlignmentService
from pipeline.audio.service import AudioExtractionService
from pipeline.logging import get_logger
from pipeline.orchestration.scheduler import PostingWindowScheduler, ScheduleDecision
from pipeline.publish.service import PublishService
from pipeline.qa_gate.service import QAGateService
from pipeline.recognition.service import RecognitionService
from pipeline.render.service import RenderService
from pipeline.state.repository import PipelineStateRepository


class AlertService(Protocol):
    async def send_orchestration_failure(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PipelineExecutionSummary:
    """Summary of the complete pipeline execution for one video message."""

    message_id: int
    status: str  # "completed", "held_for_review", "failed", "skipped_already_published", "scheduled"
    stages: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    error: str | None = None


class PipelineOrchestrator:
    """Coordinating runner for the 7-stage Quran video repurposing pipeline.

    Flow:
      1. Idempotency check (skip if already published)
      2. Audio extraction (MP4 -> 16kHz mono WAV)
      3. Verse recognition (WAV -> ASR -> Quran matcher JSON)
      4. Forced alignment (WAV + Quran text -> word timestamps)
      5. QA gate (verify confidence >= threshold & coverage >= threshold)
         - If rejected: halt, alert, persist in review-queue (never renders/publishes)
      6. Scheduler check (verify time-of-day posting window and rate limits)
         - If outside window/rate-limited: delay or mark scheduled
      7. Video render (background + HarfBuzz RTL karaoke ASS + audio + branding)
      8. Multi-platform publish (upload to S3/CDN + Ayrshare fanout)
    """

    def __init__(
        self,
        storage_root: Path,
        state_repository: PipelineStateRepository,
        alert_service: Any,
        audio_service: AudioExtractionService,
        recognition_service: RecognitionService,
        alignment_service: AlignmentService,
        qa_gate_service: QAGateService,
        render_service: RenderService,
        publish_service: PublishService,
        scheduler: PostingWindowScheduler | None = None,
    ) -> None:
        self.storage_root = storage_root
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.audio_service = audio_service
        self.recognition_service = recognition_service
        self.alignment_service = alignment_service
        self.qa_gate_service = qa_gate_service
        self.render_service = render_service
        self.publish_service = publish_service
        self.scheduler = scheduler
        self.logger = get_logger(__name__, service="orchestration.runner")

    async def _alert_failure(self, stage: str, message_id: int, error: str) -> None:
        msg = f"Pipeline execution failed at stage '{stage}' for message {message_id}: {error}"
        if hasattr(self.alert_service, "send_orchestration_failure"):
            await self.alert_service.send_orchestration_failure(msg)
        elif hasattr(self.alert_service, "send_alert"):
            await self.alert_service.send_alert(msg)

    async def run(
        self,
        message_id: int,
        source_path: Path | None = None,
        draft: bool = False,
        enforce_scheduler: bool = True,
        wait_for_window: bool = False,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> PipelineExecutionSummary:
        """Execute the end-to-end pipeline for the specified message_id."""
        start_time = time.monotonic()
        stage_records: dict[str, Any] = {}

        self.logger.info(
            "Initiating pipeline execution",
            extra={"message_id": message_id, "draft": draft, "enforce_scheduler": enforce_scheduler},
        )

        # 1. Idempotency Check: skip if already completed and artifact present
        pub_stage = self.state_repository.fetch_stage(message_id, "publish")
        pub_artifact = self.storage_root / "publish" / f"{message_id}.json"
        if pub_stage and pub_stage.get("status") == "completed" and pub_artifact.exists():
            self.logger.info("Message already published; skipping execution", extra={"message_id": message_id})
            return PipelineExecutionSummary(
                message_id=message_id,
                status="skipped_already_published",
                stages={"publish": pub_stage},
                duration_seconds=round(time.monotonic() - start_time, 3),
            )

        # 2. Stage: Audio Extraction
        try:
            audio_result = await self.audio_service.extract(message_id, source_path=source_path)
            stage_records["audio"] = {
                "status": audio_result.status,
                "storage_path": str(audio_result.storage_path) if audio_result.storage_path else None,
                "error": audio_result.error,
            }
            if audio_result.status != "completed":
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="failed",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=audio_result.error or "Audio extraction failed",
                )
        except Exception as exc:
            err = f"Unexpected error in audio extraction: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "audio", "failed", err)
            await self._alert_failure("audio", message_id, err)
            stage_records["audio"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

        # 3. Stage: Verse Recognition
        try:
            rec_result = await self.recognition_service.recognize(message_id)
            stage_records["recognition"] = {
                "status": rec_result.status,
                "storage_path": str(rec_result.storage_path) if rec_result.storage_path else None,
                "error": rec_result.error,
            }
            if rec_result.status != "completed":
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="failed",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=rec_result.error or "Recognition failed",
                )
        except Exception as exc:
            err = f"Unexpected error in verse recognition: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "recognition", "failed", err)
            await self._alert_failure("recognition", message_id, err)
            stage_records["recognition"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

        # 4. Stage: Forced Alignment
        try:
            align_result = await self.alignment_service.align(message_id)
            stage_records["alignment"] = {
                "status": align_result.status,
                "storage_path": str(align_result.storage_path) if align_result.storage_path else None,
                "coverage": align_result.coverage,
                "error": align_result.error,
            }
            if align_result.status != "completed":
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="failed",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=align_result.error or "Forced alignment failed",
                )
        except Exception as exc:
            err = f"Unexpected error in forced alignment: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "alignment", "failed", err)
            await self._alert_failure("alignment", message_id, err)
            stage_records["alignment"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

        # 5. Stage: QA Gate Verification
        try:
            qa_result = await self.qa_gate_service.evaluate(message_id)
            stage_records["qa_gate"] = {
                "status": qa_result.status,
                "approved": qa_result.approved,
                "match_confidence": qa_result.match_confidence,
                "alignment_coverage": qa_result.alignment_coverage,
                "reasons": qa_result.reasons,
                "error": qa_result.error,
            }
            if not qa_result.approved:
                self.logger.warning(
                    "QA gate held message for manual review; halting pipeline",
                    extra={"message_id": message_id, "reasons": qa_result.reasons},
                )
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="held_for_review",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=qa_result.error or "; ".join(qa_result.reasons),
                )
        except Exception as exc:
            err = f"Unexpected error in QA gate: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "qa_gate", "failed", err)
            await self._alert_failure("qa_gate", message_id, err)
            stage_records["qa_gate"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

        # 6. Stage: Posting Window & Rate Limit Scheduler Evaluation
        if self.scheduler and enforce_scheduler and self.scheduler.enabled:
            last_pub = self.state_repository.fetch_latest_published_timestamp()
            decision = self.scheduler.evaluate(last_published_at=last_pub)
            stage_records["scheduler"] = {
                "can_post": decision.can_post,
                "wait_seconds": decision.wait_seconds,
                "reason": decision.reason,
            }
            if not decision.can_post:
                if wait_for_window:
                    self.logger.info(
                        "Scheduling delay required before rendering/publishing",
                        extra={"wait_seconds": decision.wait_seconds, "reason": decision.reason},
                    )
                    sleep_coro = sleep_fn or asyncio.sleep
                    await sleep_coro(decision.wait_seconds)
                else:
                    self.logger.info(
                        "Posting window / rate limit active; pausing before publishing",
                        extra={"message_id": message_id, "reason": decision.reason},
                    )
                    self.state_repository.upsert_stage(
                        message_id, "publish", "scheduled", decision.reason
                    )
                    return PipelineExecutionSummary(
                        message_id=message_id,
                        status="scheduled",
                        stages=stage_records,
                        duration_seconds=round(time.monotonic() - start_time, 3),
                        error=decision.reason,
                    )

        # 7. Stage: Video Render
        try:
            render_result = await self.render_service.render(message_id)
            stage_records["render"] = {
                "status": render_result.status,
                "output_path": str(render_result.output_path) if render_result.output_path else None,
                "duration_seconds": render_result.duration_seconds,
                "error": render_result.error,
            }
            if render_result.status != "completed":
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="failed",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=render_result.error or "Render failed",
                )
        except Exception as exc:
            err = f"Unexpected error in video render: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "render", "failed", err)
            await self._alert_failure("render", message_id, err)
            stage_records["render"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

        # 8. Stage: Multi-Platform Publishing
        try:
            publish_result = await self.publish_service.publish(message_id, draft_mode=draft)
            stage_records["publish"] = {

                "status": publish_result.status,
                "artifact_path": str(publish_result.artifact_path) if publish_result.artifact_path else None,
                "error": publish_result.error,
            }
            final_status = (
                "completed"
                if publish_result.status in ("completed", "partial")
                else publish_result.status
            )
            return PipelineExecutionSummary(
                message_id=message_id,
                status=final_status,
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=publish_result.error,
            )
        except Exception as exc:
            err = f"Unexpected error in publishing: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, "publish", "failed", err)
            await self._alert_failure("publish", message_id, err)
            stage_records["publish"] = {"status": "failed", "error": err}
            return PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

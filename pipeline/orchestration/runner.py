"""End-to-end pipeline orchestrator coordinating all stages with fail-closed gating."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
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
    status: str  # "completed", "held_for_review", "failed", "skipped_already_published", "skipped_active_execution", "scheduled"
    stages: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    error: str | None = None


class PipelineOrchestrator:
    """Coordinating runner for the 7-stage Quran video repurposing pipeline.

    Flow:
      1. Atomic idempotency claim (skip if already published or actively processing)
      2. Audio extraction (MP4 -> 16kHz mono WAV)
      3. Verse recognition (WAV -> ASR -> Quran matcher JSON)
      4. Forced alignment (WAV + Quran text -> word timestamps)
      5. QA gate (verify confidence >= threshold & coverage >= threshold)
         - If rejected: halt, alert, persist in review-queue (never renders/publishes)
      6. Scheduler check & atomic rate-limit reservation
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

    async def _run_stage(
        self,
        stage_name: str,
        message_id: int,
        stage_coro: Awaitable[Any],
        stage_records: dict[str, Any],
        record_builder: Callable[[Any], dict[str, Any]],
        start_time: float,
        success_statuses: tuple[str, ...] = ("completed",),
    ) -> tuple[bool, Any, PipelineExecutionSummary | None]:
        """Execute a stage coroutine with unified exception catching, state store update, and alerting."""
        try:
            result = await stage_coro
            details = record_builder(result)
            stage_records[stage_name] = details
            status = getattr(result, "status", None) or details.get("status")
            if status not in success_statuses:
                error_msg = getattr(result, "error", None) or details.get("error") or f"{stage_name} failed"
                self.state_repository.upsert_stage(message_id, "orchestration", "failed", str(error_msg))
                return False, result, PipelineExecutionSummary(
                    message_id=message_id,
                    status="failed",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=error_msg,
                )
            return True, result, None
        except Exception as exc:
            err = f"Unexpected error in {stage_name}: {exc}"
            self.logger.error(err, extra={"message_id": message_id})
            self.state_repository.upsert_stage(message_id, stage_name, "failed", err)
            self.state_repository.upsert_stage(message_id, "orchestration", "failed", err)
            await self._alert_failure(stage_name, message_id, err)
            stage_records[stage_name] = {"status": "failed", "error": err}
            return False, None, PipelineExecutionSummary(
                message_id=message_id,
                status="failed",
                stages=stage_records,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error=err,
            )

    async def run(
        self,
        message_id: int,
        source_path: Path | None = None,
        draft: bool = False,
        enforce_scheduler: bool = True,
        wait_for_window: bool = False,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        force: bool = False,
    ) -> PipelineExecutionSummary:
        """Execute the end-to-end pipeline for the specified message_id."""
        start_time = time.monotonic()
        stage_records: dict[str, Any] = {}

        self.logger.info(
            "Initiating pipeline execution",
            extra={"message_id": message_id, "draft": draft, "enforce_scheduler": enforce_scheduler},
        )

        # 1. Atomic Idempotency Check & Claim: prevent duplicate concurrent runs
        pub_artifact = self.storage_root / "publish" / f"{message_id}.json"
        if not force and pub_artifact.exists():
            pub_stage = self.state_repository.fetch_stage(message_id, "publish")
            if pub_stage and pub_stage["status"] == "completed":
                self.logger.info(
                    "Message execution claim skipped: already published",
                    extra={"message_id": message_id, "status": "skipped_already_published"},
                )
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="skipped_already_published",
                    stages={"publish": pub_stage},
                    duration_seconds=round(time.monotonic() - start_time, 3),
                )

        claimed, claim_reason = self.state_repository.claim_execution(
            message_id, stage="orchestration", force=force
        )
        if not claimed:
            pub_stage = self.state_repository.fetch_stage(message_id, "publish")
            status_label = (
                "skipped_already_published"
                if claim_reason in ("already_completed", "already_published")
                else "skipped_active_execution"
            )
            self.logger.info(
                "Message execution claim skipped",
                extra={"message_id": message_id, "reason": claim_reason, "status": status_label},
            )
            return PipelineExecutionSummary(
                message_id=message_id,
                status=status_label,
                stages={"publish": pub_stage} if pub_stage else {},
                duration_seconds=round(time.monotonic() - start_time, 3),
            )

        # 2. Stage: Audio Extraction
        ok, _, fail_summary = await self._run_stage(
            "audio",
            message_id,
            self.audio_service.extract(message_id, source_path=source_path),
            stage_records,
            lambda r: {
                "status": r.status,
                "storage_path": str(r.storage_path) if r.storage_path else None,
                "error": r.error,
            },
            start_time,
        )
        if not ok:
            return fail_summary  # type: ignore[return-value]

        # 3. Stage: Verse Recognition
        ok, _, fail_summary = await self._run_stage(
            "recognition",
            message_id,
            self.recognition_service.recognize(message_id),
            stage_records,
            lambda r: {
                "status": r.status,
                "storage_path": str(r.storage_path) if r.storage_path else None,
                "error": r.error,
            },
            start_time,
        )
        if not ok:
            return fail_summary  # type: ignore[return-value]

        # 4. Stage: Forced Alignment
        ok, _, fail_summary = await self._run_stage(
            "alignment",
            message_id,
            self.alignment_service.align(message_id),
            stage_records,
            lambda r: {
                "status": r.status,
                "storage_path": str(r.storage_path) if r.storage_path else None,
                "coverage": r.coverage,
                "error": r.error,
            },
            start_time,
        )
        if not ok:
            return fail_summary  # type: ignore[return-value]

        # 5. Stage: QA Gate Verification
        ok, qa_result, fail_summary = await self._run_stage(
            "qa_gate",
            message_id,
            self.qa_gate_service.evaluate(message_id),
            stage_records,
            lambda r: {
                "status": r.status,
                "approved": r.approved,
                "match_confidence": r.match_confidence,
                "alignment_coverage": r.alignment_coverage,
                "reasons": r.reasons,
                "error": r.error,
            },
            start_time,
            success_statuses=("approved",),
        )
        if not ok:
            if qa_result is not None and not getattr(qa_result, "approved", True):
                self.logger.warning(
                    "QA gate held message for manual review; halting pipeline",
                    extra={"message_id": message_id, "reasons": qa_result.reasons},
                )
                self.state_repository.upsert_stage(message_id, "orchestration", "held_for_review")
                return PipelineExecutionSummary(
                    message_id=message_id,
                    status="held_for_review",
                    stages=stage_records,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error=qa_result.error or "; ".join(qa_result.reasons),
                )
            return fail_summary  # type: ignore[return-value]

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

            # Atomic rate-limit slot reservation prevents concurrent double-publishing
            reserved, wait_seconds, reason = self.state_repository.reserve_publish_slot(
                message_id, min_interval_seconds=self.scheduler.min_interval_seconds
            )
            if not reserved:
                stage_records["scheduler"] = {
                    "can_post": False,
                    "wait_seconds": wait_seconds,
                    "reason": reason,
                }
                if wait_for_window:
                    self.logger.info(
                        "Rate limit reservation delay required",
                        extra={"wait_seconds": wait_seconds, "reason": reason},
                    )
                    sleep_coro = sleep_fn or asyncio.sleep
                    await sleep_coro(wait_seconds)
                    reserved, wait_seconds, reason = self.state_repository.reserve_publish_slot(
                        message_id, min_interval_seconds=self.scheduler.min_interval_seconds
                    )
                    if not reserved:
                        self.state_repository.upsert_stage(message_id, "publish", "scheduled", reason)
                        return PipelineExecutionSummary(
                            message_id=message_id,
                            status="scheduled",
                            stages=stage_records,
                            duration_seconds=round(time.monotonic() - start_time, 3),
                            error=reason,
                        )
                else:
                    self.logger.info(
                        "Rate limit reservation conflict; pausing before publishing",
                        extra={"message_id": message_id, "reason": reason},
                    )
                    self.state_repository.upsert_stage(message_id, "publish", "scheduled", reason)
                    return PipelineExecutionSummary(
                        message_id=message_id,
                        status="scheduled",
                        stages=stage_records,
                        duration_seconds=round(time.monotonic() - start_time, 3),
                        error=reason,
                    )


        # 7. Stage: Video Render
        ok, _, fail_summary = await self._run_stage(
            "render",
            message_id,
            self.render_service.render(message_id),
            stage_records,
            lambda r: {
                "status": r.status,
                "output_path": str(r.output_path) if r.output_path else None,
                "duration_seconds": r.duration_seconds,
                "error": r.error,
            },
            start_time,
        )
        if not ok:
            return fail_summary  # type: ignore[return-value]

        # 8. Stage: Multi-Platform Publishing
        ok, publish_result, fail_summary = await self._run_stage(
            "publish",
            message_id,
            self.publish_service.publish(message_id, draft_mode=draft),
            stage_records,
            lambda r: {
                "status": r.status,
                "artifact_path": str(r.artifact_path) if r.artifact_path else None,
                "error": r.error,
            },
            start_time,
            success_statuses=("completed", "partial", "skipped_already_published"),
        )
        if not ok:
            return fail_summary  # type: ignore[return-value]

        final_status = (
            "completed"
            if publish_result.status in ("completed", "partial")
            else publish_result.status
        )
        self.state_repository.upsert_stage(message_id, "orchestration", final_status)
        return PipelineExecutionSummary(
            message_id=message_id,
            status=final_status,
            stages=stage_records,
            duration_seconds=round(time.monotonic() - start_time, 3),
            error=publish_result.error,
        )

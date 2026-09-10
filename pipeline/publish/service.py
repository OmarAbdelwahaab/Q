"""Multi-platform publishing service with idempotency enforcement and state tracking."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pipeline.alerts import AlertService
from pipeline.logging import get_logger
from pipeline.publish.client import (
    PlatformPublishResult,
    PublishClient,
    PublishRequest,
    PublishResponse,
)
from pipeline.publish.templating import CaptionTemplater


class PipelineStateStore(Protocol):
    """Protocol for state repository operations used by the publish service."""

    def upsert_stage(
        self, message_id: int, stage: str, status: str, error: str | None = None
    ) -> None: ...

    def fetch_stage(self, message_id: int, stage: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class PublishExecutionResult:
    """Final result of a publishing stage run."""

    message_id: int
    status: str  # "completed", "partial", "failed", "skipped_already_published"
    artifact_path: Path | None = None
    response: PublishResponse | None = None
    error: str | None = None


class PublishService:
    """Orchestrates multi-platform publishing with idempotency, captioning, and alerting."""

    def __init__(
        self,
        storage_root: Path,
        state_repository: PipelineStateStore,
        alert_service: AlertService,
        client: PublishClient,
        templater: CaptionTemplater | None = None,
        default_platforms: tuple[str, ...] = (
            "tiktok",
            "instagram",
            "youtube",
            "facebook",
            "x",
            "telegram",
        ),
        default_draft_mode: bool = False,
        branding_handle: str | None = None,
        stage_name: str = "publish",
    ) -> None:
        self.storage_root = storage_root
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.client = client
        self.templater = templater or CaptionTemplater(branding_handle=branding_handle)
        self.default_platforms = default_platforms
        self.default_draft_mode = default_draft_mode
        self.branding_handle = branding_handle
        self.stage_name = stage_name
        self.logger = get_logger(__name__, service="publish")

    async def trigger_publish(self, message_id: int) -> PublishExecutionResult:
        """Entrypoint for pipeline orchestration triggers."""
        return await self.publish(message_id)

    async def publish(
        self,
        message_id: int,
        video_path: Path | None = None,
        match_path: Path | None = None,
        platforms: tuple[str, ...] | None = None,
        draft_mode: bool | None = None,
    ) -> PublishExecutionResult:
        """Execute Phase 7 multi-platform publishing for a specified message_id."""
        target_platforms = platforms if platforms is not None else self.default_platforms
        target_draft = draft_mode if draft_mode is not None else self.default_draft_mode
        publish_artifact = self.storage_root / "publish" / f"{message_id}.json"

        # 1. Idempotency check: verify if already published
        existing_stage = self.state_repository.fetch_stage(message_id, self.stage_name)
        if (
            existing_stage
            and existing_stage.get("status") == "completed"
            and publish_artifact.is_file()
        ):
            self.logger.info(
                "Message already published; skipping execution for idempotency",
                extra={
                    "message_id": message_id,
                    "artifact": str(publish_artifact),
                },
            )
            return PublishExecutionResult(
                message_id=message_id,
                status="skipped_already_published",
                artifact_path=publish_artifact,
            )

        # 2. Mark stage as processing
        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")
        self.logger.info(
            "Starting multi-platform publishing",
            extra={
                "message_id": message_id,
                "platforms": list(target_platforms),
                "draft_mode": target_draft,
            },
        )

        # 3. Resolve input video
        resolved_video = video_path or (self.storage_root / "render" / f"{message_id}.mp4")
        if not resolved_video.is_file():
            error = f"Rendered video missing for publishing: {resolved_video}"
            return await self._fail_stage(message_id, error)

        # 4. Resolve match metadata for captions
        resolved_match = match_path or (self.storage_root / "match" / f"{message_id}.json")
        if not resolved_match.is_file():
            error = f"Match metadata missing for publishing: {resolved_match}"
            return await self._fail_stage(message_id, error)

        try:
            match_data = json.loads(resolved_match.read_text(encoding="utf-8"))
            surah = int(match_data["surah"])
            ayah_start = int(match_data["ayah_start"])
            ayah_end = int(match_data["ayah_end"])
            canonical_text = str(match_data["canonical_text"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            error = f"Invalid match artifact {resolved_match}: {exc}"
            return await self._fail_stage(message_id, error)

        # 5. Build caption
        caption = self.templater.build_caption(
            surah=surah,
            ayah_start=ayah_start,
            ayah_end=ayah_end,
            canonical_text=canonical_text,
            branding_handle=self.branding_handle,
        )

        # 6. Execute multi-platform publishing
        request = PublishRequest(
            message_id=message_id,
            video_path=resolved_video,
            caption=caption,
            platforms=target_platforms,
            draft_mode=target_draft,
            title=self.templater.format_surah_reference(surah, ayah_start, ayah_end),
            tags=self.templater.default_hashtags,
        )

        try:
            response = await self.client.publish(request)
        except Exception as exc:
            error = f"Unexpected client error during publishing: {exc}"
            return await self._fail_stage(message_id, error)

        # 7. Evaluate response and persist artifact
        if response.overall_status in ("completed", "partial"):
            publish_data = {
                "message_id": message_id,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "draft_mode": target_draft,
                "overall_status": response.overall_status,
                "caption": caption,
                "platforms": {
                    p: {
                        "status": res.status,
                        "post_id": res.post_id,
                        "url": res.url,
                        "error": res.error,
                    }
                    for p, res in response.results.items()
                },
            }

            self._write_artifact(publish_artifact, publish_data)

            stage_error: str | None = None
            if response.overall_status == "partial":
                failures = [
                    f"{p}: {r.error}"
                    for p, r in response.results.items()
                    if r.status == "failed"
                ]
                stage_error = f"Partial publishing failures: {'; '.join(failures)}"
                await self.alert_service.send_publish_failure(
                    f"Telegram message {message_id} partially published: {stage_error}"
                )

            self.state_repository.upsert_stage(
                message_id, self.stage_name, "completed", stage_error
            )
            self.logger.info(
                "Publishing stage finished",
                extra={
                    "message_id": message_id,
                    "overall_status": response.overall_status,
                    "artifact": str(publish_artifact),
                },
            )
            return PublishExecutionResult(
                message_id=message_id,
                status=response.overall_status,
                artifact_path=publish_artifact,
                response=response,
                error=stage_error,
            )

        # Overall failure
        err_msg = response.error or "All target platforms failed to publish"
        return await self._fail_stage(message_id, err_msg, response=response)

    def _write_artifact(self, destination: Path, data: dict[str, Any]) -> None:
        """Atomically persist publication artifact JSON."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".partial.json")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(destination)

    async def _fail_stage(
        self,
        message_id: int,
        error: str,
        response: PublishResponse | None = None,
    ) -> PublishExecutionResult:
        """Record stage failure in repository, emit alert, and return result."""
        self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
        self.logger.error(
            "Publish stage failed",
            extra={"message_id": message_id, "error": error},
        )
        await self.alert_service.send_publish_failure(
            f"Publishing failed for Telegram message {message_id}: {error}"
        )
        return PublishExecutionResult(
            message_id=message_id,
            status="failed",
            artifact_path=None,
            response=response,
            error=error,
        )

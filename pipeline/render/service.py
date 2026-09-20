"""Phase 6 orchestration: RenderService composing background, audio, and karaoke subtitles into MP4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.logging import get_logger
from pipeline.render.background import BackgroundAssetPool
from pipeline.render.ffmpeg import FFmpegRenderer, RenderError
from pipeline.render.subtitles import (
    KaraokeSubtitleGenerator,
    get_font_family_name_from_ttf,
)


class StateRepository(Protocol):
    def upsert_stage(
        self, message_id: int, stage: str, status: str, error: str | None = None
    ) -> None: ...


class AlertService(Protocol):
    async def send_render_failure(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RenderResult:
    message_id: int
    status: str  # "completed" or "failed"
    output_path: Path | None = None
    duration_seconds: float | None = None
    error: str | None = None


class RenderService:
    """Service coordinating background selection, ASS subtitle generation, and FFmpeg video rendering."""

    stage_name = "render"

    def __init__(
        self,
        storage_root: Path,
        state_repository: StateRepository,
        alert_service: AlertService,
        background_pool: BackgroundAssetPool,
        subtitle_generator: KaraokeSubtitleGenerator | None = None,
        renderer: FFmpegRenderer | None = None,
        branding_logo_path: Path | None = None,
        branding_handle: str | None = None,
        branding_font_path: Path | None = None,
        branding_position: str = "top_right",
        max_duration_seconds: float | None = None,
    ) -> None:
        self.storage_root = storage_root
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.background_pool = background_pool
        self.branding_font_path = branding_font_path

        if subtitle_generator is not None:
            self.subtitle_generator = subtitle_generator
        else:
            font_name = "Traditional Arabic"
            if branding_font_path and branding_font_path.is_file():
                font_name = get_font_family_name_from_ttf(branding_font_path)
            self.subtitle_generator = KaraokeSubtitleGenerator(
                font_name=font_name, font_path=branding_font_path
            )

        self.renderer = renderer or FFmpegRenderer()
        self.branding_logo_path = branding_logo_path
        self.branding_handle = branding_handle
        self.branding_position = branding_position
        self.max_duration_seconds = max_duration_seconds
        self.logger = get_logger(__name__, service="render")


    async def trigger_render(self, message_id: int) -> None:
        """RenderTrigger protocol compliance for integration with QAGateService."""
        await self.render(message_id)

    async def render(
        self,
        message_id: int,
        audio_path: Path | None = None,
        align_path: Path | None = None,
        match_path: Path | None = None,
        output_path: Path | None = None,
    ) -> RenderResult:
        """Execute Phase 6 render pipeline for a specified message_id."""
        self.state_repository.upsert_stage(message_id, self.stage_name, "processing")
        self.logger.info("Starting video render pipeline", extra={"message_id": message_id})

        # 1. Resolve inputs
        resolved_audio = audio_path or self._find_audio_file(message_id)
        if not resolved_audio or not resolved_audio.is_file():
            error = f"Audio input missing for message {message_id}: searched audio/ and raw/"
            return await self._fail_stage(message_id, error)

        resolved_align = align_path or self.storage_root / "align" / f"{message_id}.json"
        if not resolved_align.is_file():
            error = f"Alignment artifact missing: {resolved_align}"
            return await self._fail_stage(message_id, error)

        resolved_match = match_path or self.storage_root / "match" / f"{message_id}.json"
        surah: int | None = None
        ayah_start: int | None = None
        ayah_end: int | None = None

        if resolved_match.is_file():
            try:
                match_data = json.loads(resolved_match.read_text(encoding="utf-8"))
                surah = match_data.get("surah")
                ayah_start = match_data.get("ayah_start")
                ayah_end = match_data.get("ayah_end")
            except Exception as exc:
                self.logger.warning(
                    "Failed to parse match metadata, proceeding without surah header",
                    extra={"message_id": message_id, "error": str(exc)},
                )

        # 2. Parse alignment data
        try:
            align_data = json.loads(resolved_align.read_text(encoding="utf-8"))
            words = align_data.get("words", [])
            if not isinstance(words, list) or not words:
                error = f"Alignment artifact contains no words for message {message_id}"
                return await self._fail_stage(message_id, error)
        except Exception as exc:
            error = f"Invalid alignment JSON: {exc}"
            return await self._fail_stage(message_id, error)

        # 3. Select background asset
        selected_bg = self.background_pool.select(surah=surah, message_id=message_id)
        if selected_bg:
            self.logger.info("Selected background asset", extra={"asset": str(selected_bg), "message_id": message_id})
        else:
            self.logger.warning(
                "No background assets found in pool; using procedural dark canvas fallback",
                extra={"message_id": message_id},
            )

        # 4. Generate ASS subtitles
        render_dir = self.storage_root / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        ass_path = render_dir / f"{message_id}.ass"

        try:
            self.subtitle_generator.write_ass_file(
                output_path=ass_path,
                words=words,
                surah=surah,
                ayah_start=ayah_start,
                ayah_end=ayah_end,
            )
        except Exception as exc:
            error = f"Failed to generate karaoke subtitles: {exc}"
            return await self._fail_stage(message_id, error)

        # 5. Execute FFmpeg rendering
        target_output = output_path or render_dir / f"{message_id}.mp4"
        fonts_dir = (
            self.branding_font_path.parent
            if (self.branding_font_path and self.branding_font_path.is_file())
            else None
        )
        try:
            rendered_file = self.renderer.render(
                audio_path=resolved_audio,
                ass_path=ass_path,
                output_path=target_output,
                background_path=selected_bg,
                branding_logo_path=self.branding_logo_path,
                branding_handle=self.branding_handle,
                branding_position=self.branding_position,
                max_duration_seconds=self.max_duration_seconds,
                fonts_dir=fonts_dir,
            )
            probe = self.renderer.probe_media(rendered_file)

        except RenderError as exc:
            error = f"Rendering error: {exc}"
            return await self._fail_stage(message_id, error)
        except Exception as exc:
            error = f"Unexpected error during render: {exc}"
            return await self._fail_stage(message_id, error)

        # 6. Mark completed
        self.state_repository.upsert_stage(message_id, self.stage_name, "completed")
        self.logger.info(
            "Video render completed successfully",
            extra={
                "message_id": message_id,
                "output": str(rendered_file),
                "duration_seconds": probe.duration_seconds,
                "resolution": f"{probe.width}x{probe.height}",
            },
        )
        return RenderResult(
            message_id=message_id,
            status="completed",
            output_path=rendered_file,
            duration_seconds=probe.duration_seconds,
        )

    def _find_audio_file(self, message_id: int) -> Path | None:
        """Find audio input: check audio/{id}.wav first, then raw/{id}.mp4."""
        wav_path = self.storage_root / "audio" / f"{message_id}.wav"
        if wav_path.is_file():
            return wav_path

        mp4_path = self.storage_root / "raw" / f"{message_id}.mp4"
        if mp4_path.is_file():
            return mp4_path

        return None

    async def _fail_stage(self, message_id: int, error: str) -> RenderResult:
        self.state_repository.upsert_stage(message_id, self.stage_name, "failed", error)
        self.logger.error("Render stage failed", extra={"message_id": message_id, "error": error})
        await self.alert_service.send_render_failure(
            f"Render pipeline failed for Telegram message {message_id}: {error}"
        )
        return RenderResult(message_id, "failed", None, error=error)

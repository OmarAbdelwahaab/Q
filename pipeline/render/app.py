"""CLI entrypoint for Phase 6 video rendering."""

from __future__ import annotations

import argparse
import asyncio
import sys

from pipeline.alerts import CompositeAlertService
from pipeline.config import RenderSettings
from pipeline.logging import configure_logging, get_logger
from pipeline.render.background import BackgroundAssetPool
from pipeline.render.ffmpeg import FFmpegRenderer
from pipeline.render.service import RenderService
from pipeline.render.subtitles import KaraokeSubtitleGenerator
from pipeline.state.repository import PipelineStateRepository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render stylized Quran recitation video edit.")
    parser.add_argument("message_id", type=int, help="Telegram message ID to render")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = RenderSettings.from_env()
    configure_logging(level=settings.log_level)
    logger = get_logger(__name__, service="render_app")

    state_repo = PipelineStateRepository(settings.state_db_path)
    alert_service = CompositeAlertService(
        webhook_url=settings.alert_webhook_url,
        telegram_bot_token=settings.alert_telegram_bot_token,
        telegram_chat_id=settings.alert_telegram_chat_id,
    )
    background_pool = BackgroundAssetPool(
        asset_dir=settings.background_asset_path,
        strategy=settings.background_selection_strategy,
    )
    subtitle_generator = KaraokeSubtitleGenerator()
    renderer = FFmpegRenderer(
        ffmpeg_binary=settings.ffmpeg_binary,
        ffprobe_binary=settings.ffprobe_binary,
    )

    service = RenderService(
        storage_root=settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        background_pool=background_pool,
        subtitle_generator=subtitle_generator,
        renderer=renderer,
        branding_logo_path=settings.branding_logo_path,
        branding_handle=settings.branding_handle,
        branding_position=settings.branding_position,
        max_duration_seconds=settings.max_duration_seconds,
    )

    result = await service.render(args.message_id)
    if result.status == "completed":
        logger.info(
            "Video render completed",
            extra={"message_id": args.message_id, "output": str(result.output_path)},
        )
        return 0

    logger.error(
        "Video render failed",
        extra={"message_id": args.message_id, "error": result.error},
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

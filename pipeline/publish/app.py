"""CLI entrypoint for Phase 7 multi-platform video publishing."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pipeline.alerts import CompositeAlertService
from pipeline.config import PublishSettings
from pipeline.logging import configure_logging, get_logger
from pipeline.publish.client import MultiPlatformPublishClient, StubPublishClient
from pipeline.publish.service import PublishService
from pipeline.publish.templating import CaptionTemplater
from pipeline.state.repository import PipelineStateRepository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for publishing CLI."""
    parser = argparse.ArgumentParser(
        description="Publish rendered Quran edit video across social platforms."
    )
    parser.add_argument("message_id", type=int, help="Telegram message ID to publish")
    parser.add_argument(
        "--draft",
        action="store_true",
        default=False,
        help="Force draft / sandbox mode (do not publish live)",
    )
    parser.add_argument(
        "--platforms",
        type=str,
        default=None,
        help="Comma-separated target platforms (e.g. tiktok,instagram,youtube)",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=None,
        help="Explicit path to rendered video MP4",
    )
    parser.add_argument(
        "--match",
        type=Path,
        default=None,
        help="Explicit path to match JSON metadata",
    )
    parser.add_argument(
        "--media-url",
        type=str,
        default=None,
        help="Explicit public media URL (overrides uploader)",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """Run publishing service with configuration and arguments."""
    args = parse_args(argv)
    settings = PublishSettings.from_env()
    configure_logging(level=settings.log_level)
    logger = get_logger(__name__, service="publish_app")

    state_repo = PipelineStateRepository(settings.state_db_path)
    alert_service = CompositeAlertService(
        webhook_url=settings.alert_webhook_url,
        telegram_bot_token=settings.alert_telegram_bot_token,
        telegram_chat_id=settings.alert_telegram_chat_id,
    )

    templater = CaptionTemplater(
        default_template=settings.publish_caption_template,
        default_hashtags=settings.publish_default_hashtags,
        branding_handle=settings.branding_handle,
    )

    # Determine media uploader
    from pipeline.publish.uploader import (
        PublicUrlMediaUploader,
        S3MediaUploader,
        StubMediaUploader,
    )

    if settings.public_media_base_url:
        media_uploader = PublicUrlMediaUploader(settings.public_media_base_url)
    elif settings.s3_endpoint:
        media_uploader = S3MediaUploader(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket_render,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            public_base_url=settings.public_media_base_url,
        )
    else:
        media_uploader = StubMediaUploader()

    # Determine publishing client: live Ayrshare HTTP or stub
    if settings.publish_api_key:
        client = MultiPlatformPublishClient(
            api_base_url=settings.publish_api_base_url,
            api_key=settings.publish_api_key,
        )
    else:
        logger.info(
            "PUBLISH_API_KEY not set; utilizing StubPublishClient for publishing run"
        )
        client = StubPublishClient()

    platforms = (
        tuple(p.strip().lower() for p in args.platforms.split(",") if p.strip())
        if args.platforms
        else settings.publish_platforms
    )
    draft_mode = args.draft or settings.publish_draft_mode
    media_urls = (args.media_url,) if args.media_url else None

    service = PublishService(
        storage_root=settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        client=client,
        media_uploader=media_uploader,
        templater=templater,
        default_platforms=platforms,
        default_draft_mode=draft_mode,
        branding_handle=settings.branding_handle,
    )

    result = await service.publish(
        message_id=args.message_id,
        video_path=args.video,
        match_path=args.match,
        platforms=platforms,
        draft_mode=draft_mode,
        media_urls=media_urls,
    )

    if result.status in ("completed", "skipped_already_published"):
        logger.info(
            "Publishing completed successfully",
            extra={
                "message_id": args.message_id,
                "status": result.status,
                "artifact": str(result.artifact_path) if result.artifact_path else None,
            },
        )
        return 0

    if result.status == "partial":
        logger.warning(
            "Publishing completed with partial platform errors",
            extra={
                "message_id": args.message_id,
                "error": result.error,
            },
        )
        return 0

    logger.error(
        "Publishing failed",
        extra={
            "message_id": args.message_id,
            "error": result.error,
        },
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

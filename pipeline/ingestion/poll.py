"""Batch polling runner for Telegram channel videos (designed for scheduled cron / GitHub Actions)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from pipeline.config import IngestionSettings
from pipeline.ingestion.app import build_ingestion_listener
from pipeline.logging import configure_logging, get_logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Telegram channel for recent videos and process them."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of recent channel messages to check (default: 10)",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=2,
        help="Maximum number of new videos to download and process per run (default: 2)",
    )
    return parser.parse_args(argv)


async def poll_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = IngestionSettings.from_env()
    configure_logging(service_name="ingestion_poll", level=settings.log_level)
    logger = get_logger(__name__, service="ingestion_poll")

    logger.info(
        "Starting Telegram channel batch poll",
        extra={
            "channel_id": settings.telegram_channel_id,
            "limit": args.limit,
            "max_downloads": args.max_downloads,
        },
    )

    if settings.auto_orchestrate:
        import os
        asr_key = os.environ.get("ASR_API_KEY", "").strip()
        asr_url = os.environ.get("ASR_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
        if not asr_key and not any(h in asr_url for h in ("localhost", "127.0.0.1", "testserver")):
            logger.error(
                "ASR_API_KEY is missing or empty while AUTO_ORCHESTRATE is enabled. "
                "Configure ASR_API_KEY in your environment or GitHub Secrets to enable recognition."
            )
            return 1

    listener = build_ingestion_listener(settings)
    try:
        results = await listener.poll_recent_videos(
            limit=args.limit,
            max_downloads=args.max_downloads,
        )
        logger.info(
            "Batch poll completed",
            extra={"processed_count": len(results)},
        )
        return 0
    except Exception as exc:
        logger.exception(
            "Batch poll encountered an unhandled error: %s",
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        print(f"::error title=Batch Poll Failed::{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(poll_main()))

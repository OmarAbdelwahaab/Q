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
    return parser.parse_args(argv)


async def poll_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = IngestionSettings.from_env()
    configure_logging(service_name="ingestion_poll", level=settings.log_level)
    logger = get_logger(__name__, service="ingestion_poll")

    logger.info(
        "Starting Telegram channel batch poll",
        extra={"channel_id": settings.telegram_channel_id, "limit": args.limit},
    )

    listener = build_ingestion_listener(settings)
    try:
        results = await listener.poll_recent_videos(limit=args.limit)
        logger.info(
            "Batch poll completed",
            extra={"processed_count": len(results)},
        )
        return 0
    except Exception as exc:
        logger.error(
            "Batch poll encountered an unhandled error",
            extra={"error": str(exc)},
        )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(poll_main()))

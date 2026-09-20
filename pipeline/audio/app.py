"""CLI entrypoint for extracting an ingested video's audio."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pipeline.alerts import CompositeAlertService
from pipeline.audio.ffmpeg import FFmpegAudioExtractor
from pipeline.audio.service import AudioExtractionService
from pipeline.config import AudioSettings
from pipeline.logging import configure_logging
from pipeline.state.repository import PipelineStateRepository


def build_audio_service(settings: AudioSettings) -> AudioExtractionService:
    return AudioExtractionService(
        storage_root=settings.storage_root,
        state_repository=PipelineStateRepository(settings.state_db_path),
        alert_service=CompositeAlertService(
            webhook_url=settings.alert_webhook_url,
            telegram_bot_token=settings.alert_telegram_bot_token,
            telegram_chat_id=settings.alert_telegram_chat_id,
        ),
        extractor=FFmpegAudioExtractor(
            ffmpeg_binary=settings.ffmpeg_binary,
            ffprobe_binary=settings.ffprobe_binary,
            minimum_duration_seconds=settings.minimum_duration_seconds,
        ),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Extract 16 kHz mono WAV from an ingested video.")
    parser.add_argument("message_id", type=int)
    parser.add_argument("--input", type=Path, help="Override raw/{message_id}.mp4 source path.")
    args = parser.parse_args()

    settings = AudioSettings.from_env()
    configure_logging(service_name="audio", level=settings.log_level)
    result = await build_audio_service(settings).extract(args.message_id, args.input)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""CLI entrypoint for Phase 3 recognition."""

from __future__ import annotations

import argparse
import asyncio

from pipeline.alerts import CompositeAlertService
from pipeline.config import RecognitionSettings
from pipeline.logging import configure_logging
from pipeline.recognition.asr import WhisperTranscriber
from pipeline.recognition.corpus import QuranCorpus
from pipeline.recognition.matcher import QuranMatcher
from pipeline.recognition.service import RecognitionService
from pipeline.state.repository import PipelineStateRepository


def build_recognition_service(settings: RecognitionSettings) -> RecognitionService:
    corpus = QuranCorpus.load_or_fetch(settings.corpus_cache_path, settings.corpus_api_base_url)
    return RecognitionService(
        settings.storage_root,
        PipelineStateRepository(settings.state_db_path),
        CompositeAlertService(settings.alert_webhook_url, settings.alert_telegram_bot_token, settings.alert_telegram_chat_id),
        WhisperTranscriber(settings.asr_api_url, settings.asr_api_key, settings.asr_model_name),
        QuranMatcher(corpus),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize canonical Quran ayat from an extracted WAV.")
    parser.add_argument("message_id", type=int)
    args = parser.parse_args()
    settings = RecognitionSettings.from_env()
    configure_logging(service_name="recognition", level=settings.log_level)
    result = await build_recognition_service(settings).recognize(args.message_id)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

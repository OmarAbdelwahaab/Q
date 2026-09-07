"""CLI entrypoint for Phase 4 word-level alignment."""

from __future__ import annotations

import argparse
import asyncio

from pipeline.alerts import CompositeAlertService
from pipeline.alignment.ctc import CtcForcedAligner
from pipeline.alignment.service import AlignmentService
from pipeline.config import AlignmentSettings
from pipeline.logging import configure_logging
from pipeline.state.repository import PipelineStateRepository


def build_alignment_service(settings: AlignmentSettings) -> AlignmentService:
    return AlignmentService(settings.storage_root, PipelineStateRepository(settings.state_db_path), CompositeAlertService(settings.alert_webhook_url, settings.alert_telegram_bot_token, settings.alert_telegram_chat_id), CtcForcedAligner(settings.aligner_binary, settings.alignment_model, settings.alignment_device, settings.alignment_batch_size))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Align canonical Quran words to extracted audio.")
    parser.add_argument("message_id", type=int)
    args = parser.parse_args()
    settings = AlignmentSettings.from_env()
    configure_logging(service_name="alignment", level=settings.log_level)
    result = await build_alignment_service(settings).align(args.message_id)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

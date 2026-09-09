"""CLI entrypoint for Phase 5 QA gate verification."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pipeline.alerts import CompositeAlertService
from pipeline.config import QAGateSettings
from pipeline.logging import configure_logging
from pipeline.qa_gate.service import QAGateService
from pipeline.state.repository import PipelineStateRepository


def build_qa_gate_service(settings: QAGateSettings) -> QAGateService:
    return QAGateService(
        storage_root=settings.storage_root,
        state_repository=PipelineStateRepository(settings.state_db_path),
        alert_service=CompositeAlertService(
            webhook_url=settings.alert_webhook_url,
            telegram_bot_token=settings.alert_telegram_bot_token,
            telegram_chat_id=settings.alert_telegram_chat_id,
        ),
        match_confidence_threshold=settings.match_confidence_threshold,
        alignment_coverage_threshold=settings.alignment_coverage_threshold,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate recognition and alignment confidence against QA thresholds."
    )
    parser.add_argument("message_id", type=int, help="Telegram message ID to evaluate")
    parser.add_argument("--match", type=Path, help="Override match/{message_id}.json path")
    parser.add_argument("--align", type=Path, help="Override align/{message_id}.json path")
    args = parser.parse_args()

    settings = QAGateSettings.from_env()
    configure_logging(service_name="qa_gate", level=settings.log_level)
    result = await build_qa_gate_service(settings).evaluate(
        args.message_id, match_path=args.match, align_path=args.align
    )
    return 0 if result.status == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

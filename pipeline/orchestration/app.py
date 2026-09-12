"""CLI entrypoint for running the end-to-end video pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pipeline.alerts import CompositeAlertService
from pipeline.alignment.ctc import CtcForcedAligner
from pipeline.alignment.service import AlignmentService
from pipeline.audio.ffmpeg import FFmpegAudioExtractor
from pipeline.audio.service import AudioExtractionService
from pipeline.config import (
    AlignmentSettings,
    AudioSettings,
    OrchestrationSettings,
    PublishSettings,
    QAGateSettings,
    RecognitionSettings,
    RenderSettings,
)
from pipeline.logging import configure_logging, get_logger
from pipeline.orchestration.runner import PipelineOrchestrator
from pipeline.orchestration.scheduler import PostingWindowScheduler
from pipeline.publish.client import MultiPlatformPublishClient, StubPublishClient
from pipeline.publish.service import PublishService
from pipeline.publish.templating import CaptionTemplater
from pipeline.publish.uploader import (
    PublicUrlMediaUploader,
    S3MediaUploader,
    StubMediaUploader,
)
from pipeline.qa_gate.service import QAGateService
from pipeline.recognition.asr import WhisperTranscriber
from pipeline.recognition.corpus import QuranCorpus
from pipeline.recognition.matcher import QuranMatcher
from pipeline.recognition.service import RecognitionService
from pipeline.render.background import BackgroundAssetPool
from pipeline.render.ffmpeg import FFmpegRenderer
from pipeline.render.service import RenderService
from pipeline.render.subtitles import KaraokeSubtitleGenerator
from pipeline.state.repository import PipelineStateRepository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for orchestrator CLI."""
    parser = argparse.ArgumentParser(
        description="Run end-to-end Quran video repurposing pipeline."
    )
    parser.add_argument("message_id", type=int, help="Telegram message ID to process")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Explicit path to raw input MP4 file",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        default=False,
        help="Publish in draft / sandbox mode (safe for testing)",
    )
    parser.add_argument(
        "--skip-scheduler",
        action="store_true",
        default=False,
        help="Bypass posting-window and rate-limit scheduling",
    )
    parser.add_argument(
        "--wait-for-window",
        action="store_true",
        default=False,
        help="Wait asynchronously until posting window opens if currently closed",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print machine-readable JSON execution summary",
    )
    return parser.parse_args(argv)


def build_orchestrator(
    orch_settings: OrchestrationSettings,
) -> tuple[PipelineOrchestrator, PipelineStateRepository]:
    """Factory creating fully wired orchestrator from environment configurations."""
    # State Repository (PostgreSQL if URL is provided, else SQLite per ADR 001)
    if orch_settings.state_database_url:
        state_repo = PipelineStateRepository(database_url=orch_settings.state_database_url)
    else:
        state_repo = PipelineStateRepository(database_path=orch_settings.state_db_path)

    # Shared alert service
    alert_service = CompositeAlertService(
        webhook_url=orch_settings.alert_webhook_url,
        telegram_bot_token=orch_settings.alert_telegram_bot_token,
        telegram_chat_id=orch_settings.alert_telegram_chat_id,
    )

    # Audio Stage
    audio_settings = AudioSettings.from_env()
    extractor = FFmpegAudioExtractor(
        ffmpeg_binary=audio_settings.ffmpeg_binary,
        ffprobe_binary=audio_settings.ffprobe_binary,
        minimum_duration_seconds=audio_settings.minimum_duration_seconds,
    )
    audio_service = AudioExtractionService(
        storage_root=audio_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        extractor=extractor,
    )

    # Recognition Stage
    rec_settings = RecognitionSettings.from_env()
    transcriber = WhisperTranscriber(
        api_url=rec_settings.asr_api_url,
        api_key=rec_settings.asr_api_key,
        model_name=rec_settings.asr_model_name,
    )
    corpus = QuranCorpus.load_or_fetch(
        cache_path=rec_settings.corpus_cache_path,
        api_base_url=rec_settings.corpus_api_base_url,
    )
    matcher = QuranMatcher(corpus)
    recognition_service = RecognitionService(
        storage_root=rec_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        transcriber=transcriber,
        matcher=matcher,
    )

    # Alignment Stage
    align_settings = AlignmentSettings.from_env()
    aligner = CtcForcedAligner(
        binary_path=align_settings.aligner_binary,
        model_name=align_settings.alignment_model,
        device=align_settings.alignment_device,
        batch_size=align_settings.alignment_batch_size,
    )
    alignment_service = AlignmentService(
        storage_root=align_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        aligner=aligner,
    )

    # QA Gate Stage
    qa_settings = QAGateSettings.from_env()
    qa_gate_service = QAGateService(
        storage_root=qa_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        match_confidence_threshold=qa_settings.match_confidence_threshold,
        alignment_coverage_threshold=qa_settings.alignment_coverage_threshold,
    )

    # Scheduler
    scheduler = PostingWindowScheduler(
        enabled=orch_settings.posting_window_enabled,
        start_hour=orch_settings.posting_window_start_hour,
        end_hour=orch_settings.posting_window_end_hour,
        timezone_name=orch_settings.posting_window_timezone,
        min_interval_seconds=orch_settings.rate_limit_min_interval_seconds,
    )

    # Render Stage
    render_settings = RenderSettings.from_env()
    bg_pool = BackgroundAssetPool(
        render_settings.background_asset_path,
        strategy=render_settings.background_selection_strategy,
    )
    sub_gen = KaraokeSubtitleGenerator(font_path=render_settings.branding_font_path)
    renderer = FFmpegRenderer(
        ffmpeg_binary=render_settings.ffmpeg_binary,
        ffprobe_binary=render_settings.ffprobe_binary,
    )
    render_service = RenderService(
        storage_root=render_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        background_pool=bg_pool,
        subtitle_generator=sub_gen,
        renderer=renderer,
        branding_logo_path=render_settings.branding_logo_path,
        branding_handle=render_settings.branding_handle,
        branding_font_path=render_settings.branding_font_path,
        branding_position=render_settings.branding_position,
        max_duration_seconds=render_settings.max_duration_seconds,
    )

    # Publishing Stage
    pub_settings = PublishSettings.from_env()
    if pub_settings.publish_api_key and pub_settings.publish_api_base_url:
        pub_client = MultiPlatformPublishClient(
            base_url=pub_settings.publish_api_base_url,
            api_key=pub_settings.publish_api_key,
        )
    else:
        pub_client = StubPublishClient()

    if pub_settings.public_media_base_url:
        media_uploader = PublicUrlMediaUploader(pub_settings.public_media_base_url)
    elif pub_settings.s3_endpoint:
        media_uploader = S3MediaUploader(
            endpoint=pub_settings.s3_endpoint,
            bucket=pub_settings.s3_bucket_render,
            access_key=pub_settings.s3_access_key,
            secret_key=pub_settings.s3_secret_key,
        )
    else:
        media_uploader = StubMediaUploader()

    templater = CaptionTemplater(
        template=pub_settings.publish_caption_template,
        default_hashtags=pub_settings.publish_default_hashtags,
        branding_handle=pub_settings.branding_handle,
    )
    publish_service = PublishService(
        storage_root=pub_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        client=pub_client,
        media_uploader=media_uploader,
        templater=templater,
        default_platforms=pub_settings.publish_platforms,
        default_draft_mode=pub_settings.publish_draft_mode,
    )


    orchestrator = PipelineOrchestrator(
        storage_root=orch_settings.storage_root,
        state_repository=state_repo,
        alert_service=alert_service,
        audio_service=audio_service,
        recognition_service=recognition_service,
        alignment_service=alignment_service,
        qa_gate_service=qa_gate_service,
        render_service=render_service,
        publish_service=publish_service,
        scheduler=scheduler,
    )

    return orchestrator, state_repo


async def main(argv: list[str] | None = None) -> int:
    """Run pipeline orchestration CLI."""
    args = parse_args(argv)
    orch_settings = OrchestrationSettings.from_env()
    configure_logging(level=orch_settings.log_level)
    logger = get_logger(__name__, service="orchestration.cli")

    orchestrator, _ = build_orchestrator(orch_settings)

    summary = await orchestrator.run(
        message_id=args.message_id,
        source_path=args.source,
        draft=args.draft,
        enforce_scheduler=not args.skip_scheduler,
        wait_for_window=args.wait_for_window,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "message_id": summary.message_id,
                    "status": summary.status,
                    "duration_seconds": summary.duration_seconds,
                    "stages": summary.stages,
                    "error": summary.error,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        logger.info(
            "Pipeline execution complete",
            extra={
                "message_id": summary.message_id,
                "status": summary.status,
                "duration_seconds": summary.duration_seconds,
                "error": summary.error,
            },
        )

    if summary.status in ("completed", "skipped_already_published", "scheduled"):
        return 0
    elif summary.status == "held_for_review":
        return 2
    else:
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

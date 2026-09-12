"""Configuration loading for pipeline services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _read_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


@dataclass(slots=True)
class IngestionSettings:
    telegram_api_id: int | None
    telegram_api_hash: str
    telegram_bot_token: str | None
    telegram_channel_id: str
    telegram_session_name: str
    state_db_path: Path
    storage_root: Path
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    retry_attempts: int
    retry_backoff_seconds: tuple[int, ...]
    log_level: str
    log_include_source: bool

    @classmethod
    def from_env(cls) -> "IngestionSettings":
        raw_backoff = os.getenv("INGESTION_RETRY_BACKOFF_SECONDS", "1,2")
        retry_backoff = tuple(
            int(value.strip())
            for value in raw_backoff.split(",")
            if value.strip()
        ) or (1, 2)

        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        state_db_path = Path(
            os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
        ).resolve()

        return cls(
            telegram_api_id=_read_int("TELEGRAM_API_ID"),
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", ""),
            telegram_session_name=os.getenv("TELEGRAM_SESSION_NAME", "pipeline_ingestion"),
            state_db_path=state_db_path,
            storage_root=storage_root,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            retry_attempts=_read_int("INGESTION_RETRY_ATTEMPTS", 3) or 3,
            retry_backoff_seconds=retry_backoff,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_include_source=_read_bool("LOG_INCLUDE_SOURCE", False),
        )


@dataclass(slots=True)
class AudioSettings:
    """Runtime settings for the ffmpeg audio extraction stage."""

    state_db_path: Path
    storage_root: Path
    ffmpeg_binary: str
    ffprobe_binary: str
    minimum_duration_seconds: float
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "AudioSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        state_db_path = Path(
            os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
        ).resolve()
        minimum_duration = float(os.getenv("AUDIO_MIN_DURATION_SECONDS", "0.25"))
        if minimum_duration < 0:
            raise ValueError("AUDIO_MIN_DURATION_SECONDS must be zero or greater.")

        return cls(
            state_db_path=state_db_path,
            storage_root=storage_root,
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe"),
            minimum_duration_seconds=minimum_duration,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class RecognitionSettings:
    state_db_path: Path
    storage_root: Path
    asr_api_url: str
    asr_api_key: str | None
    asr_model_name: str
    corpus_cache_path: Path
    corpus_api_base_url: str
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "RecognitionSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        return cls(
            state_db_path=Path(os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))).resolve(),
            storage_root=storage_root,
            asr_api_url=os.getenv("ASR_API_URL", ""),
            asr_api_key=os.getenv("ASR_API_KEY") or None,
            asr_model_name=os.getenv("ASR_MODEL_NAME", "whisper-large-v3"),
            corpus_cache_path=Path(os.getenv("QURAN_CORPUS_CACHE_PATH", str(storage_root / "corpus" / "quran-uthmani.json"))).resolve(),
            corpus_api_base_url=os.getenv("QURAN_CORPUS_API_BASE_URL", "https://api.quran.com/api/v4/quran/verses/uthmani"),
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class AlignmentSettings:
    state_db_path: Path
    storage_root: Path
    aligner_binary: str
    alignment_model: str
    alignment_device: str
    alignment_batch_size: int
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "AlignmentSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        batch_size = _read_int("ALIGNMENT_BATCH_SIZE", 4) or 4
        if batch_size < 1:
            raise ValueError("ALIGNMENT_BATCH_SIZE must be positive.")
        return cls(
            state_db_path=Path(os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))).resolve(),
            storage_root=storage_root,
            aligner_binary=os.getenv("CTC_ALIGNER_BINARY", "ctc-forced-aligner"),
            alignment_model=os.getenv("CTC_ALIGNMENT_MODEL", "jonatasgrosman/wav2vec2-large-xlsr-53-arabic"),
            alignment_device=os.getenv("CTC_ALIGNMENT_DEVICE", "cuda"),
            alignment_batch_size=batch_size,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class QAGateSettings:
    state_db_path: Path
    storage_root: Path
    match_confidence_threshold: float
    alignment_coverage_threshold: float
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "QAGateSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        match_thresh = _read_float("QA_MATCH_CONFIDENCE_THRESHOLD", 0.90)
        align_thresh = _read_float("QA_ALIGNMENT_COVERAGE_THRESHOLD", 0.95)
        if not (0.0 <= match_thresh <= 1.0):
            raise ValueError("QA_MATCH_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0.")
        if not (0.0 <= align_thresh <= 1.0):
            raise ValueError("QA_ALIGNMENT_COVERAGE_THRESHOLD must be between 0.0 and 1.0.")
        return cls(
            state_db_path=Path(
                os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
            ).resolve(),
            storage_root=storage_root,
            match_confidence_threshold=match_thresh,
            alignment_coverage_threshold=align_thresh,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class RenderSettings:
    state_db_path: Path
    storage_root: Path
    background_asset_path: Path
    background_selection_strategy: str
    branding_logo_path: Path | None
    branding_handle: str | None
    branding_font_path: Path | None
    branding_position: str
    max_duration_seconds: float | None
    ffmpeg_binary: str
    ffprobe_binary: str
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "RenderSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        strategy = os.getenv("BACKGROUND_SELECTION_STRATEGY", "round_robin").lower()
        if strategy not in {"round_robin", "random", "keyed"}:
            raise ValueError(f"Invalid BACKGROUND_SELECTION_STRATEGY: {strategy}")

        max_dur_str = os.getenv("RENDER_MAX_DURATION_SECONDS")
        max_duration = float(max_dur_str) if max_dur_str else None

        logo_path = os.getenv("BRANDING_LOGO_PATH")
        font_path = os.getenv("BRANDING_FONT_PATH")

        return cls(
            state_db_path=Path(
                os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
            ).resolve(),
            storage_root=storage_root,
            background_asset_path=Path(
                os.getenv("BACKGROUND_ASSET_PATH", str(project_root / "pipeline" / "assets" / "backgrounds"))
            ).resolve(),
            background_selection_strategy=strategy,
            branding_logo_path=Path(logo_path).resolve() if logo_path else None,
            branding_handle=os.getenv("BRANDING_HANDLE") or None,
            branding_font_path=Path(font_path).resolve() if font_path else None,
            branding_position=os.getenv("BRANDING_POSITION", "top_right"),
            max_duration_seconds=max_duration,
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe"),
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class PublishSettings:
    """Runtime settings for the multi-platform publishing stage."""

    state_db_path: Path
    storage_root: Path
    publish_api_base_url: str
    publish_api_key: str | None
    publish_platforms: tuple[str, ...]
    publish_draft_mode: bool
    publish_caption_template: str | None
    publish_default_hashtags: tuple[str, ...]
    branding_handle: str | None
    public_media_base_url: str | None
    s3_endpoint: str | None
    s3_access_key: str | None
    s3_secret_key: str | None
    s3_bucket_render: str
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "PublishSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()

        raw_platforms = os.getenv(
            "PUBLISH_PLATFORMS",
            "tiktok,instagram,youtube,facebook,x,telegram",
        )
        platforms = tuple(
            p.strip().lower()
            for p in raw_platforms.split(",")
            if p.strip()
        )

        raw_hashtags = os.getenv(
            "PUBLISH_DEFAULT_HASHTAGS",
            "#قرآن,#تلاوة,#قرآن_كريم,#تلاوات_خاشعة,#Quran,#Islam",
        )
        hashtags = tuple(
            h.strip()
            for h in raw_hashtags.split(",")
            if h.strip()
        )

        raw_api_url = os.getenv("PUBLISH_API_BASE_URL", "https://app.ayrshare.com/api").rstrip("/")

        return cls(
            state_db_path=Path(
                os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
            ).resolve(),
            storage_root=storage_root,
            publish_api_base_url=raw_api_url,
            publish_api_key=os.getenv("PUBLISH_API_KEY") or None,
            publish_platforms=platforms,
            publish_draft_mode=_read_bool("PUBLISH_DRAFT_MODE", False),
            publish_caption_template=os.getenv("PUBLISH_CAPTION_TEMPLATE") or None,
            publish_default_hashtags=hashtags,
            branding_handle=os.getenv("BRANDING_HANDLE") or None,
            public_media_base_url=os.getenv("PUBLIC_MEDIA_BASE_URL") or None,
            s3_endpoint=os.getenv("S3_ENDPOINT") or None,
            s3_access_key=os.getenv("S3_ACCESS_KEY") or None,
            s3_secret_key=os.getenv("S3_SECRET_KEY") or None,
            s3_bucket_render=os.getenv("S3_BUCKET_RENDER", "render"),
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass(slots=True)
class OrchestrationSettings:
    """Runtime settings for the end-to-end pipeline orchestrator and scheduler."""

    state_db_path: Path
    state_database_url: str | None
    storage_root: Path
    posting_window_enabled: bool
    posting_window_start_hour: int
    posting_window_end_hour: int
    posting_window_timezone: str
    rate_limit_min_interval_seconds: int
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "OrchestrationSettings":
        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        state_db_path = Path(
            os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
        ).resolve()
        state_database_url = os.getenv("STATE_DATABASE_URL") or None

        start_hour = _read_int("POSTING_WINDOW_START_HOUR", 9)
        end_hour = _read_int("POSTING_WINDOW_END_HOUR", 23)
        if start_hour is None or not (0 <= start_hour <= 23):
            raise ValueError("POSTING_WINDOW_START_HOUR must be between 0 and 23.")
        if end_hour is None or not (0 <= end_hour <= 23):
            raise ValueError("POSTING_WINDOW_END_HOUR must be between 0 and 23.")

        min_interval = _read_int("RATE_LIMIT_MIN_INTERVAL_SECONDS", 1800)
        if min_interval is None or min_interval < 0:
            raise ValueError("RATE_LIMIT_MIN_INTERVAL_SECONDS must be zero or greater.")

        return cls(
            state_db_path=state_db_path,
            state_database_url=state_database_url,
            storage_root=storage_root,
            posting_window_enabled=_read_bool("POSTING_WINDOW_ENABLED", False),
            posting_window_start_hour=start_hour,
            posting_window_end_hour=end_hour,
            posting_window_timezone=os.getenv("POSTING_WINDOW_TIMEZONE", "UTC"),
            rate_limit_min_interval_seconds=min_interval,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )



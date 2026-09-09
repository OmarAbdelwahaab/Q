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


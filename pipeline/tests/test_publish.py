"""Comprehensive unit and integration tests for Phase 7 Multi-Platform Publishing."""

from __future__ import annotations

import asyncio
import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.alerts import CompositeAlertService
from pipeline.config import PublishSettings
from pipeline.publish.app import main as cli_main, parse_args
from pipeline.publish.client import (
    MultiPlatformPublishClient,
    PlatformPublishResult,
    PublishRequest,
    PublishResponse,
    StubPublishClient,
)
from pipeline.publish.service import PublishExecutionResult, PublishService
from pipeline.publish.templating import (
    DEFAULT_HASHTAGS,
    PLATFORM_MAX_CAPTION_LENGTHS,
    CaptionTemplater,
)
from pipeline.state.repository import PipelineStateRepository


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


class MockAlertService:
    """Mock alert recorder."""

    def __init__(self) -> None:
        self.publish_failures: list[str] = []

    async def send_publish_failure(self, message: str) -> None:
        self.publish_failures.append(message)


@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Create isolated directory structure for pipeline storage."""
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "render").mkdir(parents=True, exist_ok=True)
    (tmp_path / "match").mkdir(parents=True, exist_ok=True)
    (tmp_path / "publish").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def state_repo(tmp_path: Path) -> PipelineStateRepository:
    """Create isolated SQLite state repository."""
    db_path = tmp_path / "test_state.db"
    return PipelineStateRepository(db_path)


@pytest.fixture
def alert_service() -> MockAlertService:
    return MockAlertService()


def create_mock_artifacts(
    storage_root: Path,
    message_id: int,
    surah: int = 1,
    ayah_start: int = 1,
    ayah_end: int = 1,
    canonical_text: str = "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ",
) -> tuple[Path, Path]:
    """Create realistic dummy render MP4 and match JSON artifacts."""
    video_path = storage_root / "render" / f"{message_id}.mp4"
    video_path.write_bytes(b"dummy_h264_mp4_content")

    match_path = storage_root / "match" / f"{message_id}.json"
    match_data = {
        "surah": surah,
        "ayah_start": ayah_start,
        "ayah_end": ayah_end,
        "canonical_text": canonical_text,
        "match_confidence": 0.98,
        "recognized_text": "بسم الله الرحمن الرحيم",
    }
    match_path.write_text(json.dumps(match_data, ensure_ascii=False), encoding="utf-8")
    return video_path, match_path


# ---------------------------------------------------------------------------
# 1. CaptionTemplater Tests
# ---------------------------------------------------------------------------


class TestCaptionTemplater:
    def test_single_ayah_reference_formatting(self) -> None:
        templater = CaptionTemplater()
        ref = templater.format_surah_reference(surah=1, ayah_start=1, ayah_end=1)
        assert ref == "سورة الفاتحة • الآية 1"

    def test_ayah_range_reference_formatting(self) -> None:
        templater = CaptionTemplater()
        ref = templater.format_surah_reference(surah=2, ayah_start=255, ayah_end=257)
        assert ref == "سورة البقرة • الآيات 255-257"

    def test_default_caption_structure(self) -> None:
        templater = CaptionTemplater(branding_handle="@quran_edits")
        caption = templater.build_caption(
            surah=1,
            ayah_start=1,
            ayah_end=1,
            canonical_text="بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ",
        )
        assert "سورة الفاتحة • الآية 1" in caption
        assert "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ" in caption
        assert "@quran_edits" in caption
        assert "#قرآن" in caption

    def test_caption_omits_handle_when_none(self) -> None:
        templater = CaptionTemplater(branding_handle=None)
        caption = templater.build_caption(
            surah=112,
            ayah_start=1,
            ayah_end=4,
            canonical_text="قُلْ هُوَ اللَّهُ أَحَدٌ",
        )
        assert "سورة الإخلاص • الآيات 1-4" in caption
        assert "\n\n\n" not in caption  # No stacked extra blank lines

    def test_custom_template_and_hashtags(self) -> None:
        templater = CaptionTemplater(
            default_template="[{surah_name}:{ayah_range}] {canonical_text}\n{hashtags}",
            default_hashtags=("#تلاوة", "#القرآن"),
        )
        caption = templater.build_caption(
            surah=114,
            ayah_start=1,
            ayah_end=6,
            canonical_text="قُلْ أَعُوذُ بِرَبِّ النَّاسِ",
        )
        assert "[الناس:1-6] قُلْ أَعُوذُ بِرَبِّ النَّاسِ" in caption
        assert "#تلاوة #القرآن" in caption

    def test_x_twitter_280_character_truncation(self) -> None:
        templater = CaptionTemplater(branding_handle="@quran_reels")
        # Very long canonical text (Ayat Al-Kursi + following ayat)
        long_verse = (
            "اللَّهُ لَا إِلَهَ إِلَّا هُوَ الْحَيُّ الْقَيُّومُ لَا تَأْخُذُهُ سِنَةٌ وَلَا نَوْمٌ "
            "لَهُ مَا فِي السَّمَاوَاتِ وَمَا فِي الْأَرْضِ مَنْ ذَا الَّذِي يَشْفَعُ عِنْدَهُ إِلَّا بِإِذْنِهِ "
            "يَعْلَمُ مَا بَيْنَ أَيْدِيهِمْ وَمَا خَلْفَهُمْ وَلَا يُحِيطُونَ بِشَيْءٍ مِنْ عِلْمِهِ إِلَّا بِمَا شَاءَ "
            "وَسِعَ كُرْسِيُّهُ السَّمَاوَاتِ وَالْأَرْضَ وَلَا يَئُودُهُ حِفْظُهُمَا وَهُوَ الْعَلِيُّ الْعَظِيمُ "
            "لَا إِكْرَاهَ فِي الدِّينِ قَدْ تَبَيَّنَ الرُّشْدُ مِنَ الْغَيِّ فَمَنْ يَكْفُرْ بِالطَّاغُوتِ وَيُؤْمِنْ بِاللَّهِ"
        )
        caption = templater.build_caption(
            surah=2,
            ayah_start=255,
            ayah_end=256,
            canonical_text=long_verse,
            platform="x",
        )
        assert len(caption) <= 280
        assert "سورة البقرة • الآيات 255-256" in caption
        assert "..." in caption

    def test_telegram_1024_character_limit_respected(self) -> None:
        templater = CaptionTemplater()
        huge_text = "آية طويلة جدا " * 150
        caption = templater.build_caption(
            surah=2,
            ayah_start=1,
            ayah_end=10,
            canonical_text=huge_text,
            platform="telegram",
        )
        assert len(caption) <= 1024
        assert "..." in caption


# ---------------------------------------------------------------------------
# 2. MultiPlatformPublishClient & StubPublishClient Tests
# ---------------------------------------------------------------------------


class TestPublishClients:
    @pytest.mark.asyncio
    async def test_stub_client_records_requests_and_draft_status(
        self, tmp_path: Path
    ) -> None:
        video_file = tmp_path / "video.mp4"
        video_file.write_bytes(b"content")

        client = StubPublishClient()
        request = PublishRequest(
            message_id=101,
            video_path=video_file,
            caption="Test caption",
            platforms=("tiktok", "instagram"),
            draft_mode=True,
        )

        response = await client.publish(request)
        assert response.overall_status == "completed"
        assert response.draft_mode is True
        assert len(response.results) == 2
        assert response.results["tiktok"].status == "draft_created"
        assert response.results["instagram"].status == "draft_created"
        assert len(client.published_requests) == 1

    @pytest.mark.asyncio
    async def test_stub_client_simulates_partial_failure(self, tmp_path: Path) -> None:
        video_file = tmp_path / "video.mp4"
        video_file.write_bytes(b"content")

        client = StubPublishClient(failing_platforms={"x"})
        request = PublishRequest(
            message_id=102,
            video_path=video_file,
            caption="Test caption",
            platforms=("tiktok", "x"),
            draft_mode=False,
        )

        response = await client.publish(request)
        assert response.overall_status == "partial"
        assert response.results["tiktok"].status == "published"
        assert response.results["x"].status == "failed"
        assert "Simulated publishing failure on x" in response.results["x"].error

    @pytest.mark.asyncio
    async def test_stub_client_fail_all(self, tmp_path: Path) -> None:
        video_file = tmp_path / "video.mp4"
        video_file.write_bytes(b"content")

        client = StubPublishClient(should_fail_all=True)
        request = PublishRequest(
            message_id=103,
            video_path=video_file,
            caption="Test",
            platforms=("tiktok", "youtube"),
        )
        response = await client.publish(request)
        assert response.overall_status == "failed"
        assert response.results["tiktok"].status == "failed"
        assert response.results["youtube"].status == "failed"

    @pytest.mark.asyncio
    async def test_http_client_parses_platforms_dict_response(
        self, tmp_path: Path
    ) -> None:
        video_file = tmp_path / "video.mp4"
        video_file.write_bytes(b"content")

        client = MultiPlatformPublishClient(
            api_base_url="https://api.example.com",
            api_key="secret-token",
        )

        mock_payload = {
            "status": "success",
            "platforms": {
                "tiktok": {"status": "success", "id": "tt_123", "url": "https://tiktok.com/@post/123"},
                "x": {"status": "error", "message": "rate limit"},
            },
        }

        with patch.object(client, "_send_http_request", return_value=mock_payload):
            request = PublishRequest(
                message_id=201,
                video_path=video_file,
                caption="Caption",
                platforms=("tiktok", "x"),
                draft_mode=False,
            )
            response = await client.publish(request)

        assert response.overall_status == "partial"
        assert response.results["tiktok"].status == "published"
        assert response.results["tiktok"].post_id == "tt_123"
        assert response.results["x"].status == "failed"
        assert response.results["x"].error == "rate limit"

    @pytest.mark.asyncio
    async def test_http_client_retry_on_429_then_succeed(self, tmp_path: Path) -> None:
        video_file = tmp_path / "video.mp4"
        video_file.write_bytes(b"content")

        client = MultiPlatformPublishClient(
            api_base_url="https://api.example.com",
            api_key="test-key",
            max_retries=3,
            backoff_factor=0.01,
        )

        # First call raises HTTP 429, second call succeeds
        err_429 = urllib.error.HTTPError(
            url="https://api.example.com/posts",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=None,
        )
        mock_success = {"status": "success", "id": "post_all"}

        call_count = 0

        async def _mock_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise err_429
            return mock_success

        with patch.object(client, "_send_http_request", side_effect=_mock_send):
            request = PublishRequest(
                message_id=202,
                video_path=video_file,
                caption="Caption",
                platforms=("instagram",),
                draft_mode=False,
            )
            response = await client.publish(request)

        assert call_count == 2
        assert response.overall_status == "completed"
        assert response.results["instagram"].status == "published"


# ---------------------------------------------------------------------------
# 3. PublishService Tests (Idempotency, Flow, Failure Paths)
# ---------------------------------------------------------------------------


class TestPublishService:
    @pytest.mark.asyncio
    async def test_publish_happy_path(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 701
        create_mock_artifacts(temp_storage, message_id)

        client = StubPublishClient()
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
            default_platforms=("tiktok", "instagram", "youtube"),
            default_draft_mode=False,
            branding_handle="@quran_channel",
        )

        result = await service.publish(message_id)

        assert result.status == "completed"
        assert result.artifact_path is not None
        assert result.artifact_path.is_file()

        # Verify state in repository
        state = state_repo.fetch_stage(message_id, "publish")
        assert state is not None
        assert state["status"] == "completed"
        assert state["error"] is None

        # Verify publish/{message_id}.json artifact content
        artifact_data = json.loads(result.artifact_path.read_text(encoding="utf-8"))
        assert artifact_data["message_id"] == message_id
        assert artifact_data["overall_status"] == "completed"
        assert artifact_data["draft_mode"] is False
        assert "tiktok" in artifact_data["platforms"]
        assert artifact_data["platforms"]["tiktok"]["status"] == "published"
        assert "@quran_channel" in artifact_data["caption"]

        # No failure alerts sent
        assert len(alert_service.publish_failures) == 0

    @pytest.mark.asyncio
    async def test_publish_idempotency_prevents_duplicate_publication(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 702
        create_mock_artifacts(temp_storage, message_id)

        client = StubPublishClient()
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
            default_platforms=("tiktok", "instagram"),
        )

        # First run: publishes
        result1 = await service.publish(message_id)
        assert result1.status == "completed"
        assert len(client.published_requests) == 1

        # Second run: must recognize existing completion and skip client call
        result2 = await service.publish(message_id)
        assert result2.status == "skipped_already_published"
        assert result2.artifact_path == result1.artifact_path
        # Confirm client was NOT invoked a second time
        assert len(client.published_requests) == 1

    @pytest.mark.asyncio
    async def test_publish_draft_mode_flag(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 703
        create_mock_artifacts(temp_storage, message_id)

        client = StubPublishClient()
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
            default_draft_mode=True,
        )

        result = await service.publish(message_id, draft_mode=True)
        assert result.status == "completed"

        artifact_data = json.loads(result.artifact_path.read_text(encoding="utf-8"))
        assert artifact_data["draft_mode"] is True
        for platform_res in artifact_data["platforms"].values():
            assert platform_res["status"] == "draft_created"

    @pytest.mark.asyncio
    async def test_publish_fails_when_rendered_video_missing(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 704
        # Create match JSON but omit video MP4
        match_path = temp_storage / "match" / f"{message_id}.json"
        match_path.write_text(
            json.dumps({"surah": 1, "ayah_start": 1, "ayah_end": 1, "canonical_text": "text"}),
            encoding="utf-8",
        )

        client = StubPublishClient()
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
        )

        result = await service.publish(message_id)
        assert result.status == "failed"
        assert "Rendered video missing" in result.error

        # Assert state updated to failed and alert dispatched
        state = state_repo.fetch_stage(message_id, "publish")
        assert state["status"] == "failed"
        assert len(alert_service.publish_failures) == 1
        assert "Rendered video missing" in alert_service.publish_failures[0]

    @pytest.mark.asyncio
    async def test_publish_fails_when_match_metadata_missing(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 705
        # Create video MP4 but omit match JSON
        video_path = temp_storage / "render" / f"{message_id}.mp4"
        video_path.write_bytes(b"dummy")

        client = StubPublishClient()
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
        )

        result = await service.publish(message_id)
        assert result.status == "failed"
        assert "Match metadata missing" in result.error
        assert len(alert_service.publish_failures) == 1

    @pytest.mark.asyncio
    async def test_publish_partial_failure_alerts_and_records_state(
        self,
        temp_storage: Path,
        state_repo: PipelineStateRepository,
        alert_service: MockAlertService,
    ) -> None:
        message_id = 706
        create_mock_artifacts(temp_storage, message_id)

        client = StubPublishClient(failing_platforms={"x"})
        service = PublishService(
            storage_root=temp_storage,
            state_repository=state_repo,
            alert_service=alert_service,
            client=client,
            default_platforms=("tiktok", "x"),
        )

        result = await service.publish(message_id)
        assert result.status == "partial"
        assert result.artifact_path.is_file()

        # State records completed with error message
        state = state_repo.fetch_stage(message_id, "publish")
        assert state["status"] == "completed"
        assert "Partial publishing failures" in state["error"]

        # Alert sent for partial failure
        assert len(alert_service.publish_failures) == 1
        assert "partially published" in alert_service.publish_failures[0]


# ---------------------------------------------------------------------------
# 4. Publish Settings & CLI Tests
# ---------------------------------------------------------------------------


class TestPublishCLIAndConfig:
    def test_publish_settings_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PUBLISH_API_BASE_URL", "https://api.postiz.example.com")
        monkeypatch.setenv("PUBLISH_API_KEY", "xyz123")
        monkeypatch.setenv("PUBLISH_PLATFORMS", "tiktok,youtube")
        monkeypatch.setenv("PUBLISH_DRAFT_MODE", "true")
        monkeypatch.setenv("BRANDING_HANDLE", "@myquran")
        monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "test.db"))

        settings = PublishSettings.from_env()
        assert settings.publish_api_base_url == "https://api.postiz.example.com"
        assert settings.publish_api_key == "xyz123"
        assert settings.publish_platforms == ("tiktok", "youtube")
        assert settings.publish_draft_mode is True
        assert settings.branding_handle == "@myquran"

    @pytest.mark.asyncio
    async def test_cli_execution_with_stub_client(
        self, monkeypatch: pytest.MonkeyPatch, temp_storage: Path, tmp_path: Path
    ) -> None:
        message_id = 707
        create_mock_artifacts(temp_storage, message_id)

        monkeypatch.setenv("PIPELINE_STORAGE_ROOT", str(temp_storage))
        monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "cli_state.db"))
        monkeypatch.setenv("PUBLISH_API_BASE_URL", "")  # Uses stub
        monkeypatch.setenv("PUBLISH_PLATFORMS", "tiktok,instagram")

        # First run: returns 0 (completed)
        code1 = await cli_main([str(message_id), "--draft"])
        assert code1 == 0

        # Second run: returns 0 (idempotent skip)
        code2 = await cli_main([str(message_id), "--draft"])
        assert code2 == 0

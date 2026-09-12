"""Unit and integration tests for Phase 7 Multi-Platform Publishing (Ayrshare & Media Uploader).

Written with pure standard-library unittest for hermetic execution in all environments,
while also fully discoverable and compatible with pytest.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pipeline.config import PublishSettings
from pipeline.publish.app import main as cli_main, parse_args
from pipeline.publish.client import (
    AYRSHARE_TO_PLATFORM,
    PLATFORM_TO_AYRSHARE,
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
from pipeline.publish.uploader import (
    MediaUploader,
    PublicUrlMediaUploader,
    S3MediaUploader,
    StubMediaUploader,
)
from pipeline.state.repository import PipelineStateRepository


class MockAlertService:
    """Mock alert recorder."""

    def __init__(self) -> None:
        self.publish_failures: list[str] = []

    async def send_publish_failure(self, message: str) -> None:
        self.publish_failures.append(message)


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
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"dummy_h264_mp4_content")

    match_path = storage_root / "match" / f"{message_id}.json"
    match_path.parent.mkdir(parents=True, exist_ok=True)
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


class CaptionTemplaterTests(unittest.TestCase):
    def test_single_ayah_reference_formatting(self) -> None:
        templater = CaptionTemplater()
        ref = templater.format_surah_reference(surah=1, ayah_start=1, ayah_end=1)
        self.assertEqual(ref, "سورة الفاتحة • الآية 1")

    def test_ayah_range_reference_formatting(self) -> None:
        templater = CaptionTemplater()
        ref = templater.format_surah_reference(surah=2, ayah_start=255, ayah_end=257)
        self.assertEqual(ref, "سورة البقرة • الآيات 255-257")

    def test_default_caption_structure(self) -> None:
        templater = CaptionTemplater(branding_handle="@quran_edits")
        caption = templater.build_caption(
            surah=1,
            ayah_start=1,
            ayah_end=1,
            canonical_text="بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ",
        )
        self.assertIn("سورة الفاتحة • الآية 1", caption)
        self.assertIn("بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ", caption)
        self.assertIn("@quran_edits", caption)
        self.assertIn("#قرآن", caption)

    def test_caption_omits_handle_when_none(self) -> None:
        templater = CaptionTemplater(branding_handle=None)
        caption = templater.build_caption(
            surah=112,
            ayah_start=1,
            ayah_end=4,
            canonical_text="قُلْ هُوَ اللَّهُ أَحَدٌ",
        )
        self.assertIn("سورة الإخلاص • الآيات 1-4", caption)
        self.assertNotIn("\n\n\n", caption)

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
        self.assertIn("[الناس:1-6] قُلْ أَعُوذُ بِرَبِّ النَّاسِ", caption)
        self.assertIn("#تلاوة #القرآن", caption)

    def test_x_twitter_280_character_truncation(self) -> None:
        templater = CaptionTemplater(branding_handle="@quran_reels")
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
        self.assertLessEqual(len(caption), 280)
        self.assertIn("سورة البقرة • الآيات 255-256", caption)
        self.assertIn("...", caption)

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
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("...", caption)


# ---------------------------------------------------------------------------
# 2. Media Uploader Tests
# ---------------------------------------------------------------------------


class MediaUploaderTests(unittest.TestCase):
    def test_public_url_uploader(self) -> None:
        uploader = PublicUrlMediaUploader(public_base_url="https://cdn.example.com/videos")
        path = Path("render/101.mp4")
        url = asyncio.run(uploader.upload_media(path, 101))
        self.assertEqual(url, "https://cdn.example.com/videos/101.mp4")

    def test_stub_media_uploader_records_history(self) -> None:
        uploader = StubMediaUploader(base_url="https://s3.local/render")
        path = Path("render/202.mp4")
        url = asyncio.run(uploader.upload_media(path, 202))
        self.assertEqual(url, "https://s3.local/render/202.mp4")
        self.assertEqual(len(uploader.uploaded_files), 1)
        self.assertEqual(uploader.uploaded_files[0], (path, 202, url))

    def test_s3_media_uploader_constructs_public_url_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "303.mp4"
            file_path.write_bytes(b"sample_data")

            uploader = S3MediaUploader(
                endpoint="http://localhost:9000",
                bucket="render",
                public_base_url="https://cdn.example.com/render",
            )
            with patch("urllib.request.urlopen"):
                with patch("boto3.client", side_effect=Exception("no boto3")):
                    url = asyncio.run(uploader.upload_media(file_path, 303))
                    self.assertEqual(url, "https://cdn.example.com/render/303.mp4")

    def test_s3_media_uploader_raises_runtime_error_when_both_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "304.mp4"
            file_path.write_bytes(b"sample_data")

            uploader = S3MediaUploader(
                endpoint="http://localhost:9000",
                bucket="render",
            )
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                with patch("boto3.client", side_effect=Exception("no boto3")):
                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(uploader.upload_media(file_path, 304))
                    self.assertIn("S3 media upload failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# 3. MultiPlatformPublishClient & Ayrshare Schema Tests
# ---------------------------------------------------------------------------


class PublishClientTests(unittest.TestCase):
    def test_ayrshare_endpoint_resolution(self) -> None:
        client1 = MultiPlatformPublishClient(api_base_url="https://app.ayrshare.com/api")
        self.assertEqual(client1._resolve_endpoint_url(), "https://app.ayrshare.com/api/post")

        client2 = MultiPlatformPublishClient(api_base_url="https://app.ayrshare.com/api/post")
        self.assertEqual(client2._resolve_endpoint_url(), "https://app.ayrshare.com/api/post")

        client3 = MultiPlatformPublishClient(api_base_url="https://custom.endpoint.com/posts")
        self.assertEqual(client3._resolve_endpoint_url(), "https://custom.endpoint.com/post")

    def test_stub_client_records_requests_and_draft_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = StubPublishClient()
            request = PublishRequest(
                message_id=101,
                video_path=video_file,
                caption="Test caption",
                platforms=("tiktok", "instagram"),
                media_urls=("https://cdn.example.com/render/101.mp4",),
                draft_mode=True,
            )

            response = asyncio.run(client.publish(request))
            self.assertEqual(response.overall_status, "completed")
            self.assertTrue(response.draft_mode)
            self.assertEqual(len(response.results), 2)
            self.assertEqual(response.results["tiktok"].status, "draft_created")
            self.assertEqual(response.results["instagram"].status, "draft_created")
            self.assertEqual(len(client.published_requests), 1)

    def test_stub_client_simulates_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = StubPublishClient(failing_platforms={"x"})
            request = PublishRequest(
                message_id=102,
                video_path=video_file,
                caption="Test caption",
                platforms=("tiktok", "x"),
                media_urls=("https://cdn.example.com/render/102.mp4",),
                draft_mode=False,
            )

            response = asyncio.run(client.publish(request))
            self.assertEqual(response.overall_status, "partial")
            self.assertEqual(response.results["tiktok"].status, "published")
            self.assertEqual(response.results["x"].status, "failed")
            self.assertIn("Simulated publishing failure on x", response.results["x"].error)

    def test_stub_client_fail_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = StubPublishClient(should_fail_all=True)
            request = PublishRequest(
                message_id=103,
                video_path=video_file,
                caption="Test",
                platforms=("tiktok", "youtube"),
            )
            response = asyncio.run(client.publish(request))
            self.assertEqual(response.overall_status, "failed")
            self.assertEqual(response.results["tiktok"].status, "failed")
            self.assertEqual(response.results["youtube"].status, "failed")

    def test_http_client_sends_ayrshare_schema_and_parses_post_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = MultiPlatformPublishClient(
                api_base_url="https://app.ayrshare.com/api",
                api_key="secret-ayrshare-token",
            )

            mock_ayrshare_response = {
                "status": "success",
                "errors": [],
                "postIds": [
                    {
                        "platform": "twitter",
                        "status": "success",
                        "id": "1858948421974925758",
                        "postUrl": "https://twitter.com/user/status/1858948421974925758",
                    },
                    {
                        "platform": "tiktok",
                        "status": "success",
                        "id": "tt_998877",
                        "postUrl": "https://www.tiktok.com/@user/video/998877",
                    },
                ],
                "id": "ayr_post_123",
            }

            captured_payload: dict[str, Any] = {}

            async def _mock_send(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
                nonlocal captured_payload
                captured_payload = json.loads(body.decode("utf-8"))
                self.assertEqual(url, "https://app.ayrshare.com/api/post")
                self.assertEqual(headers["Authorization"], "Bearer secret-ayrshare-token")
                return mock_ayrshare_response

            with patch.object(client, "_send_http_request", side_effect=_mock_send):
                request = PublishRequest(
                    message_id=201,
                    video_path=video_file,
                    caption="Sublime recitation #Quran",
                    platforms=("x", "tiktok"),
                    media_urls=("https://cdn.example.com/render/201.mp4",),
                    title="Al-Fatiha",
                    draft_mode=False,
                )
                response = asyncio.run(client.publish(request))

            # Verify Ayrshare request payload fields
            self.assertEqual(captured_payload["post"], "Sublime recitation #Quran")
            self.assertEqual(captured_payload["platforms"], ["twitter", "tiktok"])  # 'x' mapped to 'twitter'
            self.assertEqual(captured_payload["mediaUrls"], ["https://cdn.example.com/render/201.mp4"])
            self.assertTrue(captured_payload["isVideo"])
            self.assertEqual(captured_payload["title"], "Al-Fatiha")

            # Verify response parsing: 'twitter' mapped back to 'x'
            self.assertEqual(response.overall_status, "completed")
            self.assertIn("x", response.results)
            self.assertEqual(response.results["x"].status, "published")
            self.assertEqual(response.results["x"].post_id, "1858948421974925758")
            self.assertEqual(
                response.results["x"].url,
                "https://twitter.com/user/status/1858948421974925758",
            )
            self.assertEqual(response.results["tiktok"].status, "published")
            self.assertEqual(response.results["tiktok"].post_id, "tt_998877")

    def test_http_client_parses_ayrshare_errors_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = MultiPlatformPublishClient(
                api_base_url="https://app.ayrshare.com/api",
                api_key="test-key",
            )

            mock_partial_response = {
                "status": "success",
                "errors": [
                    {"platform": "twitter", "message": "Twitter account token expired"}
                ],
                "postIds": [
                    {
                        "platform": "tiktok",
                        "status": "success",
                        "id": "tt_111",
                        "postUrl": "https://tiktok.com/111",
                    }
                ],
            }

            with patch.object(client, "_send_http_request", return_value=mock_partial_response):
                request = PublishRequest(
                    message_id=202,
                    video_path=video_file,
                    caption="Caption",
                    platforms=("x", "tiktok"),
                    media_urls=("https://cdn.example.com/render/202.mp4",),
                )
                response = asyncio.run(client.publish(request))

            self.assertEqual(response.overall_status, "partial")
            self.assertEqual(response.results["tiktok"].status, "published")
            self.assertEqual(response.results["x"].status, "failed")
            self.assertIn("Twitter account token expired", response.results["x"].error)

    def test_http_client_retry_on_429_then_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_file = Path(temp_dir) / "video.mp4"
            video_file.write_bytes(b"content")

            client = MultiPlatformPublishClient(
                api_base_url="https://app.ayrshare.com/api",
                api_key="test-key",
                max_retries=3,
                backoff_factor=0.01,
            )

            err_429 = urllib.error.HTTPError(
                url="https://app.ayrshare.com/api/post",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=None,
            )
            mock_success = {
                "status": "success",
                "postIds": [{"platform": "instagram", "id": "ig_555", "postUrl": "https://ig/555"}],
            }

            call_count = 0

            async def _mock_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise err_429
                return mock_success

            with patch.object(client, "_send_http_request", side_effect=_mock_send):
                request = PublishRequest(
                    message_id=203,
                    video_path=video_file,
                    caption="Caption",
                    platforms=("instagram",),
                    media_urls=("https://cdn.example.com/render/203.mp4",),
                )
                response = asyncio.run(client.publish(request))

            self.assertEqual(call_count, 2)
            self.assertEqual(response.overall_status, "completed")
            self.assertEqual(response.results["instagram"].status, "published")


# ---------------------------------------------------------------------------
# 4. PublishService Tests (Idempotency, Media Upload, Alerts)
# ---------------------------------------------------------------------------


class PublishServiceTests(unittest.TestCase):
    def test_publish_happy_path_with_media_uploader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 701
            create_mock_artifacts(storage, message_id)

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient()
            uploader = StubMediaUploader(base_url="https://cdn.example.com/render")

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
                media_uploader=uploader,
                default_platforms=("tiktok", "instagram", "youtube"),
                default_draft_mode=False,
                branding_handle="@quran_channel",
            )

            result = asyncio.run(service.publish(message_id))

            self.assertEqual(result.status, "completed")
            self.assertIsNotNone(result.artifact_path)
            self.assertTrue(result.artifact_path.is_file())

            # Verify uploader was called and passed to client
            self.assertEqual(len(uploader.uploaded_files), 1)
            self.assertEqual(client.published_requests[0].media_urls, (f"https://cdn.example.com/render/{message_id}.mp4",))

            # Verify state in repository
            state = state_repo.fetch_stage(message_id, "publish")
            self.assertIsNotNone(state)
            self.assertEqual(state["status"], "completed")
            self.assertIsNone(state["error"])

            # Verify publish/{message_id}.json artifact content
            artifact_data = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact_data["message_id"], message_id)
            self.assertEqual(artifact_data["overall_status"], "completed")
            self.assertEqual(artifact_data["media_urls"], [f"https://cdn.example.com/render/{message_id}.mp4"])
            self.assertFalse(artifact_data["draft_mode"])
            self.assertIn("tiktok", artifact_data["platforms"])
            self.assertEqual(artifact_data["platforms"]["tiktok"]["status"], "published")
            self.assertIn("@quran_channel", artifact_data["caption"])

            # No failure alerts sent
            self.assertEqual(len(alert_service.publish_failures), 0)

    def test_publish_idempotency_prevents_duplicate_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 702
            create_mock_artifacts(storage, message_id)

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient()
            uploader = StubMediaUploader()

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
                media_uploader=uploader,
                default_platforms=("tiktok", "instagram"),
            )

            # First run: publishes
            result1 = asyncio.run(service.publish(message_id))
            self.assertEqual(result1.status, "completed")
            self.assertEqual(len(client.published_requests), 1)
            self.assertEqual(len(uploader.uploaded_files), 1)

            # Second run: must recognize existing completion and skip client & uploader
            result2 = asyncio.run(service.publish(message_id))
            self.assertEqual(result2.status, "skipped_already_published")
            self.assertEqual(result2.artifact_path, result1.artifact_path)
            self.assertEqual(len(client.published_requests), 1)
            self.assertEqual(len(uploader.uploaded_files), 1)

    def test_publish_draft_mode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 703
            create_mock_artifacts(storage, message_id)

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient()

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
                default_draft_mode=True,
            )

            result = asyncio.run(service.publish(message_id, draft_mode=True))
            self.assertEqual(result.status, "completed")

            artifact_data = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact_data["draft_mode"])
            for platform_res in artifact_data["platforms"].values():
                self.assertEqual(platform_res["status"], "draft_created")

    def test_publish_fails_when_rendered_video_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 704
            match_path = storage / "match" / f"{message_id}.json"
            match_path.parent.mkdir(parents=True, exist_ok=True)
            match_path.write_text(
                json.dumps({"surah": 1, "ayah_start": 1, "ayah_end": 1, "canonical_text": "text"}),
                encoding="utf-8",
            )

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient()

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
            )

            result = asyncio.run(service.publish(message_id))
            self.assertEqual(result.status, "failed")
            self.assertIn("Rendered video missing", result.error)

            state = state_repo.fetch_stage(message_id, "publish")
            self.assertEqual(state["status"], "failed")
            self.assertEqual(len(alert_service.publish_failures), 1)

    def test_publish_fails_when_match_metadata_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 705
            video_path = storage / "render" / f"{message_id}.mp4"
            video_path.parent.mkdir(parents=True, exist_ok=True)
            video_path.write_bytes(b"dummy")

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient()

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
            )

            result = asyncio.run(service.publish(message_id))
            self.assertEqual(result.status, "failed")
            self.assertIn("Match metadata missing", result.error)
            self.assertEqual(len(alert_service.publish_failures), 1)

    def test_publish_partial_failure_alerts_and_records_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 706
            create_mock_artifacts(storage, message_id)

            state_repo = PipelineStateRepository(storage / "test_state.db")
            alert_service = MockAlertService()
            client = StubPublishClient(failing_platforms={"x"})

            service = PublishService(
                storage_root=storage,
                state_repository=state_repo,
                alert_service=alert_service,
                client=client,
                default_platforms=("tiktok", "x"),
            )

            result = asyncio.run(service.publish(message_id))
            self.assertEqual(result.status, "partial")
            self.assertTrue(result.artifact_path.is_file())

            state = state_repo.fetch_stage(message_id, "publish")
            self.assertEqual(state["status"], "completed")
            self.assertIn("Partial publishing failures", state["error"])
            self.assertEqual(len(alert_service.publish_failures), 1)
            self.assertIn("partially published", alert_service.publish_failures[0])


# ---------------------------------------------------------------------------
# 5. Publish Settings & CLI Tests
# ---------------------------------------------------------------------------


class PublishCLIAndConfigTests(unittest.TestCase):
    def test_publish_settings_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PUBLISH_API_BASE_URL": "https://app.ayrshare.com/api",
                "PUBLISH_API_KEY": "xyz123",
                "PUBLISH_PLATFORMS": "tiktok,youtube",
                "PUBLISH_DRAFT_MODE": "true",
                "BRANDING_HANDLE": "@myquran",
                "PUBLIC_MEDIA_BASE_URL": "https://cdn.example.com/render",
                "STATE_DB_PATH": "state.db",
            },
        ):
            settings = PublishSettings.from_env()
            self.assertEqual(settings.publish_api_base_url, "https://app.ayrshare.com/api")
            self.assertEqual(settings.publish_api_key, "xyz123")
            self.assertEqual(settings.publish_platforms, ("tiktok", "youtube"))
            self.assertTrue(settings.publish_draft_mode)
            self.assertEqual(settings.branding_handle, "@myquran")
            self.assertEqual(settings.public_media_base_url, "https://cdn.example.com/render")

    def test_cli_execution_with_stub_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Path(temp_dir)
            message_id = 707
            create_mock_artifacts(storage, message_id)

            with patch.dict(
                "os.environ",
                {
                    "PIPELINE_STORAGE_ROOT": str(storage),
                    "STATE_DB_PATH": str(storage / "cli_state.db"),
                    "PUBLISH_API_KEY": "",  # Triggers stub client
                    "PUBLISH_PLATFORMS": "tiktok,instagram",
                },
            ):
                code1 = asyncio.run(cli_main([str(message_id), "--draft"]))
                self.assertEqual(code1, 0)

                code2 = asyncio.run(cli_main([str(message_id), "--draft"]))
                self.assertEqual(code2, 0)


if __name__ == "__main__":
    unittest.main()

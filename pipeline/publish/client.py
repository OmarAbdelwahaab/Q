"""Multi-platform publishing API client and abstractions."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pipeline.logging import get_logger


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Publishing request payload across target platforms."""

    message_id: int
    video_path: Path
    caption: str
    platforms: tuple[str, ...]
    draft_mode: bool = False
    title: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformPublishResult:
    """Individual platform publishing outcome."""

    platform: str
    status: str  # "published", "draft_created", "failed", "skipped"
    post_id: str | None = None
    url: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResponse:
    """Aggregated outcome of a multi-platform publishing operation."""

    message_id: int
    overall_status: str  # "completed", "partial", "failed"
    draft_mode: bool
    results: dict[str, PlatformPublishResult]
    raw_response: dict[str, Any] | None = None
    error: str | None = None


class PublishClient(Protocol):
    """Client protocol for multi-platform video publishing."""

    async def publish(self, request: PublishRequest) -> PublishResponse:
        """Publish a video across configured social platforms."""
        ...


class MultiPlatformPublishClient:
    """Concrete HTTP client communicating with a unified multi-platform publishing endpoint."""

    def __init__(
        self,
        api_base_url: str,
        api_key: str | None = None,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout_seconds = timeout_seconds
        self.logger = get_logger(__name__, service="publish_client")

    async def publish(self, request: PublishRequest) -> PublishResponse:
        """Post the video and metadata to the unified API with retry and draft support."""
        url = f"{self.api_base_url}/posts" if not self.api_base_url.endswith("/posts") else self.api_base_url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "messageId": request.message_id,
            "videoPath": str(request.video_path),
            "caption": request.caption,
            "platforms": list(request.platforms),
            "draft": request.draft_mode,
            "sandbox": request.draft_mode,
        }
        if request.title:
            payload["title"] = request.title
        if request.tags:
            payload["tags"] = list(request.tags)

        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw_json = await self._send_http_request(url, headers, body_bytes)
                return self._parse_api_response(request, raw_json)
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
                response_text = ""
                try:
                    response_text = exc.read().decode("utf-8")
                except Exception:
                    pass

                self.logger.warning(
                    "Publish API HTTP error",
                    extra={
                        "attempt": attempt,
                        "code": exc.code,
                        "error": last_error,
                        "response": response_text[:300],
                    },
                )
                # Retry on rate limits (429) or transient server errors (502, 503, 504)
                if exc.code in {429, 502, 503, 504} and attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"Network error: {exc}"
                self.logger.warning(
                    "Publish API network error",
                    extra={"attempt": attempt, "error": last_error},
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                    continue
                break

        # All retries failed
        error_msg = last_error or "Publish API request failed"
        failed_results = {
            p: PlatformPublishResult(
                platform=p,
                status="failed",
                error=error_msg,
            )
            for p in request.platforms
        }
        return PublishResponse(
            message_id=request.message_id,
            overall_status="failed",
            draft_mode=request.draft_mode,
            results=failed_results,
            error=error_msg,
        )

    async def _send_http_request(
        self, url: str, headers: dict[str, str], body: bytes
    ) -> dict[str, Any]:
        """Execute HTTP POST asynchronously on standard asyncio executor."""
        loop = asyncio.get_running_loop()

        def _do_request() -> dict[str, Any]:
            req = urllib.request.Request(
                url=url,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else {}

        return await loop.run_in_executor(None, _do_request)

    def _parse_api_response(
        self, request: PublishRequest, raw: dict[str, Any]
    ) -> PublishResponse:
        """Parse diverse multi-platform API responses into standard PublishResponse."""
        results: dict[str, PlatformPublishResult] = {}
        success_status = "draft_created" if request.draft_mode else "published"

        # Case 1: platforms dict in response, e.g. {"platforms": {"tiktok": {"status": "success", "id": "..."}}}
        if "platforms" in raw and isinstance(raw["platforms"], dict):
            for platform, pdata in raw["platforms"].items():
                if isinstance(pdata, dict):
                    status = pdata.get("status", "success")
                    p_status = success_status if status in ("success", "ok", "published", "draft_created") else "failed"
                    results[platform] = PlatformPublishResult(
                        platform=platform,
                        status=p_status,
                        post_id=str(pdata.get("id") or pdata.get("post_id") or ""),
                        url=pdata.get("url") or pdata.get("postUrl"),
                        error=pdata.get("error") or pdata.get("message"),
                    )

        # Case 2: postIds list, e.g. {"postIds": [{"platform": "tiktok", "id": "123", "postUrl": "..."}]}
        elif "postIds" in raw and isinstance(raw["postIds"], list):
            for item in raw["postIds"]:
                if isinstance(item, dict) and "platform" in item:
                    p = str(item["platform"]).lower()
                    status = item.get("status", "success")
                    p_status = success_status if status in ("success", "ok", "published", "draft_created") else "failed"
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status=p_status,
                        post_id=str(item.get("id") or item.get("postId") or ""),
                        url=item.get("postUrl") or item.get("url"),
                        error=item.get("error"),
                    )

        # Fill any missing platforms from request
        for p in request.platforms:
            if p not in results:
                # If top-level indicates success, mark as succeeded
                if raw.get("status") in ("success", "ok", True) or "id" in raw:
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status=success_status,
                        post_id=str(raw.get("id") or f"{p}_{request.message_id}"),
                        url=raw.get("url"),
                    )
                else:
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status="failed",
                        error=raw.get("message") or raw.get("error") or "Unknown platform publishing failure",
                    )

        success_count = sum(1 for r in results.values() if r.status in ("published", "draft_created"))
        if success_count == len(request.platforms):
            overall_status = "completed"
        elif success_count > 0:
            overall_status = "partial"
        else:
            overall_status = "failed"

        return PublishResponse(
            message_id=request.message_id,
            overall_status=overall_status,
            draft_mode=request.draft_mode,
            results=results,
            raw_response=raw,
            error=None if overall_status != "failed" else raw.get("message") or "Publishing failed",
        )


class StubPublishClient:
    """Hermetic in-memory stub client for unit testing and local dry runs."""

    def __init__(
        self,
        failing_platforms: set[str] | None = None,
        should_fail_all: bool = False,
        error_message: str = "Simulated publishing failure",
        delay_seconds: float = 0.0,
    ) -> None:
        self.published_requests: list[PublishRequest] = []
        self.failing_platforms = set(failing_platforms or ())
        self.should_fail_all = should_fail_all
        self.error_message = error_message
        self.delay_seconds = delay_seconds

    async def publish(self, request: PublishRequest) -> PublishResponse:
        """Simulate multi-platform publishing."""
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        self.published_requests.append(request)

        if self.should_fail_all:
            results = {
                p: PlatformPublishResult(
                    platform=p,
                    status="failed",
                    error=self.error_message,
                )
                for p in request.platforms
            }
            return PublishResponse(
                message_id=request.message_id,
                overall_status="failed",
                draft_mode=request.draft_mode,
                results=results,
                error=self.error_message,
            )

        results: dict[str, PlatformPublishResult] = {}
        for p in request.platforms:
            if p in self.failing_platforms:
                results[p] = PlatformPublishResult(
                    platform=p,
                    status="failed",
                    error=f"{self.error_message} on {p}",
                )
            else:
                status = "draft_created" if request.draft_mode else "published"
                results[p] = PlatformPublishResult(
                    platform=p,
                    status=status,
                    post_id=f"stub_{p}_{request.message_id}",
                    url=f"https://{p}.com/post/stub_{p}_{request.message_id}",
                )

        success_count = sum(1 for r in results.values() if r.status in ("published", "draft_created"))
        if success_count == len(request.platforms):
            overall = "completed"
        elif success_count > 0:
            overall = "partial"
        else:
            overall = "failed"

        return PublishResponse(
            message_id=request.message_id,
            overall_status=overall,
            draft_mode=request.draft_mode,
            results=results,
        )

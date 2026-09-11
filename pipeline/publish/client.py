"""Multi-platform publishing API client (Ayrshare integration and abstractions)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pipeline.logging import get_logger

# Platform mapping between pipeline names and Ayrshare provider names
PLATFORM_TO_AYRSHARE: dict[str, str] = {
    "x": "twitter",
    "twitter": "twitter",
    "tiktok": "tiktok",
    "instagram": "instagram",
    "youtube": "youtube",
    "facebook": "facebook",
    "telegram": "telegram",
}

AYRSHARE_TO_PLATFORM: dict[str, str] = {
    "twitter": "x",
}


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Publishing request payload across target platforms."""

    message_id: int
    video_path: Path
    caption: str
    platforms: tuple[str, ...]
    media_urls: tuple[str, ...] = ()
    is_video: bool = True
    draft_mode: bool = False
    title: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformPublishResult:
    """Individual platform publishing outcome."""

    platform: str
    status: str  # "published", "draft_created", "pending", "failed", "skipped"
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
    """Concrete client communicating with Ayrshare (POST /api/post) and compatible endpoints."""

    def __init__(
        self,
        api_base_url: str = "https://app.ayrshare.com/api",
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

    def _resolve_endpoint_url(self) -> str:
        """Resolve the official post endpoint path (/api/post)."""
        base = self.api_base_url
        if base.endswith("/post"):
            return base
        if base.endswith("/posts"):
            return base[:-1]  # Normalize plural to singular /post
        return f"{base}/post"

    async def publish(self, request: PublishRequest) -> PublishResponse:
        """Post the video and metadata to Ayrshare API with retry and draft support."""
        url = self._resolve_endpoint_url()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Map pipeline platform names (e.g. 'x') to Ayrshare platform names (e.g. 'twitter')
        mapped_platforms = [
            PLATFORM_TO_AYRSHARE.get(p.lower(), p.lower())
            for p in request.platforms
        ]

        # Ayrshare official /api/post payload
        payload: dict[str, Any] = {
            "post": request.caption,
            "platforms": mapped_platforms,
        }
        if request.media_urls:
            payload["mediaUrls"] = list(request.media_urls)
            payload["isVideo"] = request.is_video

        if request.title:
            payload["title"] = request.title
        if request.draft_mode:
            payload["draft"] = True

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
        """Parse Ayrshare official API response into standard PublishResponse."""
        results: dict[str, PlatformPublishResult] = {}
        success_status = "draft_created" if request.draft_mode else "published"

        # Ayrshare error map from errors array: e.g. [{"platform": "twitter", "message": "..."}]
        platform_errors: dict[str, str] = {}
        if "errors" in raw and isinstance(raw["errors"], list):
            for err in raw["errors"]:
                if isinstance(err, dict):
                    p_name = str(err.get("platform", "")).lower()
                    msg = err.get("message") or err.get("error") or "Platform publishing error"
                    if p_name:
                        platform_errors[p_name] = msg
                        # Also map Ayrshare platform alias if applicable
                        if p_name in AYRSHARE_TO_PLATFORM:
                            platform_errors[AYRSHARE_TO_PLATFORM[p_name]] = msg

        # Primary Ayrshare schema: "postIds" list: [{"platform": "twitter", "status": "success", "id": "...", "postUrl": "..."}]
        if "postIds" in raw and isinstance(raw["postIds"], list):
            for item in raw["postIds"]:
                if isinstance(item, dict) and "platform" in item:
                    raw_p = str(item["platform"]).lower()
                    # Resolve to pipeline platform name (e.g. 'twitter' -> 'x' if 'x' was requested)
                    p = "x" if raw_p == "twitter" and "x" in request.platforms else raw_p

                    status_str = item.get("status", "success").lower()
                    if status_str in ("success", "ok", "published", "draft_created"):
                        p_status = success_status
                    elif status_str == "pending":
                        p_status = "pending"
                    else:
                        p_status = "failed"

                    err_msg = item.get("error") or platform_errors.get(raw_p) or platform_errors.get(p)
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status=p_status,
                        post_id=str(item.get("id") or item.get("postId") or ""),
                        url=item.get("postUrl") or item.get("url"),
                        error=err_msg,
                    )

        # Secondary schema: "platforms" dict: {"platforms": {"tiktok": {"status": "success", ...}}}
        elif "platforms" in raw and isinstance(raw["platforms"], dict):
            for raw_p, pdata in raw["platforms"].items():
                p = "x" if raw_p == "twitter" and "x" in request.platforms else raw_p
                if isinstance(pdata, dict):
                    status_str = pdata.get("status", "success").lower()
                    p_status = success_status if status_str in ("success", "ok", "published") else "failed"
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status=p_status,
                        post_id=str(pdata.get("id") or pdata.get("post_id") or ""),
                        url=pdata.get("url") or pdata.get("postUrl"),
                        error=pdata.get("error") or platform_errors.get(raw_p),
                    )

        # Fill any missing platforms from request
        for p in request.platforms:
            if p not in results:
                ayr_alias = PLATFORM_TO_AYRSHARE.get(p, p)
                if p in platform_errors or ayr_alias in platform_errors:
                    results[p] = PlatformPublishResult(
                        platform=p,
                        status="failed",
                        error=platform_errors.get(p) or platform_errors.get(ayr_alias),
                    )
                elif raw.get("status") in ("success", "ok", True) or "id" in raw:
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

        success_count = sum(
            1 for r in results.values() if r.status in ("published", "draft_created", "pending")
        )
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
    """Hermetic in-memory stub client simulating Ayrshare behavior for unit testing and local dry runs."""

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
        """Simulate Ayrshare multi-platform publishing."""
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

        success_count = sum(
            1 for r in results.values() if r.status in ("published", "draft_created", "pending")
        )
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

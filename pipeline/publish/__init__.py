"""Multi-platform publishing package."""

from __future__ import annotations

from pipeline.publish.client import (
    AYRSHARE_TO_PLATFORM,
    PLATFORM_TO_AYRSHARE,
    MultiPlatformPublishClient,
    PlatformPublishResult,
    PublishClient,
    PublishRequest,
    PublishResponse,
    StubPublishClient,
)
from pipeline.publish.service import (
    PublishExecutionResult,
    PublishService,
)
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

__all__ = [
    "AYRSHARE_TO_PLATFORM",
    "CaptionTemplater",
    "DEFAULT_HASHTAGS",
    "MediaUploader",
    "MultiPlatformPublishClient",
    "PLATFORM_MAX_CAPTION_LENGTHS",
    "PLATFORM_TO_AYRSHARE",
    "PlatformPublishResult",
    "PublicUrlMediaUploader",
    "PublishClient",
    "PublishExecutionResult",
    "PublishRequest",
    "PublishResponse",
    "PublishService",
    "S3MediaUploader",
    "StubMediaUploader",
    "StubPublishClient",
]

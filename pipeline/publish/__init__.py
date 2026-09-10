"""Multi-platform publishing package."""

from __future__ import annotations

from pipeline.publish.client import (
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

__all__ = [
    "CaptionTemplater",
    "DEFAULT_HASHTAGS",
    "MultiPlatformPublishClient",
    "PLATFORM_MAX_CAPTION_LENGTHS",
    "PlatformPublishResult",
    "PublishClient",
    "PublishExecutionResult",
    "PublishRequest",
    "PublishResponse",
    "PublishService",
    "StubPublishClient",
]

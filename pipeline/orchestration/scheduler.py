"""Posting window and rate limit scheduler for pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pipeline.logging import get_logger


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """Outcome of evaluating posting window and rate limit constraints."""

    can_post: bool
    wait_seconds: float
    reason: str


class PostingWindowScheduler:
    """Enforce allowed time-of-day posting windows and inter-post rate limits."""

    def __init__(
        self,
        enabled: bool = False,
        start_hour: int = 9,
        end_hour: int = 23,
        timezone_name: str = "UTC",
        min_interval_seconds: int = 1800,
    ) -> None:
        if not (0 <= start_hour <= 23):
            raise ValueError(f"start_hour must be in 0..23, got {start_hour}")
        if not (0 <= end_hour <= 23):
            raise ValueError(f"end_hour must be in 0..23, got {end_hour}")
        if min_interval_seconds < 0:
            raise ValueError(f"min_interval_seconds must be >= 0, got {min_interval_seconds}")

        self.enabled = enabled
        self.start_hour = start_hour
        self.end_hour = end_hour
        self.timezone_name = timezone_name
        self.min_interval_seconds = min_interval_seconds
        self.logger = get_logger(__name__, service="orchestration.scheduler")

        try:
            self._tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            self.logger.warning(
                "Unrecognized timezone name; falling back to UTC",
                extra={"timezone": timezone_name},
            )
            self._tz = timezone.utc

    def _normalize_dt(self, dt: datetime | None) -> datetime:
        if dt is None:
            return datetime.now(self._tz)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc).astimezone(self._tz)
        return dt.astimezone(self._tz)

    def is_within_posting_window(self, dt: datetime | None = None) -> bool:
        """Check if the given datetime is within the configured daily posting window."""
        if not self.enabled:
            return True

        local_dt = self._normalize_dt(dt)
        current_hour = local_dt.hour + (local_dt.minute / 60.0) + (local_dt.second / 3600.0)

        if self.start_hour == self.end_hour:
            return True

        if self.start_hour < self.end_hour:
            # e.g., 09:00 to 23:00
            return self.start_hour <= current_hour < self.end_hour
        else:
            # Overnight window spanning midnight, e.g., 22:00 to 06:00
            return current_hour >= self.start_hour or current_hour < self.end_hour

    def seconds_until_next_window(self, dt: datetime | None = None) -> float:
        """Calculate seconds until the posting window next opens. Returns 0.0 if open."""
        if not self.enabled or self.is_within_posting_window(dt):
            return 0.0

        local_dt = self._normalize_dt(dt)

        if self.start_hour < self.end_hour:
            current_hour = local_dt.hour + (local_dt.minute / 60.0)
            if current_hour < self.start_hour:
                # Window opens later today
                next_open = local_dt.replace(
                    hour=self.start_hour, minute=0, second=0, microsecond=0
                )
            else:
                # Window opens tomorrow morning
                tomorrow = local_dt + timedelta(days=1)
                next_open = tomorrow.replace(
                    hour=self.start_hour, minute=0, second=0, microsecond=0
                )
        else:
            # Overnight window spanning midnight: start_hour > end_hour
            # Outside means: end_hour <= current_hour < start_hour
            next_open = local_dt.replace(
                hour=self.start_hour, minute=0, second=0, microsecond=0
            )

        wait_seconds = max(0.0, (next_open - local_dt).total_seconds())
        return wait_seconds

    def check_rate_limit(
        self,
        last_published_at: datetime | None,
        now: datetime | None = None,
    ) -> float:
        """Return remaining seconds until the rate-limit interval expires, or 0.0 if clear."""
        if last_published_at is None or self.min_interval_seconds <= 0:
            return 0.0

        now_dt = self._normalize_dt(now)
        last_dt = self._normalize_dt(last_published_at)

        elapsed = (now_dt - last_dt).total_seconds()
        remaining = self.min_interval_seconds - elapsed
        return max(0.0, remaining)

    def evaluate(
        self,
        last_published_at: datetime | None = None,
        now: datetime | None = None,
    ) -> ScheduleDecision:
        """Evaluate both posting window and rate limit constraints."""
        current_dt = self._normalize_dt(now)

        if not self.is_within_posting_window(current_dt):
            wait_seconds = self.seconds_until_next_window(current_dt)
            reason = (
                f"Outside posting window ({self.start_hour:02d}:00 - "
                f"{self.end_hour:02d}:00 {self.timezone_name})"
            )
            return ScheduleDecision(
                can_post=False,
                wait_seconds=wait_seconds,
                reason=reason,
            )

        rate_limit_wait = self.check_rate_limit(last_published_at, current_dt)
        if rate_limit_wait > 0:
            reason = (
                f"Rate limit active: must wait {rate_limit_wait:.1f}s "
                f"({self.min_interval_seconds}s min interval)"
            )
            return ScheduleDecision(
                can_post=False,
                wait_seconds=rate_limit_wait,
                reason=reason,
            )

        return ScheduleDecision(
            can_post=True,
            wait_seconds=0.0,
            reason="Ready to publish",
        )

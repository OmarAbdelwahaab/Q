"""Monitoring service for pipeline status reporting and automated health checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from pipeline.alerts import AlertService, CompositeAlertService
from pipeline.config import MonitoringSettings
from pipeline.logging import get_logger
from pipeline.state.repository import PipelineStateRepository


@dataclass(slots=True)
class MonitoringReport:
    """Consolidated operational metrics report across all pipeline stages."""

    window_hours: int | None
    total_messages: int
    stages: dict[str, dict[str, int]]
    review_queue_count: int
    active_processing_count: int
    consecutive_failures: int
    latest_published_at: datetime | None
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        d = asdict(self)
        if self.latest_published_at:
            d["latest_published_at"] = self.latest_published_at.isoformat()
        d["generated_at"] = self.generated_at.isoformat()
        # Ensure failure datetime objects are string formatted
        for f in d.get("recent_failures", []):
            if "updated_at_dt" in f:
                f.pop("updated_at_dt", None)
        return d


@dataclass(slots=True)
class HealthCheckResult:
    """Outcome of an automated health evaluation against configured thresholds."""

    is_healthy: bool
    consecutive_failures: int
    failure_threshold: int
    failure_threshold_breached: bool
    review_queue_count: int
    backlog_threshold: int
    backlog_threshold_breached: bool
    reasons: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert health check result to JSON-serializable dictionary."""
        d = asdict(self)
        d["checked_at"] = self.checked_at.isoformat()
        return d


class PipelineMonitor:
    """Monitor pipeline health, generate status reports, and trigger automated alerts."""

    def __init__(
        self,
        state_repository: PipelineStateRepository,
        alert_service: AlertService | None = None,
        settings: MonitoringSettings | None = None,
    ) -> None:
        self.state_repo = state_repository
        self.settings = settings or MonitoringSettings.from_env()
        self.alert_service = alert_service or CompositeAlertService(
            webhook_url=self.settings.alert_webhook_url,
            telegram_bot_token=self.settings.alert_telegram_bot_token,
            telegram_chat_id=self.settings.alert_telegram_chat_id,
        )
        self.logger = get_logger(__name__, service="monitoring")
        self._last_alert_time: dict[str, datetime] = {}

    def generate_report(self, window_hours: int | None = None) -> MonitoringReport:
        """Query state store and construct a comprehensive MonitoringReport."""
        hours = window_hours if window_hours is not None else self.settings.window_hours
        summary = self.state_repo.get_pipeline_summary(window_hours=hours)
        consecutive_failures = self.state_repo.count_consecutive_failures()
        recent_failures = self.state_repo.fetch_recent_failures(
            limit=self.settings.failure_threshold * 2, window_hours=hours
        )

        return MonitoringReport(
            window_hours=hours,
            total_messages=summary["total_messages"],
            stages=summary["stages"],
            review_queue_count=summary["review_queue_count"],
            active_processing_count=summary["active_processing_count"],
            consecutive_failures=consecutive_failures,
            latest_published_at=summary["latest_published_at"],
            recent_failures=recent_failures,
        )

    def format_text_report(self, report: MonitoringReport) -> str:
        """Format monitoring report as clean Markdown text suitable for Telegram / chat alerts."""
        window_str = f"Past {report.window_hours}h" if report.window_hours else "All-Time"
        last_pub_str = (
            report.latest_published_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if report.latest_published_at
            else "None recorded"
        )

        lines = [
            f"📊 *Quran Pipeline Status Report* ({window_str})",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"• *Total Messages Processed*: {report.total_messages}",
            f"• *Active Processing*: {report.active_processing_count}",
            f"• *QA Review Backlog*: {report.review_queue_count} held",
            f"• *Consecutive Failures*: {report.consecutive_failures}",
            f"• *Latest Publication*: {last_pub_str}",
            "",
            "📈 *Stage Breakdown*:",
        ]

        if not report.stages:
            lines.append("  (No stage records found)")
        else:
            ordered_stages = [
                "ingestion",
                "audio",
                "recognition",
                "alignment",
                "qa_gate",
                "render",
                "publish",
                "orchestration",
            ]
            displayed_stages = [s for s in ordered_stages if s in report.stages] + [
                s for s in report.stages if s not in ordered_stages
            ]

            for stg in displayed_stages:
                status_dict = report.stages[stg]
                parts = [f"{status}: {count}" for status, count in sorted(status_dict.items())]
                summary_parts = ", ".join(parts)
                lines.append(f"  • *{stg.capitalize()}*: {summary_parts}")

        if report.recent_failures:
            lines.append("")
            lines.append("⚠️ *Recent Failures*:")
            for f in report.recent_failures[:5]:
                err = (f.get("error") or "Unknown error").strip().replace("\n", " ")
                if len(err) > 80:
                    err = err[:77] + "..."
                lines.append(
                    f"  • Msg #{f.get('message_id')} [{f.get('stage')}]: {err}"
                )

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    def format_json_report(self, report: MonitoringReport) -> dict[str, Any]:
        """Format monitoring report as structured JSON dictionary."""
        return report.to_dict()

    def check_health(self) -> HealthCheckResult:
        """Evaluate pipeline state against configured repeated-failure and backlog thresholds."""
        consecutive_failures = self.state_repo.count_consecutive_failures()
        review_count = self.state_repo.fetch_review_queue_count()

        failure_breached = consecutive_failures >= self.settings.failure_threshold
        backlog_breached = review_count > self.settings.backlog_threshold

        reasons: list[str] = []
        if failure_breached:
            reasons.append(
                f"Repeated failures detected: {consecutive_failures} consecutive failed runs "
                f"(threshold: {self.settings.failure_threshold})"
            )
        if backlog_breached:
            reasons.append(
                f"QA review backlog growing: {review_count} items currently held for review "
                f"(threshold: {self.settings.backlog_threshold})"
            )

        is_healthy = not (failure_breached or backlog_breached)
        return HealthCheckResult(
            is_healthy=is_healthy,
            consecutive_failures=consecutive_failures,
            failure_threshold=self.settings.failure_threshold,
            failure_threshold_breached=failure_breached,
            review_queue_count=review_count,
            backlog_threshold=self.settings.backlog_threshold,
            backlog_threshold_breached=backlog_breached,
            reasons=reasons,
        )

    def _should_suppress_alert(self, alert_key: str, now: datetime) -> bool:
        """Check if an alert of the given type was dispatched recently within cooldown."""
        last_time = self._last_alert_time.get(alert_key)
        if last_time is None:
            return False
        elapsed = (now - last_time).total_seconds()
        return elapsed < self.settings.alert_cooldown_seconds

    async def dispatch_health_alerts(self) -> list[str]:
        """Evaluate health and dispatch alerts for any breached thresholds respecting cooldown."""
        health = self.check_health()
        if health.is_healthy:
            return []

        now = datetime.now(timezone.utc)
        dispatched_alerts: list[str] = []

        if health.failure_threshold_breached:
            key = "repeated_failures"
            if not self._should_suppress_alert(key, now):
                msg = (
                    f"🚨 *CRITICAL PIPELINE HEALTH ALERT: Repeated Failures*\n"
                    f"The pipeline has experienced {health.consecutive_failures} consecutive failures "
                    f"(alert threshold is {health.failure_threshold}).\n"
                    f"Investigate recent logs or execute `/status` for diagnostic details."
                )
                if self.alert_service:
                    await self.alert_service.send_monitoring_alert(
                        msg, payload={"event": key, "consecutive_failures": health.consecutive_failures}
                    )
                self._last_alert_time[key] = now
                dispatched_alerts.append(msg)
            else:
                self.logger.info("Suppressed repeated failure alert due to active cooldown window")

        if health.backlog_threshold_breached:
            key = "review_backlog"
            if not self._should_suppress_alert(key, now):
                msg = (
                    f"⚠️ *PIPELINE HEALTH ALERT: Growing Review Backlog*\n"
                    f"The QA Gate review queue currently has {health.review_queue_count} held items "
                    f"(alert threshold is {health.backlog_threshold}).\n"
                    f"Action required: inspect held items with `/queue` and approve or re-run with `--force`."
                )
                if self.alert_service:
                    await self.alert_service.send_monitoring_alert(
                        msg, payload={"event": key, "review_queue_count": health.review_queue_count}
                    )
                self._last_alert_time[key] = now
                dispatched_alerts.append(msg)
            else:
                self.logger.info("Suppressed review queue backlog alert due to active cooldown window")

        return dispatched_alerts

    async def send_status_report(self, window_hours: int | None = None) -> str:
        """Generate and dispatch formatted status report to alert channels."""
        report = self.generate_report(window_hours=window_hours)
        text = self.format_text_report(report)
        if self.alert_service:
            await self.alert_service.send_status_report(
                text, payload={"event": "status_report", "window_hours": report.window_hours}
            )
        return text

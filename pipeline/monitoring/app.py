"""Command-line interface for pipeline status reporting and health checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Sequence

from pipeline.alerts import CompositeAlertService
from pipeline.config import MonitoringSettings
from pipeline.logging import configure_logging, get_logger
from pipeline.monitoring.service import PipelineMonitor
from pipeline.state.repository import PipelineStateRepository


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for monitoring CLI."""
    parser = argparse.ArgumentParser(
        description="Quran Video Pipeline Monitoring & Health Inspection Tool."
    )
    subparsers = parser.add_subparsers(dest="command", help="Monitoring command to execute")

    # 'report' subcommand
    report_parser = subparsers.add_parser(
        "report", help="Generate and display pipeline operational report"
    )
    report_parser.add_argument(
        "--window-hours",
        type=int,
        default=None,
        help="Time window in hours to summarize (default: from env or 24h)",
    )
    report_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON instead of human-readable text",
    )
    report_parser.add_argument(
        "--send-alert",
        action="store_true",
        default=False,
        help="Dispatch report to configured alert channels (Telegram/Webhook)",
    )

    # 'check' subcommand
    check_parser = subparsers.add_parser(
        "check", help="Evaluate health thresholds (repeated failures, review backlog)"
    )
    check_parser.add_argument(
        "--failure-threshold",
        type=int,
        default=None,
        help="Override consecutive failure threshold",
    )
    check_parser.add_argument(
        "--backlog-threshold",
        type=int,
        default=None,
        help="Override review queue backlog threshold",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output health result as structured JSON",
    )
    check_parser.add_argument(
        "--send-alert",
        action="store_true",
        default=False,
        help="Dispatch alerts if health check fails/breaches thresholds",
    )

    return parser.parse_args(argv)


def build_monitor(settings: MonitoringSettings | None = None) -> PipelineMonitor:
    """Construct PipelineMonitor from settings and state database."""
    mon_settings = settings or MonitoringSettings.from_env()
    if mon_settings.state_database_url:
        state_repo = PipelineStateRepository(database_url=mon_settings.state_database_url)
    else:
        state_repo = PipelineStateRepository(database_path=mon_settings.state_db_path)

    alert_service = CompositeAlertService(
        webhook_url=mon_settings.alert_webhook_url,
        telegram_bot_token=mon_settings.alert_telegram_bot_token,
        telegram_chat_id=mon_settings.alert_telegram_chat_id,
    )
    return PipelineMonitor(
        state_repository=state_repo,
        alert_service=alert_service,
        settings=mon_settings,
    )


async def cli_main(argv: Sequence[str] | None = None) -> int:
    """Async main entrypoint for monitoring CLI."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args(argv)
    settings = MonitoringSettings.from_env()
    configure_logging(level=settings.log_level)
    logger = get_logger(__name__, service="monitoring.cli")

    command = args.command or "report"
    monitor = build_monitor(settings)

    try:
        if command == "report":
            window = getattr(args, "window_hours", None)
            report = monitor.generate_report(window_hours=window)

            if getattr(args, "json", False):
                print(json.dumps(monitor.format_json_report(report), indent=2, ensure_ascii=False))
            else:
                print(monitor.format_text_report(report))

            if getattr(args, "send_alert", False):
                await monitor.send_status_report(window_hours=window)
                logger.info("Dispatched status report to alert channels")
            return 0

        elif command == "check":
            if getattr(args, "failure_threshold", None) is not None:
                monitor.settings.failure_threshold = args.failure_threshold
            if getattr(args, "backlog_threshold", None) is not None:
                monitor.settings.backlog_threshold = args.backlog_threshold

            health = monitor.check_health()

            if getattr(args, "send_alert", False):
                sent = await monitor.dispatch_health_alerts()
                if sent:
                    logger.warning("Dispatched health alerts", extra={"alerts_count": len(sent)})

            if getattr(args, "json", False):
                print(json.dumps(health.to_dict(), indent=2, ensure_ascii=False))
            else:
                if health.is_healthy:
                    print("✅ Pipeline is healthy. Zero threshold breaches.")
                else:
                    print("🚨 Pipeline health issues detected:")
                    for r in health.reasons:
                        print(f"  • {r}")

            return 0 if health.is_healthy else 2

        else:
            logger.error(f"Unknown command: {command}")
            return 1

    except Exception as exc:
        logger.exception("Monitoring command failed", extra={"error": str(exc)})
        return 1
    finally:
        monitor.state_repo.close()


def main() -> None:
    """Synchronous entrypoint for python -m pipeline.monitoring.app."""
    code = asyncio.run(cli_main(sys.argv[1:]))
    sys.exit(code)


if __name__ == "__main__":
    main()

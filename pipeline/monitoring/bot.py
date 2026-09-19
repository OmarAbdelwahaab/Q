"""Chat-bot command handling for interactive pipeline administration."""

from __future__ import annotations

import re
from typing import Any

from pipeline.monitoring.service import PipelineMonitor


class MonitoringBotHandler:
    """Interprets and executes chat-bot commands (Telegram/Slack) for pipeline monitoring."""

    def __init__(self, monitor: PipelineMonitor) -> None:
        self.monitor = monitor

    def handle_command(self, command_text: str) -> str:
        """Parse chat command and return formatted markdown reply."""
        text = command_text.strip()
        parts = text.split()
        if not parts:
            return self._help()

        cmd = parts[0].lower()
        # Handle command with bot mention (e.g. /status@MyBot)
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        if cmd in ("/status", "status"):
            window_hours = None
            if len(parts) > 1:
                try:
                    window_hours = int(parts[1])
                except ValueError:
                    pass
            return self._handle_status(window_hours)

        if cmd in ("/health", "health"):
            return self._handle_health()

        if cmd in ("/queue", "queue"):
            return self._handle_queue()

        if cmd in ("/retry", "retry"):
            if len(parts) < 2:
                return "⚠️ *Usage*: `/retry <message_id>`\nExample: `/retry 105`"
            return self._handle_retry(parts[1])

        if cmd in ("/help", "help", "/start", "start"):
            return self._help()

        return f"❓ Unknown command `{cmd}`.\n\n" + self._help()

    def _handle_status(self, window_hours: int | None) -> str:
        report = self.monitor.generate_report(window_hours=window_hours)
        return self.monitor.format_text_report(report)

    def _handle_health(self) -> str:
        health = self.monitor.check_health()
        checked_str = health.checked_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if health.is_healthy:
            return (
                f"✅ *Pipeline System Status: HEALTHY*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Consecutive Failures: {health.consecutive_failures}/{health.failure_threshold}\n"
                f"• Review Queue Backlog: {health.review_queue_count}/{health.backlog_threshold}\n"
                f"• Timestamp: {checked_str}\n"
                f"All systems operational."
            )

        reasons_list = "\n".join(f"  • {r}" for r in health.reasons)
        return (
            f"🚨 *Pipeline System Status: ATTENTION REQUIRED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{reasons_list}\n\n"
            f"• Consecutive Failures: {health.consecutive_failures} (Threshold: {health.failure_threshold})\n"
            f"• Review Queue Backlog: {health.review_queue_count} (Threshold: {health.backlog_threshold})\n"
            f"• Timestamp: {checked_str}\n\n"
            f"Run `/status` for full breakdown or `/queue` to inspect held items."
        )

    def _handle_queue(self) -> str:
        items = self.monitor.state_repo.fetch_review_queue_items(limit=20)
        if not items:
            return "✅ *QA Review Queue is Empty*\nNo items are currently held for manual review."

        lines = [
            f"📋 *QA Gate Review Queue ({len(items)} items)*",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for it in items:
            mid = it.get("message_id")
            up = it.get("updated_at")
            err = (it.get("error") or "Held by QA Gate thresholds").strip().replace("\n", " ")
            lines.append(f"• *Msg #{mid}* ({up}): {err}")

        lines.append("")
        lines.append("To approve and re-run an item, execute:")
        lines.append("`/retry <message_id>`")
        return "\n".join(lines)

    def _handle_retry(self, message_id_str: str) -> str:
        try:
            mid = int(message_id_str)
        except ValueError:
            return f"❌ Invalid message ID `{message_id_str}`. Must be an integer."

        row = self.monitor.state_repo.fetch_stage(mid, "qa_gate")
        status = row.get("status") if row else "unknown"

        return (
            f"🔄 *Reprocess Instructions for Message #{mid}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Current QA Gate Status: `{status}`\n\n"
            f"To re-run with operator override, execute in the pipeline shell or container:\n"
            f"```bash\n"
            f"python -m pipeline.orchestration.app {mid} --force\n"
            f"```\n"
            f"If an active worker crashed recently (<300s), add `--force-active`."
        )

    def _help(self) -> str:
        return (
            "🤖 *Quran Pipeline Admin Bot Commands*:\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "• `/status [hours]` — Display operational report (default: 24h)\n"
            "• `/health` — Check system health & breach thresholds\n"
            "• `/queue` — List items currently held in QA review queue\n"
            "• `/retry <id>` — Get reprocess commands for a specific item\n"
            "• `/help` — Show this command help list"
        )

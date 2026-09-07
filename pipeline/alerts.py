"""Alerting helpers for pipeline failures."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from pipeline.logging import get_logger


class AlertService(Protocol):
    async def send_ingestion_failure(self, message: str) -> None:
        """Send an alert for an ingestion failure."""

    async def send_audio_extraction_failure(self, message: str) -> None:
        """Send an alert for an audio extraction failure."""


class CompositeAlertService:
    """Dispatch alerts to any configured channels and always log them."""

    def __init__(
        self,
        webhook_url: str | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.logger = get_logger(__name__, service="alerts")

    async def send_ingestion_failure(self, message: str) -> None:
        self._dispatch("ingestion_failure", message)

    async def send_audio_extraction_failure(self, message: str) -> None:
        self._dispatch("audio_extraction_failure", message)

    def _dispatch(self, event: str, message: str) -> None:
        self.logger.error("Pipeline failure alert triggered", extra={"event": event, "alert_message": message})

        if self.webhook_url:
            self._post_json(self.webhook_url, {"text": message, "event": event})

        if self.telegram_bot_token and self.telegram_chat_id:
            encoded_message = urllib.parse.urlencode(
                {"chat_id": self.telegram_chat_id, "text": message}
            ).encode("utf-8")
            url = (
                f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            )
            self._post_form(url, encoded_message)

    def _post_json(self, url: str, payload: dict[str, object]) -> None:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        self._send(request)

    def _post_form(self, url: str, payload: bytes) -> None:
        request = urllib.request.Request(
            url=url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self._send(request)

    def _send(self, request: urllib.request.Request) -> None:
        try:
            with urllib.request.urlopen(request, timeout=10):
                return
        except urllib.error.URLError as exc:
            self.logger.warning(
                "Alert delivery failed",
                extra={"alert_target": request.full_url, "error": str(exc)},
            )

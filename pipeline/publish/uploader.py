"""Media uploader components for publishing rendered videos to public URLs."""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pipeline.logging import get_logger


class MediaUploader(Protocol):
    """Protocol for uploading local video artifacts to a publicly accessible HTTP(S) URL."""

    async def upload_media(self, local_path: Path, message_id: int) -> str:
        """Upload or resolve public media URL for a rendered video."""
        ...


class PublicUrlMediaUploader:
    """Format a public URL when local artifacts are served via CDN or reverse proxy."""

    def __init__(self, public_base_url: str) -> None:
        self.public_base_url = public_base_url.rstrip("/")
        self.logger = get_logger(__name__, service="public_url_uploader")

    async def upload_media(self, local_path: Path, message_id: int) -> str:
        filename = local_path.name
        public_url = f"{self.public_base_url}/{filename}"
        self.logger.info(
            "Resolved public media URL via base URL",
            extra={"message_id": message_id, "public_url": public_url},
        )
        return public_url


class S3MediaUploader:
    """Upload media file to an S3 or MinIO bucket and return public URL."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.logger = get_logger(__name__, service="s3_uploader")

    async def upload_media(self, local_path: Path, message_id: int) -> str:
        filename = local_path.name
        # If public base URL is explicitly given (e.g. CloudFront/CDN), use it; otherwise fallback to endpoint/bucket
        if self.public_base_url:
            public_url = f"{self.public_base_url}/{filename}"
        else:
            public_url = f"{self.endpoint}/{self.bucket}/{filename}"

        upload_url = f"{self.endpoint}/{self.bucket}/{filename}"
        self.logger.info(
            "Uploading media to object storage",
            extra={"message_id": message_id, "target": upload_url},
        )

        loop = asyncio.get_running_loop()

        def _do_upload() -> None:
            # Check if boto3 is installed for signed S3 upload
            boto_error: Exception | None = None
            try:
                import boto3  # type: ignore[import-not-found]
                from botocore.client import Config  # type: ignore[import-not-found]

                s3 = boto3.client(
                    "s3",
                    endpoint_url=self.endpoint,
                    aws_access_key_id=self.access_key,
                    aws_secret_access_key=self.secret_key,
                    config=Config(signature_version="s3v4"),
                )
                s3.upload_file(
                    str(local_path),
                    self.bucket,
                    filename,
                    ExtraArgs={"ContentType": "video/mp4"},
                )
                return
            except Exception as exc:
                boto_error = exc
                self.logger.warning(
                    "Boto3 S3 upload failed or credentials unavailable; attempting HTTP PUT fallback",
                    extra={"error": str(exc)},
                )

            # Fallback: direct HTTP PUT for unauthenticated/MinIO test bucket
            file_bytes = local_path.read_bytes()
            headers = {"Content-Type": "video/mp4"}
            req = urllib.request.Request(upload_url, data=file_bytes, headers=headers, method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=30):
                    return
            except Exception as exc:
                err_msg = (
                    f"S3 media upload failed for {local_path} to {upload_url}: "
                    f"boto3 error: {boto_error}; HTTP PUT error: {exc}"
                )
                self.logger.error("Media upload failed completely", extra={"error": err_msg})
                raise RuntimeError(err_msg) from exc

        await loop.run_in_executor(None, _do_upload)
        return public_url


class StubMediaUploader:
    """Hermetic in-memory stub media uploader for unit testing and offline dry runs."""

    def __init__(self, base_url: str = "https://cdn.example.com/render") -> None:
        self.base_url = base_url.rstrip("/")
        self.uploaded_files: list[tuple[Path, int, str]] = []

    async def upload_media(self, local_path: Path, message_id: int) -> str:
        public_url = f"{self.base_url}/{local_path.name}"
        self.uploaded_files.append((local_path, message_id, public_url))
        return public_url

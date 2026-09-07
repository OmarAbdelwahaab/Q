"""Minimal OpenAI-compatible Whisper transcription client."""

from __future__ import annotations

import json
import mimetypes
import uuid
import urllib.error
import urllib.request
from pathlib import Path


class TranscriptionError(RuntimeError):
    pass


class WhisperTranscriber:
    """Call a compatible ``/audio/transcriptions`` endpoint for Arabic ASR."""

    def __init__(self, api_url: str, api_key: str | None, model_name: str) -> None:
        self.api_url, self.api_key, self.model_name = api_url, api_key, model_name

    def transcribe(self, audio_path: Path) -> str:
        if not self.api_url:
            raise TranscriptionError("ASR_API_URL is required to transcribe audio.")
        if not audio_path.is_file():
            raise TranscriptionError(f"Audio artifact does not exist: {audio_path}")
        boundary = f"----pipeline-{uuid.uuid4().hex}"
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.api_url, data=self._multipart(boundary, audio_path), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise TranscriptionError(f"ASR request failed: {exc}") from exc
        if not isinstance(result.get("text"), str) or not result["text"].strip():
            raise TranscriptionError("ASR response did not contain transcription text.")
        return result["text"].strip()

    def _multipart(self, boundary: str, audio_path: Path) -> bytes:
        def field(name: str, value: str) -> bytes:
            return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        content_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
        file_header = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        return b"".join([field("model", self.model_name), field("language", "ar"), file_header, audio_path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode()])

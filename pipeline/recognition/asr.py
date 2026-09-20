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
        if not self.api_key and not any(h in self.api_url for h in ("localhost", "127.0.0.1", "testserver")):
            raise TranscriptionError(
                "ASR_API_KEY is not configured. A valid API key is required to call cloud ASR endpoints. "
                "Please configure ASR_API_KEY in your environment or GitHub Secrets."
            )
        boundary = f"----pipeline-{uuid.uuid4().hex}"
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "QuranPipeline/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.api_url, data=self._multipart(boundary, audio_path), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw_body = exc.read()
                if raw_body:
                    try:
                        error_json = json.loads(raw_body.decode("utf-8", errors="replace"))
                        if isinstance(error_json, dict) and "error" in error_json:
                            err_info = error_json["error"]
                            if isinstance(err_info, dict) and "message" in err_info:
                                detail = err_info["message"]
                            else:
                                detail = str(err_info)
                        else:
                            detail = str(error_json)
                    except Exception:
                        detail = raw_body.decode("utf-8", errors="replace").strip()[:500]
            except Exception:
                pass

            msg = f"ASR HTTP Error {exc.code} ({exc.reason})"
            if detail:
                msg += f": {detail}"
            if exc.code == 401:
                msg += " - Verify that ASR_API_KEY is correctly set in your environment / GitHub Secrets."
            elif exc.code == 403:
                msg += " - Access forbidden. Verify your ASR account status, permissions, or API key quota."
            elif exc.code == 429:
                msg += " - Rate limit reached on ASR provider."
            raise TranscriptionError(msg) from exc
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

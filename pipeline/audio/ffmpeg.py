"""Safe, testable ffmpeg/ffprobe integration for normalized WAV extraction."""

from __future__ import annotations

import json
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class AudioExtractionError(RuntimeError):
    """Base error for media that cannot safely enter the ASR pipeline."""


class AudioToolUnavailableError(AudioExtractionError):
    """ffmpeg or ffprobe is unavailable on the worker."""


class MissingAudioTrackError(AudioExtractionError):
    """The input video has no usable audio stream."""


class CorruptMediaError(AudioExtractionError):
    """The input cannot be probed or decoded as a valid media file."""


class ShortAudioError(AudioExtractionError):
    """The audio duration is below the configured minimum."""


@dataclass(frozen=True, slots=True)
class AudioDetails:
    """Verified characteristics of a normalized WAV file."""

    duration_seconds: float
    sample_rate: int
    channels: int


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a media command without a shell, preserving paths with spaces safely."""
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AudioToolUnavailableError(f"Media tool not found: {command[0]}") from exc


class FFmpegAudioExtractor:
    """Extract the first audio stream as 16 kHz mono PCM WAV.

    The wrapper probes first so missing tracks are classified distinctly from corrupt
    media. Video decoding is disabled during extraction, reducing CPU and I/O work.
    """

    def __init__(
        self,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        minimum_duration_seconds: float = 0.25,
        runner: CommandRunner = run_command,
    ) -> None:
        if minimum_duration_seconds < 0:
            raise ValueError("minimum_duration_seconds must be zero or greater.")
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.minimum_duration_seconds = minimum_duration_seconds
        self.runner = runner

    def extract(self, source: Path, destination: Path) -> AudioDetails:
        """Probe, normalize, validate, then atomically publish a WAV artifact."""
        if not source.is_file():
            raise CorruptMediaError(f"Source video does not exist: {source}")

        source_duration = self._probe_audio(source)
        self._ensure_minimum_duration(source_duration)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_destination = destination.with_suffix(".partial.wav")
        try:
            temporary_destination.unlink(missing_ok=True)
            result = self.runner(
                [
                    self.ffmpeg_binary,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-threads",
                    "0",
                    str(temporary_destination),
                ]
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
                raise CorruptMediaError(f"ffmpeg could not extract audio: {detail}")
            if not temporary_destination.is_file():
                raise CorruptMediaError("ffmpeg completed without producing a WAV file.")

            details = self._validate_wav(temporary_destination)
            self._ensure_minimum_duration(details.duration_seconds)
            temporary_destination.replace(destination)
            return details
        except Exception:
            temporary_destination.unlink(missing_ok=True)
            raise

    def _probe_audio(self, source: Path) -> float | None:
        result = self.runner(
            [
                self.ffprobe_binary,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(source),
            ]
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown ffprobe error").strip()
            raise CorruptMediaError(f"ffprobe could not read source media: {detail}")
        try:
            probe = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise CorruptMediaError("ffprobe returned invalid media metadata.") from exc

        streams = probe.get("streams", [])
        if not streams:
            raise MissingAudioTrackError("Source video has no audio track.")

        duration = probe.get("format", {}).get("duration")
        if duration in (None, "N/A"):
            return None
        try:
            parsed_duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise CorruptMediaError("Source media has an invalid duration.") from exc
        if parsed_duration < 0:
            raise CorruptMediaError("Source media has a negative duration.")
        return parsed_duration

    def _validate_wav(self, wav_path: Path) -> AudioDetails:
        try:
            with wave.open(str(wav_path), "rb") as audio:
                channels = audio.getnchannels()
                sample_rate = audio.getframerate()
                sample_width = audio.getsampwidth()
                frames = audio.getnframes()
        except (wave.Error, EOFError) as exc:
            raise CorruptMediaError("ffmpeg produced an invalid WAV file.") from exc

        if channels != 1 or sample_rate != 16000 or sample_width != 2:
            raise CorruptMediaError(
                "ffmpeg output is not 16 kHz mono 16-bit PCM WAV as required."
            )
        if frames <= 0:
            raise MissingAudioTrackError("Extracted WAV contains no audio samples.")
        return AudioDetails(
            duration_seconds=frames / sample_rate,
            sample_rate=sample_rate,
            channels=channels,
        )

    def _ensure_minimum_duration(self, duration_seconds: float | None) -> None:
        if duration_seconds is not None and duration_seconds < self.minimum_duration_seconds:
            raise ShortAudioError(
                f"Audio is {duration_seconds:.3f}s; minimum is "
                f"{self.minimum_duration_seconds:.3f}s."
            )

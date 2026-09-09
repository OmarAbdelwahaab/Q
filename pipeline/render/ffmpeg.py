"""FFmpeg wrapper for composing 1080x1920 H.264 Quran video edits with karaoke subtitles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class RenderError(RuntimeError):
    """Raised when video composition, encoding, or verification fails."""


@dataclass(frozen=True, slots=True)
class VideoProbeResult:
    width: int
    height: int
    duration_seconds: float
    video_codec: str
    has_audio: bool


def escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape file paths for FFmpeg filtergraph arguments on Windows/POSIX."""
    posix_path = path.resolve().as_posix()
    # Windows drive colons (e.g. C:/) must be escaped as C\:/ in filter expressions
    return posix_path.replace(":", r"\:")


def default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


class FFmpegRenderer:
    """Composes background, recitation audio, ASS karaoke subtitles, and branding overlay."""

    def __init__(
        self,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        target_width: int = 1080,
        target_height: int = 1920,
        runner: CommandRunner | None = None,
    ) -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.target_width = target_width
        self.target_height = target_height
        self.runner = runner or default_runner

    def probe_media(self, media_path: Path) -> VideoProbeResult:
        """Probe media file and extract streams, codec, resolution, and duration."""
        if not media_path.is_file():
            raise RenderError(f"Media file does not exist: {media_path}")

        command = [
            self.ffprobe_binary,
            "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,duration:format=duration",
            "-of", "json",
            str(media_path),
        ]
        try:
            result = self.runner(command)
        except FileNotFoundError as exc:
            raise RenderError(f"ffprobe binary not found: {self.ffprobe_binary}") from exc

        if result.returncode != 0:
            raise RenderError(f"ffprobe failed: {result.stderr.strip()}")

        try:
            payload = json.loads(result.stdout)
            streams = payload.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            duration_val = payload.get("format", {}).get("duration")
            if duration_val is None and video_stream:
                duration_val = video_stream.get("duration")
            if duration_val is None and audio_stream:
                duration_val = audio_stream.get("duration")

            duration = float(duration_val or 0.0)
            width = int(video_stream.get("width", 0)) if video_stream else 0
            height = int(video_stream.get("height", 0)) if video_stream else 0
            video_codec = str(video_stream.get("codec_name", "")) if video_stream else ""

            return VideoProbeResult(
                width=width,
                height=height,
                duration_seconds=duration,
                video_codec=video_codec,
                has_audio=audio_stream is not None,
            )
        except Exception as exc:
            raise RenderError(f"Failed to parse ffprobe output: {exc}") from exc

    def render(
        self,
        audio_path: Path,
        ass_path: Path,
        output_path: Path,
        background_path: Path | None = None,
        branding_logo_path: Path | None = None,
        branding_handle: str | None = None,
        branding_position: str = "top_right",
        max_duration_seconds: float | None = None,
    ) -> Path:
        """Render complete 1080x1920 H.264 MP4 to output_path."""
        if not audio_path.is_file():
            raise RenderError(f"Audio file does not exist: {audio_path}")
        if not ass_path.is_file():
            raise RenderError(f"Subtitle file does not exist: {ass_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial_output = output_path.with_suffix(".partial.mp4")
        if partial_output.exists():
            partial_output.unlink()

        # Build FFmpeg command
        command: list[str] = [self.ffmpeg_binary, "-y"]

        # Background input
        use_procedural_background = background_path is None or not background_path.is_file()
        if use_procedural_background:
            # Procedural dark slate
            command.extend([
                "-f", "lavfi",
                "-i", f"color=c=0x0b0e14:s={self.target_width}x{self.target_height}:r=30",
            ])
        else:
            is_image = background_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            if is_image:
                command.extend(["-loop", "1", "-i", str(background_path)])
            else:
                command.extend(["-stream_loop", "-1", "-i", str(background_path)])

        # Audio input (Input index 1)
        command.extend(["-i", str(audio_path)])

        # Logo input (Input index 2 if present)
        has_logo = branding_logo_path is not None and branding_logo_path.is_file()
        if has_logo:
            command.extend(["-i", str(branding_logo_path)])

        # Construct filtergraph
        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        vf_filters: list[str] = []

        if not use_procedural_background:
            # Scale and crop background to exact 1080x1920 (9:16)
            vf_filters.append(
                f"scale={self.target_width}:{self.target_height}:force_original_aspect_ratio=increase,"
                f"crop={self.target_width}:{self.target_height},"
                f"eq=brightness=-0.05:contrast=1.05"
            )

        # Burn-in ASS subtitles
        vf_filters.append(f"ass=filename='{escaped_ass}'")

        # Watermark handle text
        if branding_handle:
            # Escape handle text
            clean_handle = branding_handle.replace("'", "").replace(":", "")
            x_expr, y_expr = self._get_text_position_expr(branding_position)
            vf_filters.append(
                f"drawtext=text='{clean_handle}':fontsize=32:fontcolor=white@0.85:"
                f"shadowcolor=black@0.6:shadowx=2:shadowy=2:{x_expr}:{y_expr}"
            )

        if has_logo:
            # Complex filter with logo overlay
            base_filter = ",".join(vf_filters)
            overlay_pos = self._get_overlay_position_expr(branding_position)
            filter_complex = (
                f"[0:v]{base_filter}[bg];"
                f"[2:v]scale=160:-1[logo];"
                f"[bg][logo]overlay={overlay_pos}[outv]"
            )
            command.extend(["-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a"])
        else:
            # Simple video filter
            command.extend(["-vf", ",".join(vf_filters), "-map", "0:v", "-map", "1:a"])

        # Audio and video encoding settings
        command.extend([
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "fast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
        ])

        if max_duration_seconds and max_duration_seconds > 0:
            command.extend(["-t", str(max_duration_seconds)])

        command.append(str(partial_output))

        try:
            result = self.runner(command)
        except FileNotFoundError as exc:
            raise RenderError(f"ffmpeg binary not found: {self.ffmpeg_binary}") from exc

        if result.returncode != 0:
            if partial_output.exists():
                partial_output.unlink()
            err = (result.stderr or result.stdout or "ffmpeg execution failed").strip()
            raise RenderError(f"FFmpeg render failed: {err}")

        if not partial_output.is_file() or partial_output.stat().st_size == 0:
            raise RenderError("FFmpeg produced an empty or missing output file.")

        # Probe and verify output
        probe = self.probe_media(partial_output)
        if probe.width != self.target_width or probe.height != self.target_height:
            partial_output.unlink()
            raise RenderError(
                f"Rendered video resolution mismatch: expected {self.target_width}x{self.target_height}, "
                f"got {probe.width}x{probe.height}"
            )

        if probe.duration_seconds <= 0:
            partial_output.unlink()
            raise RenderError("Rendered video duration is 0 seconds.")

        # Atomic rename
        if output_path.exists():
            output_path.unlink()
        partial_output.replace(output_path)
        return output_path

    @staticmethod
    def _get_overlay_position_expr(position: str) -> str:
        pos = position.lower()
        if pos == "top_left":
            return "x=60:y=60"
        if pos == "bottom_right":
            return "x=W-w-60:y=H-h-120"
        if pos == "bottom_left":
            return "x=60:y=H-h-120"
        if pos == "bottom_center":
            return "x=(W-w)/2:y=H-h-120"
        # Default: top_right
        return "x=W-w-60:y=60"

    @staticmethod
    def _get_text_position_expr(position: str) -> tuple[str, str]:
        pos = position.lower()
        if pos == "top_left":
            return ("x=60", "y=60")
        if pos == "bottom_right":
            return ("x=w-text_w-60", "y=h-text_h-120")
        if pos == "bottom_left":
            return ("x=60", "y=h-text_h-120")
        if pos == "bottom_center":
            return ("x=(w-text_w)/2", "y=h-text_h-120")
        # Default: top_right
        return ("x=w-text_w-60", "y=60")

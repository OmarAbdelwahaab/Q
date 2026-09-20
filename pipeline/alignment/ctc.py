"""Adapter for the documented ctc-forced-aligner command-line contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class AlignmentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlignedWord:
    word: str
    start_ms: int
    end_ms: int


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class CtcForcedAligner:
    """Run CTC alignment in a disposable directory; its JSON sidecar never leaks."""

    def __init__(self, binary: str = "ctc-forced-aligner", model: str = "jonatasgrosman/wav2vec2-large-xlsr-53-arabic", device: str = "cuda", batch_size: int = 4, runner: CommandRunner | None = None) -> None:
        self.binary, self.model, self.device, self.batch_size = binary, model, device, batch_size
        self.runner = runner or self._run

    def align(self, audio_path: Path, canonical_text: str) -> list[AlignedWord]:
        words = canonical_text.split()
        if not words:
            raise AlignmentError("Canonical text contains no words.")
        if not audio_path.is_file():
            raise AlignmentError(f"Audio artifact does not exist: {audio_path}")
        with tempfile.TemporaryDirectory(prefix="quran-align-") as directory:
            work = Path(directory)
            work_audio = work / "audio.wav"
            try:
                os.link(audio_path, work_audio)
            except OSError:
                shutil.copy2(audio_path, work_audio)
            text_path = work / "text.txt"
            text_path.write_text(canonical_text, encoding="utf-8")
            command = [self.binary, "--audio_path", str(work_audio), "--text_path", str(text_path), "--language", "ara", "--split_size", "word", "--alignment_model", self.model, "--device", self.device, "--batch_size", str(self.batch_size)]
            # Unverified pending a real CTC run: upstream documents JSON output but not
            # its location. Only accept a newly created/changed file from likely paths.
            output_candidates = [work_audio.with_suffix(".json"), Path.cwd() / "audio.json"]
            binary_path = shutil.which(self.binary)
            if binary_path:
                output_candidates.append(Path(binary_path).parent / "audio.json")
            before = {path: path.stat().st_mtime_ns if path.is_file() else None for path in output_candidates}
            try:
                result = self.runner(command)
            except FileNotFoundError as exc:
                raise AlignmentError(f"Alignment tool not found: {self.binary}") from exc
            if result.returncode != 0:
                raise AlignmentError((result.stderr or result.stdout or "ctc-forced-aligner failed").strip())
            output = next((path for path in output_candidates if path.is_file() and before[path] != path.stat().st_mtime_ns), None)
            if output is None:
                searched = ", ".join(str(path) for path in output_candidates)
                raise AlignmentError(f"ctc-forced-aligner completed without JSON output; searched: {searched}")
            return self._parse(output, words, self._duration_ms(work_audio))

    @staticmethod
    def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @staticmethod
    def _duration_ms(audio_path: Path) -> int:
        with wave.open(str(audio_path), "rb") as audio:
            return round(audio.getnframes() * 1000 / audio.getframerate())

    @staticmethod
    def _parse(output: Path, words: list[str], duration_ms: int) -> list[AlignedWord]:
        payload = json.loads(output.read_text(encoding="utf-8"))
        segments = payload.get("segments", payload) if isinstance(payload, dict) else payload
        if not isinstance(segments, list) or not segments or len(segments) > len(words):
            raise AlignmentError("Aligner returned an invalid number of word segments.")
        aligned: list[AlignedWord] = []
        previous_end = 0
        for word, segment in zip(words, segments, strict=True):
            try:
                start, end = round(float(segment["start"]) * 1000), round(float(segment["end"]) * 1000)
            except (KeyError, TypeError, ValueError) as exc:
                raise AlignmentError("Aligner JSON has invalid word timing.") from exc
            if start < previous_end or end <= start or end > duration_ms:
                raise AlignmentError("Aligner produced non-monotonic or out-of-duration timestamps.")
            aligned.append(AlignedWord(word, start, end))
            previous_end = end
        return aligned

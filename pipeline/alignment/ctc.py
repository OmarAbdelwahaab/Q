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


# Strict vocabulary of jonatasgrosman/wav2vec2-large-xlsr-53-arabic
WAV2VEC2_ARABIC_VOCAB: frozenset[str] = frozenset({
    '\u0621', '\u0622', '\u0623', '\u0624', '\u0625', '\u0626', '\u0627', '\u0628',
    '\u0629', '\u062a', '\u062b', '\u062c', '\u062d', '\u062e', '\u062f', '\u0630',
    '\u0631', '\u0632', '\u0633', '\u0634', '\u0635', '\u0636', '\u0637', '\u0638',
    '\u0639', '\u063a', '\u0641', '\u0642', '\u0643', '\u0644', '\u0645', '\u0646',
    '\u0647', '\u0648', '\u0649', '\u064a', '\u064b', '\u064c', '\u064d', '\u064e',
    '\u064f', '\u0650', '\u0651', '\u0652',
})

# Mapping Quranic orthography to phonetically equivalent CTC vocabulary tokens
QURAN_CHAR_MAP: dict[str, str] = {
    '\u0671': '\u0627',  # ٱ alef wasla -> ا
    '\u0670': '\u0627',  # ٰ dagger alef -> ا
    '\u06e5': '\u0648',  # ۥ small waw -> و
    '\u06e6': '\u064a',  # ۦ small yeh -> ي
    '\u06e7': '\u064a',  # ۧ small high yeh -> ي
    '\u06e8': '\u0646',  # ۨ small high noon -> ن
    '\u06e1': '\u0652',  # ۡ Quranic sukun -> standard sukun ْ
    '\u0654': '\u0621',  # ٔ combining hamza above -> hamza ء
}


def _normalize_word_for_ctc(word: str) -> str:
    """Strip non-vocalic characters and map Quranic symbols to Wav2Vec2 vocab."""
    res: list[str] = []
    for char in word:
        mapped = QURAN_CHAR_MAP.get(char, char)
        if mapped in WAV2VEC2_ARABIC_VOCAB:
            res.append(mapped)
    normalized = "".join(res)
    return normalized if normalized else word


def prepare_alignment_words(canonical_text: str) -> tuple[list[str], list[str]]:
    """Prepare Quranic canonical text for subtitle display and CTC alignment.

    Attaches isolated non-letter marks (e.g. pause marks ۚ, ۖ, sajdah ۩, hizb ۞)
    to adjacent words so subtitle display preserves full Uthmani text while
    producing a 1:1 word-for-word normalized text containing only characters
    supported by the Wav2Vec2 Arabic acoustic model vocabulary.
    """
    raw_tokens = canonical_text.split()
    display_words: list[str] = []

    for token in raw_tokens:
        has_letters = any(
            '\u0621' <= c <= '\u064a' or c in '\u0670\u0671\u06e5\u06e6\u06e7\u06e8'
            for c in token
        )
        if not has_letters:
            if display_words:
                display_words[-1] = f"{display_words[-1]} {token}"
            else:
                display_words.append(token)
        else:
            if display_words and not any(
                '\u0621' <= c <= '\u064a' or c in '\u0670\u0671\u06e5\u06e6\u06e7\u06e8'
                for c in display_words[-1]
            ):
                display_words[-1] = f"{display_words[-1]} {token}"
            else:
                display_words.append(token)

    ctc_words = [_normalize_word_for_ctc(w) for w in display_words]
    return display_words, ctc_words


@dataclass(frozen=True, slots=True)
class AlignedWord:
    word: str
    start_ms: int
    end_ms: int


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class CtcForcedAligner:
    """Run CTC alignment in a disposable directory; its JSON sidecar never leaks."""

    def __init__(self, binary: str = "ctc-forced-aligner", model: str = "jonatasgrosman/wav2vec2-large-xlsr-53-arabic", device: str = "cpu", batch_size: int = 4, runner: CommandRunner | None = None) -> None:
        self.binary, self.model, self.device, self.batch_size = binary, model, device, batch_size
        self.runner = runner or self._run

    def align(self, audio_path: Path, canonical_text: str) -> list[AlignedWord]:
        display_words, aligner_words = prepare_alignment_words(canonical_text)
        if not display_words:
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
            text_path.write_text(" ".join(aligner_words), encoding="utf-8")
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
            return self._parse(output, display_words, self._duration_ms(work_audio))

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

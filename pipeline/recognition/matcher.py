"""Arabic-normalized fuzzy matching against contiguous Quran verses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from pipeline.recognition.corpus import CanonicalVerse, QuranCorpus

_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06edـ]")
_NON_ARABIC = re.compile(r"[^ء-ي ]+")


def normalize_arabic(text: str) -> str:
    text = _DIACRITICS.sub("", text).translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(_NON_ARABIC.sub(" ", text).split())


@dataclass(frozen=True, slots=True)
class VerseMatch:
    surah: int
    ayah_start: int
    ayah_end: int
    canonical_text: str
    confidence: float


class QuranMatcher:
    """Choose the best contiguous range; v1 caps a post at six ayat."""

    def __init__(self, corpus: QuranCorpus, maximum_ayahs: int = 6) -> None:
        self.corpus, self.maximum_ayahs = corpus, maximum_ayahs

    def match(self, transcript: str) -> VerseMatch:
        normalized = normalize_arabic(transcript)
        if not normalized:
            raise ValueError("Transcript contains no Arabic text to match.")
        best: tuple[float, list[CanonicalVerse]] | None = None
        for start, first in enumerate(self.corpus.verses):
            candidate: list[CanonicalVerse] = []
            for verse in self.corpus.verses[start : start + self.maximum_ayahs]:
                if verse.surah != first.surah or (candidate and verse.ayah != candidate[-1].ayah + 1):
                    break
                candidate.append(verse)
                score = SequenceMatcher(None, normalized, normalize_arabic(" ".join(item.text for item in candidate))).ratio()
                if best is None or score > best[0]:
                    best = score, candidate.copy()
        assert best is not None
        score, matched = best
        return VerseMatch(matched[0].surah, matched[0].ayah, matched[-1].ayah, " ".join(item.text for item in matched), round(score, 4))

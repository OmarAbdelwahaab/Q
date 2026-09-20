"""Locally cached canonical Uthmani Quran corpus."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CanonicalVerse:
    surah: int
    ayah: int
    text: str


class QuranCorpus:
    def __init__(self, verses: list[CanonicalVerse]) -> None:
        if not verses:
            raise ValueError("Canonical Quran corpus is empty.")
        self.verses = sorted(verses, key=lambda verse: (verse.surah, verse.ayah))

    @classmethod
    def load_or_fetch(cls, cache_path: Path, api_base_url: str) -> "QuranCorpus":
        if cache_path.is_file():
            return cls._from_json(cache_path.read_text(encoding="utf-8"))
        verses: list[CanonicalVerse] = []
        for surah in range(1, 115):
            query = urllib.parse.urlencode({"chapter_number": surah})
            with urllib.request.urlopen(f"{api_base_url}?{query}", timeout=30) as response:
                payload = json.load(response)
            for verse in payload.get("verses", []):
                chapter, ayah = verse["verse_key"].split(":", 1)
                verses.append(CanonicalVerse(int(chapter), int(ayah), verse["text_uthmani"]))
        corpus = cls(verses)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".partial.json")
        temporary.write_text(json.dumps([asdict(verse) for verse in corpus.verses], ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache_path)
        return corpus

    @classmethod
    def _from_json(cls, payload: str) -> "QuranCorpus":
        return cls([CanonicalVerse(**record) for record in json.loads(payload)])

"""Arabic-normalized fuzzy matching and chronological recitation alignment against Quran verses."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from pipeline.alignment.ctc import prepare_alignment_words
from pipeline.recognition.corpus import CanonicalVerse, QuranCorpus

_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06edـ]")
_NON_ARABIC = re.compile(r"[^ء-ي ]+")


def normalize_arabic(text: str) -> str:
    """Normalize Arabic text by unifying character variants and stripping non-alphabet marks."""
    # Convert Quranic alef wasla (ٱ) to standard alef (ا)
    text = text.replace("\u0671", "ا")
    text = _DIACRITICS.sub("", text).translate(
        str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})
    )
    return " ".join(_NON_ARABIC.sub(" ", text).split())


@dataclass(frozen=True, slots=True)
class VerseMatch:
    surah: int
    ayah_start: int
    ayah_end: int
    canonical_text: str
    confidence: float
    segments: tuple[dict[str, Any], ...] = ()


class QuranMatcher:
    """Chronological recitation matcher with support for multi-surah prayers, skips, and repetitions."""

    def __init__(self, corpus: QuranCorpus, maximum_ayahs: int = 6000) -> None:
        self.corpus = corpus
        self.maximum_ayahs = maximum_ayahs
        self.verse_map: dict[tuple[int, int], dict[str, Any]] = {}
        self.index: dict[tuple[str, str], list[tuple[dict[str, Any], int]]] = defaultdict(list)
        self.unigram_index: dict[str, list[tuple[dict[str, Any], int]]] = defaultdict(list)
        self._build_index()

    def _build_index(self) -> None:
        for v in self.corpus.verses:
            norm = normalize_arabic(v.text)
            words = norm.split()
            display_words, _ = prepare_alignment_words(v.text)
            record = {
                "surah": v.surah,
                "ayah": v.ayah,
                "text": v.text,
                "norm": norm,
                "words": words,
                "display_words": display_words,
            }
            self.verse_map[(v.surah, v.ayah)] = record
            for j in range(len(words) - 1):
                self.index[(words[j], words[j + 1])].append((record, j))
            for j, w in enumerate(words):
                self.unigram_index[w].append((record, j))
            if words:
                self.index[(words[0], "")].append((record, 0))

    def match(self, transcript: str) -> VerseMatch:
        norm_t = normalize_arabic(transcript)
        raw_t_words = [w for w in norm_t.split() if w != "امين"]
        if not raw_t_words:
            raise ValueError("Transcript contains no Arabic text to match.")

        # Contract vocatives split by ASR (e.g. 'يا أيها' -> 'يايها')
        t_words: list[str] = []
        i = 0
        while i < len(raw_t_words):
            if (
                i + 1 < len(raw_t_words)
                and raw_t_words[i] == "يا"
                and raw_t_words[i + 1] in ("ايها", "ايتها")
            ):
                t_words.append("يا" + raw_t_words[i + 1][1:])
                i += 2
            else:
                t_words.append(raw_t_words[i])
                i += 1

        matched_segments: list[dict[str, Any]] = []
        idx = 0
        current_surah: int | None = None
        current_ayah: int | None = None

        while idx < len(t_words):
            candidates: list[tuple[dict[str, Any], int]] = []

            # 1. Look up bigram candidates from transcript
            if idx + 1 < len(t_words):
                candidates.extend(self.index.get((t_words[idx], t_words[idx + 1]), []))
                if idx + 2 < len(t_words):
                    candidates.extend(self.index.get((t_words[idx + 1], t_words[idx + 2]), []))

            # 2. Look up distinctive unigram candidates
            for k in range(min(3, len(t_words) - idx)):
                w = t_words[idx + k]
                hits = self.unigram_index.get(w, [])
                if 0 < len(hits) <= 60:
                    for rec, off in hits:
                        candidates.append((rec, max(0, off - k)))

            # 3. Add contextual anticipated verses (repetitions or upcoming ayahs in same surah)
            if current_surah is not None and current_ayah is not None:
                same_v = self.verse_map.get((current_surah, current_ayah))
                if same_v:
                    for off in range(len(same_v["words"])):
                        candidates.append((same_v, off))
                for next_a in range(current_ayah + 1, current_ayah + 5):
                    next_v = self.verse_map.get((current_surah, next_a))
                    if next_v:
                        candidates.append((next_v, 0))

            # Deduplicate candidate checks
            seen: set[tuple[int, int, int]] = set()
            dedup: list[tuple[dict[str, Any], int]] = []
            for v, off in candidates:
                key = (v["surah"], v["ayah"], off)
                if key not in seen:
                    seen.add(key)
                    dedup.append((v, off))

            best_v: dict[str, Any] | None = None
            best_off = 0
            best_vlen = 0
            best_tlen = 0
            best_score = 0.0

            for v, off in dedup:
                v_words = v["words"][off:]
                if not v_words:
                    continue
                rem_len = len(v_words)

                # 1. Try matching the full remaining verse first
                matched_verse = False
                for dt in (0, -1):
                    t_len = rem_len + dt
                    if t_len <= 0 or idx + t_len > len(t_words):
                        continue
                    chunk_v = " ".join(v_words)
                    chunk_t = " ".join(t_words[idx : idx + t_len])
                    sc = SequenceMatcher(None, chunk_t, chunk_v).ratio()
                    if current_surah == v["surah"]:
                        sc += 0.05

                    if sc >= 0.70:
                        matched_verse = True
                        key_val = sc + (0.005 * rem_len)
                        best_val = best_score + (0.005 * best_vlen)
                        if key_val > best_val:
                            best_score, best_v, best_off, best_vlen, best_tlen = (
                                sc,
                                v,
                                off,
                                rem_len,
                                t_len,
                            )

                # 2. Only if full verse did NOT match, try sub-phrase repetition (>= 0.85 similarity)
                if not matched_verse and rem_len > 4:
                    for p_len in (7, 6, 5, 4):
                        if p_len >= rem_len:
                            continue
                        chunk_v = " ".join(v_words[:p_len])
                        for dt in (0, -1):
                            t_len = p_len + dt
                            if t_len <= 0 or idx + t_len > len(t_words):
                                continue
                            chunk_t = " ".join(t_words[idx : idx + t_len])
                            sc = SequenceMatcher(None, chunk_t, chunk_v).ratio()
                            if current_surah == v["surah"]:
                                sc += 0.05
                            if sc >= 0.85:
                                key_val = sc + (0.005 * p_len)
                                best_val = best_score + (0.005 * best_vlen)
                                if key_val > best_val:
                                    best_score, best_v, best_off, best_vlen, best_tlen = (
                                        sc,
                                        v,
                                        off,
                                        p_len,
                                        t_len,
                                    )

            if best_v and best_score >= 0.70:
                if best_off == 0 and best_vlen == len(best_v["words"]):
                    raw_text = best_v["text"]
                else:
                    raw_text = " ".join(best_v["display_words"][best_off : best_off + best_vlen])
                matched_segments.append({
                    "surah": best_v["surah"],
                    "ayah": best_v["ayah"],
                    "raw_text": raw_text,
                    "words_count": best_tlen,
                    "score": best_score,
                })
                current_surah = best_v["surah"]
                current_ayah = best_v["ayah"]
                idx += best_tlen
            else:
                idx += 1

        if not matched_segments:
            raise ValueError("Could not match transcript to Quran corpus.")

        canonical_text = " ".join(s["raw_text"] for s in matched_segments)
        matched_words_total = sum(s["words_count"] for s in matched_segments)
        confidence = min(1.0, round(matched_words_total / len(t_words), 4))

        # Determine primary surah (most words recited, preferring non-Fatiha if present)
        surah_counts: dict[int, int] = defaultdict(int)
        for s in matched_segments:
            surah_counts[s["surah"]] += s["words_count"]

        non_fatiha = {k: v for k, v in surah_counts.items() if k != 1}
        primary_surah = max(non_fatiha, key=non_fatiha.get) if non_fatiha else 1

        primary_ayahs = [s["ayah"] for s in matched_segments if s["surah"] == primary_surah]
        ayah_start = min(primary_ayahs)
        ayah_end = max(primary_ayahs)

        return VerseMatch(
            surah=primary_surah,
            ayah_start=ayah_start,
            ayah_end=ayah_end,
            canonical_text=canonical_text,
            confidence=confidence,
            segments=tuple(matched_segments),
        )

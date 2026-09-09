"""Advanced SubStation Alpha (.ass) karaoke subtitle generator for Quran recitation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Canonical Arabic names for the 114 Surahs
SURAH_NAMES: dict[int, str] = {
    1: "الفاتحة", 2: "البقرة", 3: "آل عمران", 4: "النساء", 5: "المائدة",
    6: "الأنعام", 7: "الأعراف", 8: "الأنفال", 9: "التوبة", 10: "يونس",
    11: "هود", 12: "يوسف", 13: "الرعد", 14: "إبراهيم", 15: "الحجر",
    16: "النحل", 17: "الإسراء", 18: "الكهف", 19: "مريم", 20: "طه",
    21: "الأنبياء", 22: "الحج", 23: "المؤمنون", 24: "النور", 25: "الفرقان",
    26: "الشعراء", 27: "النمل", 28: "القصص", 29: "العنكبوت", 30: "الروم",
    31: "لقمان", 32: "السجدة", 33: "الأحزاب", 34: "سبأ", 35: "فاطر",
    36: "يس", 37: "الصافات", 38: "ص", 39: "الزمر", 40: "غافر",
    41: "فصلت", 42: "الشورى", 43: "الزخرف", 44: "الدخان", 45: "الجاثية",
    46: "الأحقاف", 47: "محمد", 48: "الفتح", 49: "الحجرات", 50: "ق",
    51: "الذاريات", 52: "الطور", 53: "النجم", 54: "القمر", 55: "الرحمن",
    56: "الواقعة", 57: "الحديد", 58: "المجادلة", 59: "الحشر", 60: "الممتحنة",
    61: "الصف", 62: "الجمعة", 63: "المنافقون", 64: "التغابن", 65: "الطلاق",
    66: "التحريم", 67: "الملك", 68: "القلم", 69: "الحاقة", 70: "المعارج",
    71: "نوح", 72: "الجن", 73: "المزمل", 74: "المدثر", 75: "القيامة",
    76: "الإنسان", 77: "المرسلات", 78: "النبأ", 79: "النازعات", 80: "عبس",
    81: "التكوير", 82: "الانفطار", 83: "المطففين", 84: "الانشقاق", 85: "البروج",
    86: "الطارق", 87: "الأعلى", 88: "الغاشية", 89: "الفجر", 90: "البلد",
    91: "الشمس", 92: "الليل", 93: "الضحى", 94: "الشرح", 95: "التين",
    96: "العلق", 97: "القدر", 98: "البينة", 99: "الزلزلة", 100: "العاديات",
    101: "القارعة", 102: "التكاثر", 103: "العصر", 104: "الهمزة", 105: "الفيل",
    106: "قريش", 107: "الماعون", 108: "الكوثر", 109: "الكافرون", 110: "النصر",
    111: "المسد", 112: "الإخلاص", 113: "الفلق", 114: "الناس",
}


@dataclass(frozen=True, slots=True)
class AlignedWordItem:
    word: str
    start_ms: int
    end_ms: int


def ms_to_ass_time(ms: int) -> str:
    """Format milliseconds into ASS timestamp H:MM:SS.cs."""
    total_cs = max(0, ms // 10)
    cs = total_cs % 100
    total_seconds = total_cs // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


class KaraokeSubtitleGenerator:
    """Generates Advanced SubStation Alpha (.ass) scripts with word-level karaoke timing."""

    def __init__(
        self,
        font_name: str = "Traditional Arabic",
        font_size: int = 70,
        words_per_line: int = 6,
        pause_threshold_ms: int = 500,
    ) -> None:
        self.font_name = font_name
        self.font_size = font_size
        self.words_per_line = max(1, words_per_line)
        self.pause_threshold_ms = pause_threshold_ms

    def generate(
        self,
        words: Sequence[dict[str, object] | AlignedWordItem],
        surah: int | None = None,
        ayah_start: int | None = None,
        ayah_end: int | None = None,
        total_duration_ms: int | None = None,
    ) -> str:
        """Construct full ASS script text for word-by-word reveal over vertical 1080x1920 canvas."""
        normalized_words = self._normalize_words(words)
        if not normalized_words:
            return self._build_empty_script()

        lines = self._chunk_words(normalized_words)
        events: list[str] = []

        # 1. Surah header banner (if surah metadata is provided)
        if surah is not None:
            header_text = self._format_header_text(surah, ayah_start, ayah_end)
            start_time = ms_to_ass_time(0)
            end_time = ms_to_ass_time(
                total_duration_ms or (normalized_words[-1].end_ms + 1000)
            )
            events.append(
                f"Dialogue: 1,{start_time},{end_time},SurahHeader,,0,0,0,,{header_text}"
            )

        # 2. Karaoke line dialogue events
        for line_words in lines:
            line_start_ms = line_words[0].start_ms
            line_end_ms = line_words[-1].end_ms + 250  # 250ms hold tail after line ends

            karaoke_text_parts: list[str] = []
            previous_end_ms = line_start_ms

            for word_item in line_words:
                # Handle intra-word lead-in pause if any
                gap_ms = word_item.start_ms - previous_end_ms
                if gap_ms > 20:
                    gap_cs = max(1, round(gap_ms / 10))
                    karaoke_text_parts.append(f"{{\\k{gap_cs}}}")

                duration_ms = word_item.end_ms - word_item.start_ms
                duration_cs = max(1, round(duration_ms / 10))
                karaoke_text_parts.append(f"{{\\k{duration_cs}}}{word_item.word} ")
                previous_end_ms = word_item.end_ms

            line_text = "".join(karaoke_text_parts).rstrip()
            event_line = (
                f"Dialogue: 0,{ms_to_ass_time(line_start_ms)},{ms_to_ass_time(line_end_ms)},"
                f"QuranText,,0,0,0,,{line_text}"
            )
            events.append(event_line)

        return self._build_script(events)

    def write_ass_file(
        self,
        output_path: Path,
        words: Sequence[dict[str, object] | AlignedWordItem],
        surah: int | None = None,
        ayah_start: int | None = None,
        ayah_end: int | None = None,
        total_duration_ms: int | None = None,
    ) -> Path:
        """Write generated ASS content to target path atomically."""
        content = self.generate(
            words=words,
            surah=surah,
            ayah_start=ayah_start,
            ayah_end=ayah_end,
            total_duration_ms=total_duration_ms,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = output_path.with_suffix(".tmp.ass")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(output_path)
        return output_path

    def _normalize_words(
        self, words: Sequence[dict[str, object] | AlignedWordItem]
    ) -> list[AlignedWordItem]:
        normalized: list[AlignedWordItem] = []
        for item in words:
            if isinstance(item, AlignedWordItem):
                normalized.append(item)
            elif isinstance(item, dict):
                normalized.append(
                    AlignedWordItem(
                        word=str(item.get("word", "")),
                        start_ms=int(item.get("start_ms", 0)),
                        end_ms=int(item.get("end_ms", 0)),
                    )
                )
        return [w for w in normalized if w.word.strip()]

    def _chunk_words(
        self, words: list[AlignedWordItem]
    ) -> list[list[AlignedWordItem]]:
        """Group words into visually balanced chunks for 9:16 vertical viewports."""
        chunks: list[list[AlignedWordItem]] = []
        current_chunk: list[AlignedWordItem] = []

        for word in words:
            if not current_chunk:
                current_chunk.append(word)
                continue

            pause_from_prev = word.start_ms - current_chunk[-1].end_ms
            if (
                len(current_chunk) >= self.words_per_line
                or pause_from_prev >= self.pause_threshold_ms
            ):
                chunks.append(current_chunk)
                current_chunk = [word]
            else:
                current_chunk.append(word)

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _format_header_text(
        self,
        surah: int,
        ayah_start: int | None,
        ayah_end: int | None,
    ) -> str:
        surah_name = SURAH_NAMES.get(surah, f"رقم {surah}")
        header = f"سورة {surah_name}"
        if ayah_start is not None:
            if ayah_end is not None and ayah_end != ayah_start:
                header += f" • الآيات {ayah_start}-{ayah_end}"
            else:
                header += f" • الآية {ayah_start}"
        return header

    def _build_script(self, events: list[str]) -> str:
        events_block = "\n".join(events)
        return f"""[Script Info]
Title: Quran Video Karaoke Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: QuranText,{self.font_name},{self.font_size},&H00FFFFFF,&H50C0C0C0,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,80,1
Style: SurahHeader,{self.font_name},44,&H00D4AF37,&H00D4AF37,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,8,80,80,240,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events_block}
"""

    def _build_empty_script(self) -> str:
        return self._build_script([])

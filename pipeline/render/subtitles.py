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


def get_font_family_name_from_ttf(font_path: Path) -> str:
    """Extract font family name from TTF/OTF name table for libass font resolution."""
    if not font_path.is_file():
        return "Traditional Arabic"

    try:
        data = font_path.read_bytes()
        if len(data) >= 12:
            import struct

            num_tables = struct.unpack(">H", data[4:6])[0]
            for i in range(num_tables):
                tag = data[12 + i * 16 : 16 + i * 16]
                if tag == b"name":
                    offset = struct.unpack(">I", data[20 + i * 16 : 24 + i * 16])[0]
                    count = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
                    string_offset = (
                        struct.unpack(">H", data[offset + 4 : offset + 6])[0] + offset
                    )
                    for j in range(count):
                        rec = data[offset + 6 + j * 12 : offset + 18 + j * 12]
                        (
                            platform_id,
                            encoding_id,
                            language_id,
                            name_id,
                            length,
                            str_off,
                        ) = struct.unpack(">HHHHHH", rec)
                        if name_id == 1:  # Font Family name
                            raw = data[
                                string_offset + str_off : string_offset + str_off + length
                            ]
                            try:
                                decoded = raw.decode(
                                    "utf-16be" if platform_id in (0, 3) else "utf-8"
                                ).strip()
                                if decoded:
                                    return decoded
                            except Exception:
                                pass
    except Exception:
        pass

    return font_path.stem


class KaraokeSubtitleGenerator:
    """Generates Advanced SubStation Alpha (.ass) scripts with word-level karaoke timing."""

    def __init__(
        self,
        font_name: str = "Traditional Arabic",
        font_size: int = 70,
        words_per_line: int = 6,
        pause_threshold_ms: int = 500,
        font_path: Path | None = None,
        center_y: int = 960,
        play_res_x: int = 1080,
        play_res_y: int = 1920,
    ) -> None:
        self.font_name = font_name
        self.font_size = font_size
        self.words_per_line = max(1, words_per_line)
        self.pause_threshold_ms = pause_threshold_ms
        self.font_path = font_path
        self.center_y = center_y
        self.play_res_x = play_res_x
        self.play_res_y = play_res_y

    def _compute_word_clips(
        self,
        line_text: str,
        line_words: list[AlignedWordItem],
    ) -> list[tuple[int, int]]:
        """Compute pixel clip intervals [x1, x2] for each word in line_text.

        Uses OpenType text shaping via uharfbuzz when available to determine exact
        glyph advances and visual bounding boxes. Because Arabic is Right-to-Left,
        word N-1 is on the far visual left and word 0 is on the far visual right.
        Midpoints in inter-word spaces are used as clip boundaries so clipping
        never bisects a character.
        """
        words = [w.word for w in line_words]
        num_words = len(words)
        if num_words == 0:
            return []

        resolved_font_path = self.font_path
        if resolved_font_path is None or not resolved_font_path.is_file():
            for candidate in [
                Path("pipeline/assets/fonts/Amiri-Regular.ttf"),
                Path("pipeline/assets/fonts/arabic-display.ttf"),
            ]:
                if candidate.is_file():
                    resolved_font_path = candidate
                    break

        if resolved_font_path and resolved_font_path.is_file():
            try:
                import uharfbuzz as hb

                blob = hb.Blob.from_file_path(str(resolved_font_path))
                face = hb.Face(blob)
                font = hb.Font(face)
                upem = face.upem

                buf = hb.Buffer()
                buf.add_str(line_text)
                buf.guess_segment_properties()
                hb.shape(font, buf)

                word_char_ranges: list[tuple[int, int]] = []
                idx = 0
                for w in words:
                    word_char_ranges.append((idx, idx + len(w)))
                    idx += len(w) + 1

                word_x_bounds = {
                    i: [float("inf"), float("-inf")] for i in range(num_words)
                }
                curr_x = 0
                for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                    gx1 = curr_x
                    gx2 = curr_x + pos.x_advance
                    c = info.cluster
                    for i, (w_start, w_end) in enumerate(word_char_ranges):
                        if w_start <= c < w_end:
                            word_x_bounds[i][0] = min(word_x_bounds[i][0], gx1)
                            word_x_bounds[i][1] = max(word_x_bounds[i][1], gx2)
                            break
                    curr_x += pos.x_advance

                total_w = curr_x
                if total_w > 0:
                    line_render_w = total_w * (self.font_size / upem) * 0.36
                    line_x1 = (self.play_res_x - line_render_w) / 2.0

                    visual_order = sorted(
                        range(num_words), key=lambda i: word_x_bounds[i][0]
                    )
                    visual_cutoffs = [0.0]
                    for idx_v in range(len(visual_order) - 1):
                        w_left = visual_order[idx_v]
                        w_right = visual_order[idx_v + 1]
                        mid = (
                            word_x_bounds[w_left][1] + word_x_bounds[w_right][0]
                        ) / 2.0
                        visual_cutoffs.append(mid / total_w)
                    visual_cutoffs.append(1.0)

                    clips: list[tuple[int, int]] = [(0, 0)] * num_words
                    for idx_v, w_idx in enumerate(visual_order):
                        f1 = visual_cutoffs[idx_v]
                        f2 = visual_cutoffs[idx_v + 1]
                        sx1 = int(round(line_x1 + f1 * line_render_w))
                        sx2 = int(round(line_x1 + f2 * line_render_w))
                        if idx_v == 0:
                            sx1 = max(0, sx1 - 40)
                        if idx_v == len(visual_order) - 1:
                            sx2 = min(self.play_res_x, sx2 + 40)
                        clips[w_idx] = (sx1, sx2)
                    return clips
            except Exception:
                pass

        # Proportional fallback based on character count if font parsing fails
        char_counts = [max(1, len(w)) for w in words]
        total_chars = sum(char_counts)
        est_w = min(self.play_res_x - 160, int(total_chars * self.font_size * 0.36))
        line_x1 = (self.play_res_x - est_w) / 2.0
        clips = [(0, 0)] * num_words
        curr_frac = 0.0
        for idx_v, w_idx in enumerate(reversed(range(num_words))):
            frac = char_counts[w_idx] / total_chars
            sx1 = int(round(line_x1 + curr_frac * est_w))
            sx2 = int(round(line_x1 + (curr_frac + frac) * est_w))
            if idx_v == 0:
                sx1 = max(0, sx1 - 40)
            if idx_v == num_words - 1:
                sx2 = min(self.play_res_x, sx2 + 40)
            clips[w_idx] = (sx1, sx2)
            curr_frac += frac
        return clips

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

        # 2. Karaoke line dialogue events using non-interleaved clip architecture.
        # Standard ASS \\k tags or inline per-word \\c overrides cause libass to split Arabic into
        # separate LTR layout chunks, flipping word order. By rendering the entire continuous Arabic
        # sentence unbroken and applying rectangular \\clip bounds for word highlight reveals,
        # HarfBuzz shapes 100% native Right-to-Left Arabic text without any word transposition.
        for line_idx, line_words in enumerate(lines):
            next_line_start_ms = (
                lines[line_idx + 1][0].start_ms if line_idx + 1 < len(lines) else None
            )
            line_start_ms = line_words[0].start_ms
            raw_line_end_ms = line_words[-1].end_ms + 250
            line_end_ms = (
                min(raw_line_end_ms, next_line_start_ms)
                if next_line_start_ms is not None
                else raw_line_end_ms
            )

            line_text = " ".join(item.word for item in line_words)
            clips = self._compute_word_clips(line_text, line_words)
            pos_tag = f"{{\\an5\\pos({self.play_res_x // 2},{self.center_y})}}"

            # Base Layer 0: Whole line in Dim Gray for entire line duration
            events.append(
                f"Dialogue: 0,{ms_to_ass_time(line_start_ms)},{ms_to_ass_time(line_end_ms)},"
                f"QuranDim,,0,0,0,,{pos_tag}{line_text}"
            )

            # Layers 1 & 2: Word-by-word active (Gold) and completed (White) reveal clips
            for idx, word_item in enumerate(line_words):
                c1, c2 = clips[idx]
                clip_tag = (
                    f"{{\\an5\\pos({self.play_res_x // 2},{self.center_y})"
                    f"\\clip({c1},0,{c2},{self.play_res_y})}}"
                )

                if word_item.end_ms > word_item.start_ms:
                    # Layer 2: Active Gold highlight while word is spoken
                    events.append(
                        f"Dialogue: 2,{ms_to_ass_time(word_item.start_ms)},{ms_to_ass_time(word_item.end_ms)},"
                        f"QuranActive,,0,0,0,,{clip_tag}{line_text}"
                    )

                if line_end_ms > word_item.end_ms:
                    # Layer 1: Completed White reveal once word finishes
                    events.append(
                        f"Dialogue: 1,{ms_to_ass_time(word_item.end_ms)},{ms_to_ass_time(line_end_ms)},"
                        f"QuranCompleted,,0,0,0,,{clip_tag}{line_text}"
                    )

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
PlayResX: {self.play_res_x}
PlayResY: {self.play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: QuranText,{self.font_name},{self.font_size},&H00FFFFFF,&H90707070,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,80,1
Style: QuranDim,{self.font_name},{self.font_size},&H90707070,&H90707070,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,80,1
Style: QuranActive,{self.font_name},{self.font_size},&H0037AFD4,&H0037AFD4,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,80,1
Style: QuranCompleted,{self.font_name},{self.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,80,1
Style: SurahHeader,{self.font_name},44,&H0037AFD4,&H0037AFD4,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,8,80,80,240,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events_block}
"""

    def _build_empty_script(self) -> str:
        return self._build_script([])


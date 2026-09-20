"""Caption and hashtag templating for social media publishing."""

from __future__ import annotations

import re
from typing import Mapping

from pipeline.render.subtitles import SURAH_NAMES

DEFAULT_HASHTAGS: tuple[str, ...] = (
    "#قرآن",
    "#تلاوة",
    "#قرآن_كريم",
    "#تلاوات_خاشعة",
    "#Quran",
    "#Islam",
)

PLATFORM_MAX_CAPTION_LENGTHS: dict[str, int] = {
    "x": 280,
    "twitter": 280,
    "telegram": 1024,
    "tiktok": 2200,
    "instagram": 2200,
    "youtube": 5000,
    "facebook": 63206,
}

DEFAULT_TEMPLATE = "{surah_ref}\n\n{canonical_text}\n\n{branding_handle}\n\n{hashtags}"


class CaptionTemplater:
    """Format post captions with Surah name, Ayah range, canonical text, handle, and hashtags."""

    def __init__(
        self,
        default_template: str | None = None,
        default_hashtags: tuple[str, ...] | None = None,
        branding_handle: str | None = None,
    ) -> None:
        self.default_template = default_template or DEFAULT_TEMPLATE
        self.default_hashtags = (
            default_hashtags if default_hashtags is not None else DEFAULT_HASHTAGS
        )
        self.branding_handle = branding_handle

    def format_surah_reference(self, surah: int, ayah_start: int, ayah_end: int) -> str:
        """Format the Quranic reference header in Arabic."""
        surah_name = SURAH_NAMES.get(surah, f"رقم {surah}")
        if ayah_start == ayah_end:
            return f"سورة {surah_name} • الآية {ayah_start}"
        return f"سورة {surah_name} • الآيات {ayah_start}-{ayah_end}"

    def format_ayah_range(self, ayah_start: int, ayah_end: int) -> str:
        """Format the Ayah range as a string."""
        if ayah_start == ayah_end:
            return str(ayah_start)
        return f"{ayah_start}-{ayah_end}"

    def build_caption(
        self,
        surah: int,
        ayah_start: int,
        ayah_end: int,
        canonical_text: str,
        platform: str | None = None,
        branding_handle: str | None = None,
        template: str | None = None,
        hashtags: tuple[str, ...] | None = None,
    ) -> str:
        """Render the caption with optional platform-specific length constraints."""
        handle = branding_handle if branding_handle is not None else self.branding_handle
        tags_list = list(hashtags if hashtags is not None else self.default_hashtags)
        tmpl = template or self.default_template

        surah_name = SURAH_NAMES.get(surah, f"رقم {surah}")
        surah_ref = self.format_surah_reference(surah, ayah_start, ayah_end)
        ayah_range = self.format_ayah_range(ayah_start, ayah_end)

        def _render(
            cur_text: str,
            cur_handle: str | None,
            cur_tags: list[str],
        ) -> str:
            tag_str = " ".join(cur_tags) if cur_tags else ""
            handle_str = cur_handle or ""
            rendered = tmpl.format(
                surah_name=surah_name,
                surah_ref=surah_ref,
                ayah_range=ayah_range,
                canonical_text=cur_text,
                branding_handle=handle_str,
                hashtags=tag_str,
            )
            # Collapse excessive empty lines from empty variables
            lines = [line.strip() for line in rendered.splitlines()]
            collapsed: list[str] = []
            prev_blank = False
            for line in lines:
                if not line:
                    if not prev_blank:
                        collapsed.append("")
                        prev_blank = True
                else:
                    collapsed.append(line)
                    prev_blank = False
            return "\n".join(collapsed).strip()

        # Initial render
        rendered = _render(canonical_text, handle, tags_list)

        if not platform:
            return rendered

        max_length = PLATFORM_MAX_CAPTION_LENGTHS.get(platform.lower())
        if max_length is None or len(rendered) <= max_length:
            return rendered

        # Platform length constraint exceeded: truncate canonical_text
        cur_text = canonical_text
        while len(rendered) > max_length and len(cur_text) > 0:
            excess = len(rendered) - max_length + 3  # reserve for ellipsis
            cut_point = max(0, len(cur_text) - excess)
            if cut_point > 0:
                cur_text = cur_text[:cut_point].rstrip() + "..."
            else:
                cur_text = ""
            rendered = _render(cur_text, handle, tags_list)

        # If still over limit (e.g. hashtags or handle too long on X 280), shed hashtags
        while len(rendered) > max_length and tags_list:
            tags_list.pop()
            rendered = _render(cur_text, handle, tags_list)

        # Extreme fallback if still exceeding: hard truncate to max_length
        if len(rendered) > max_length:
            rendered = rendered[:max_length]

        return rendered

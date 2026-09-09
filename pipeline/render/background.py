"""Background asset pool loader and selection strategy implementation."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Sequence

SUPPORTED_MEDIA_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)


class BackgroundAssetPool:
    """Discovers background video/image loops and selects an asset per configured strategy."""

    def __init__(
        self,
        asset_dir: Path,
        strategy: str = "round_robin",
        random_seed: int | None = None,
    ) -> None:
        self.asset_dir = asset_dir
        self.strategy = strategy.lower()
        self._rng = random.Random(random_seed)
        self._round_robin_counter = 0

    def list_assets(self) -> list[Path]:
        """Scan asset_dir and return sorted valid background media paths."""
        if not self.asset_dir.is_dir():
            return []

        assets = [
            path
            for path in self.asset_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
        ]
        return sorted(assets, key=lambda p: p.name.lower())

    def select(
        self,
        surah: int | None = None,
        message_id: int | None = None,
    ) -> Path | None:
        """Select a background asset path according to the configured strategy.

        Returns None if no assets exist in the pool, indicating procedural fallback.
        """
        assets = self.list_assets()
        if not assets:
            return None

        if self.strategy == "random":
            if message_id is not None:
                # Deterministic selection based on message_id if provided
                return assets[message_id % len(assets)]
            return self._rng.choice(assets)

        if self.strategy == "keyed":
            if surah is not None:
                # Try exact surah number prefix match (e.g. '001_...', 'surah_1_...')
                surah_patterns = (f"{surah:03d}", f"surah_{surah}", f"surah{surah}")
                for asset in assets:
                    stem = asset.stem.lower()
                    if any(pattern in stem for pattern in surah_patterns):
                        return asset
                # Fallback to deterministic modulo indexing by surah
                return assets[(surah - 1) % len(assets)]

            if message_id is not None:
                return assets[message_id % len(assets)]
            return assets[0]

        # Default: round_robin
        if message_id is not None:
            return assets[message_id % len(assets)]

        chosen = assets[self._round_robin_counter % len(assets)]
        self._round_robin_counter += 1
        return chosen

    @staticmethod
    def get_procedural_background_filter(width: int = 1080, height: int = 1920) -> str:
        """FFmpeg filter expression generating a stylized deep architectural dark slate."""
        return f"color=c=0x0d1117:s={width}x{height}"

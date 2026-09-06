"""Storage helpers for pipeline artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable


DownloadToPath = Callable[[Path], Awaitable[None]]


class LocalArtifactStorage:
    """Persist pipeline artifacts under a configurable local root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def write_from_downloader(self, key: str, downloader: DownloadToPath) -> Path:
        destination = self.root / Path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await downloader(destination)
        return destination

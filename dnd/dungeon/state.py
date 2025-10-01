"""Persistent storage for dungeon session metadata."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

__all__ = ["DungeonMetadataStore", "GuildSessionMetadata"]


@dataclass
class GuildSessionMetadata:
    """Metadata tracked per guild for dungeon sessions."""

    guild_id: int
    default_theme: str | None = None
    last_theme: str | None = None
    last_seed: int | None = None

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {}
        if self.default_theme is not None:
            data["default_theme"] = self.default_theme
        if self.last_theme is not None:
            data["last_theme"] = self.last_theme
        if self.last_seed is not None:
            data["last_seed"] = self.last_seed
        return data

    @classmethod
    def from_dict(cls, guild_id: int, raw: Mapping[str, object]) -> "GuildSessionMetadata":
        default_theme = raw.get("default_theme")
        last_theme = raw.get("last_theme")
        last_seed = raw.get("last_seed")
        return cls(
            guild_id=guild_id,
            default_theme=str(default_theme) if isinstance(default_theme, str) else None,
            last_theme=str(last_theme) if isinstance(last_theme, str) else None,
            last_seed=int(last_seed) if isinstance(last_seed, int) else None,
        )


class DungeonMetadataStore:
    """Concurrency-safe storage for guild dungeon metadata."""

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._loaded = False
        self._cache: Dict[str, GuildSessionMetadata] = {}

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._storage_path.exists():
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache = {}
            self._loaded = True
            return
        text = await asyncio.to_thread(self._storage_path.read_text, encoding="utf-8")
        if text.strip():
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                self._cache = {}
            else:
                if isinstance(raw, dict):
                    cache: Dict[str, GuildSessionMetadata] = {}
                    for guild_id, payload in raw.items():
                        if not isinstance(payload, Mapping):
                            continue
                        try:
                            numeric_id = int(guild_id)
                        except (TypeError, ValueError):
                            continue
                        cache[str(numeric_id)] = GuildSessionMetadata.from_dict(numeric_id, payload)
                    self._cache = cache
        self._loaded = True

    async def _persist(self) -> None:
        serialised = {
            guild_id: metadata.to_dict()
            for guild_id, metadata in self._cache.items()
        }
        text = json.dumps(serialised, indent=2, sort_keys=True)
        await asyncio.to_thread(self._storage_path.write_text, text, encoding="utf-8")

    async def get_default_theme(self, guild_id: int) -> Optional[str]:
        async with self._lock:
            await self._ensure_loaded()
            metadata = self._cache.get(str(guild_id))
            return metadata.default_theme if metadata else None

    async def set_default_theme(self, guild_id: int, theme: Optional[str]) -> GuildSessionMetadata:
        async with self._lock:
            await self._ensure_loaded()
            key = str(guild_id)
            metadata = self._cache.get(key)
            if metadata is None:
                metadata = GuildSessionMetadata(guild_id=guild_id)
                self._cache[key] = metadata
            metadata.default_theme = theme
            if metadata.default_theme is None and metadata.last_theme is None and metadata.last_seed is None:
                del self._cache[key]
            await self._persist()
            return metadata

    async def record_session(self, guild_id: int, *, theme: str, seed: Optional[int]) -> GuildSessionMetadata:
        async with self._lock:
            await self._ensure_loaded()
            key = str(guild_id)
            metadata = self._cache.get(key)
            if metadata is None:
                metadata = GuildSessionMetadata(guild_id=guild_id)
                self._cache[key] = metadata
            metadata.last_theme = theme
            metadata.last_seed = seed if seed is None else int(seed)
            await self._persist()
            return metadata

    async def clear_guild(self, guild_id: int) -> None:
        async with self._lock:
            await self._ensure_loaded()
            key = str(guild_id)
            metadata = self._cache.get(key)
            if metadata is None:
                return
            metadata.default_theme = None
            metadata.last_theme = None
            metadata.last_seed = None
            del self._cache[key]
            await self._persist()

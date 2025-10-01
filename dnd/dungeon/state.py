"""Persistent storage for dungeon session metadata."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

__all__ = ["DungeonMetadataStore", "GuildSessionMetadata", "StoredDungeon"]


@dataclass
class GuildSessionMetadata:
    """Metadata tracked per guild for dungeon sessions."""

    guild_id: int
    default_theme: str | None = None
    last_theme: str | None = None
    last_seed: int | None = None
    last_difficulty: str | None = None
    last_name: str | None = None
    dungeons: Dict[str, "StoredDungeon"] = field(default_factory=dict)

    def _resolve_dungeon_key(self, name: str) -> Optional[str]:
        lowered = name.casefold()
        for existing in self.dungeons:
            if existing.casefold() == lowered:
                return existing
        return None

    def get_dungeon(self, name: str) -> Optional["StoredDungeon"]:
        key = self._resolve_dungeon_key(name)
        if key is None:
            return None
        return self.dungeons.get(key)

    def upsert_dungeon(self, dungeon: "StoredDungeon") -> None:
        key = self._resolve_dungeon_key(dungeon.name)
        if key is not None and key != dungeon.name:
            del self.dungeons[key]
        self.dungeons[dungeon.name] = dungeon

    def remove_dungeon(self, name: str) -> bool:
        key = self._resolve_dungeon_key(name)
        if key is None:
            return False
        del self.dungeons[key]
        return True

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {}
        if self.default_theme is not None:
            data["default_theme"] = self.default_theme
        if self.last_theme is not None:
            data["last_theme"] = self.last_theme
        if self.last_seed is not None:
            data["last_seed"] = self.last_seed
        if self.last_difficulty is not None:
            data["last_difficulty"] = self.last_difficulty
        if self.last_name is not None:
            data["last_name"] = self.last_name
        if self.dungeons:
            data["dungeons"] = {
                name: dungeon.to_dict() for name, dungeon in self.dungeons.items()
            }
        return data

    @classmethod
    def from_dict(cls, guild_id: int, raw: Mapping[str, object]) -> "GuildSessionMetadata":
        default_theme = raw.get("default_theme")
        last_theme = raw.get("last_theme")
        last_seed = raw.get("last_seed")
        last_difficulty = raw.get("last_difficulty")
        last_name = raw.get("last_name")
        dungeons: Dict[str, StoredDungeon] = {}
        raw_dungeons = raw.get("dungeons")
        if isinstance(raw_dungeons, Mapping):
            for dungeon_name, dungeon_payload in raw_dungeons.items():
                if not isinstance(dungeon_name, str) or not isinstance(
                    dungeon_payload, Mapping
                ):
                    continue
                try:
                    dungeon = StoredDungeon.from_dict(dungeon_name, dungeon_payload)
                except ValueError:
                    continue
                dungeons[dungeon.name] = dungeon
        return cls(
            guild_id=guild_id,
            default_theme=str(default_theme) if isinstance(default_theme, str) else None,
            last_theme=str(last_theme) if isinstance(last_theme, str) else None,
            last_seed=int(last_seed) if isinstance(last_seed, int) else None,
            last_difficulty=str(last_difficulty) if isinstance(last_difficulty, str) else None,
            last_name=str(last_name) if isinstance(last_name, str) else None,
            dungeons=dungeons,
        )


@dataclass
class StoredDungeon:
    """Persisted information about a generated dungeon."""

    name: str
    theme: str
    seed: int | None = None
    difficulty: str | None = None

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {"theme": self.theme}
        if self.seed is not None:
            data["seed"] = self.seed
        if self.difficulty is not None:
            data["difficulty"] = self.difficulty
        return data

    @classmethod
    def from_dict(cls, name: str, raw: Mapping[str, object]) -> "StoredDungeon":
        theme = raw.get("theme")
        if not isinstance(theme, str):
            raise ValueError("Dungeon entries must include a theme")
        seed = raw.get("seed")
        difficulty = raw.get("difficulty")
        return cls(
            name=name,
            theme=theme,
            seed=int(seed) if isinstance(seed, int) else None,
            difficulty=str(difficulty) if isinstance(difficulty, str) else None,
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
            if (
                metadata.default_theme is None
                and metadata.last_theme is None
                and metadata.last_seed is None
                and metadata.last_difficulty is None
                and metadata.last_name is None
                and not metadata.dungeons
            ):
                del self._cache[key]
            await self._persist()
            return metadata

    async def record_session(
        self,
        guild_id: int,
        *,
        theme: str,
        seed: Optional[int],
        difficulty: Optional[str] = None,
        name: Optional[str] = None,
    ) -> GuildSessionMetadata:
        async with self._lock:
            await self._ensure_loaded()
            key = str(guild_id)
            metadata = self._cache.get(key)
            if metadata is None:
                metadata = GuildSessionMetadata(guild_id=guild_id)
                self._cache[key] = metadata
            metadata.last_theme = theme
            metadata.last_seed = seed if seed is None else int(seed)
            metadata.last_difficulty = difficulty
            metadata.last_name = name
            if name:
                dungeon = StoredDungeon(
                    name=name,
                    theme=theme,
                    seed=metadata.last_seed,
                    difficulty=difficulty,
                )
                metadata.upsert_dungeon(dungeon)
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
            metadata.last_difficulty = None
            metadata.last_name = None
            metadata.dungeons.clear()
            del self._cache[key]
            await self._persist()

    async def list_dungeon_names(self, guild_id: int) -> tuple[str, ...]:
        async with self._lock:
            await self._ensure_loaded()
            metadata = self._cache.get(str(guild_id))
            if metadata is None:
                return ()
            return tuple(sorted(metadata.dungeons))

    async def get_dungeon(self, guild_id: int, name: str) -> Optional[StoredDungeon]:
        async with self._lock:
            await self._ensure_loaded()
            metadata = self._cache.get(str(guild_id))
            if metadata is None:
                return None
            return metadata.get_dungeon(name)

    async def delete_dungeon(self, guild_id: int, name: str) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            key = str(guild_id)
            metadata = self._cache.get(key)
            if metadata is None:
                return False
            removed = metadata.remove_dungeon(name)
            if not removed:
                return False
            if metadata.last_name and metadata.last_name.casefold() == name.casefold():
                metadata.last_name = None
            if (
                metadata.default_theme is None
                and metadata.last_theme is None
                and metadata.last_seed is None
                and metadata.last_difficulty is None
                and metadata.last_name is None
                and not metadata.dungeons
            ):
                del self._cache[key]
            await self._persist()
            return True

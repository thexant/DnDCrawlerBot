"""Concurrency-safe persistence helpers for characters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, Optional

from .characters import Character


class CharacterRepository:
    """Store characters per guild and user backed by disk."""

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._cache: Dict[str, Dict[str, Dict[str, object]]] = {}
        self._loaded = False
        self._storage_serial: Optional[tuple[int, int]] = None

    async def _ensure_loaded(self) -> None:
        current_serial = await self._current_storage_serial()
        if self._loaded and self._storage_serial == current_serial:
            return
        if current_serial is None:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache = {}
            self._loaded = True
            self._storage_serial = None
            return
        self._cache = {}
        data = await asyncio.to_thread(self._storage_path.read_text)
        if data.strip():
            try:
                raw = json.loads(data)
            except json.JSONDecodeError:
                self._cache = {}
            else:
                if isinstance(raw, dict):
                    self._cache = {
                        str(guild_id): {
                            str(user_id): dict(character)
                            for user_id, character in guild_bucket.items()
                        }
                        for guild_id, guild_bucket in raw.items()
                    }
        self._loaded = True
        self._storage_serial = current_serial

    async def _persist(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._cache, indent=2, sort_keys=True)
        await asyncio.to_thread(self._storage_path.write_text, text)
        self._storage_serial = await self._current_storage_serial()
        self._loaded = True

    async def _current_storage_serial(self) -> Optional[tuple[int, int]]:
        if not self._storage_path.exists():
            return None
        stat_result = await asyncio.to_thread(self._storage_path.stat)
        mtime_ns = getattr(stat_result, "st_mtime_ns", None) or int(
            stat_result.st_mtime * 1_000_000_000
        )
        return (mtime_ns, stat_result.st_size)

    async def get(self, guild_id: int, user_id: int) -> Optional[Character]:
        async with self._lock:
            await self._ensure_loaded()
            guild_bucket = self._cache.get(str(guild_id), {})
            raw = guild_bucket.get(str(user_id))
            return Character.from_dict(raw) if raw else None

    async def exists(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            guild_bucket = self._cache.get(str(guild_id), {})
            return str(user_id) in guild_bucket

    async def save(self, character: Character) -> None:
        async with self._lock:
            await self._ensure_loaded()
            guild_bucket = self._cache.setdefault(str(character.guild_id), {})
            guild_bucket[str(character.user_id)] = character.to_dict()
            await self._persist()

    async def clear(self, guild_id: int, user_id: int) -> None:
        async with self._lock:
            await self._ensure_loaded()
            guild_bucket = self._cache.get(str(guild_id))
            if guild_bucket and str(user_id) in guild_bucket:
                del guild_bucket[str(user_id)]
                if not guild_bucket:
                    del self._cache[str(guild_id)]
                await self._persist()

    async def list_guild_characters(self, guild_id: int) -> Dict[int, Character]:
        """Return all characters stored for ``guild_id`` keyed by user id."""

        async with self._lock:
            await self._ensure_loaded()
            guild_bucket = self._cache.get(str(guild_id), {})
            characters: Dict[int, Character] = {}
            for user_id, payload in guild_bucket.items():
                try:
                    numeric_id = int(user_id)
                except (TypeError, ValueError):
                    continue
                try:
                    characters[numeric_id] = Character.from_dict(payload)
                except (KeyError, ValueError):
                    continue
            return characters

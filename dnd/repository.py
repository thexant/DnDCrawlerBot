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

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._storage_path.exists():
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache = {}
            self._loaded = True
            return
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

    async def _persist(self) -> None:
        text = json.dumps(self._cache, indent=2, sort_keys=True)
        await asyncio.to_thread(self._storage_path.write_text, text)

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

"""Persistence helpers for configurable guild game listings."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple


def _normalise_key(name: str) -> str:
    """Return a normalised lookup key for ``name``."""

    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class GuildGame:
    """Description of a game configured for a guild."""

    key: str
    title: str
    image_url: Optional[str] = None

    def to_payload(self) -> Dict[str, object]:
        payload: Dict[str, object] = {"title": self.title}
        if self.image_url:
            payload["image_url"] = self.image_url
        return payload

    @classmethod
    def from_payload(cls, key: str, payload: Mapping[str, object]) -> "GuildGame":
        title = str(payload.get("title") or key)
        image_value = payload.get("image_url")
        image_url = str(image_value).strip() if isinstance(image_value, str) else None
        if image_url == "":
            image_url = None
        return cls(key=key, title=title, image_url=image_url)


class GuildGameConfigStore:
    """Concurrency-safe storage for per-guild game configuration."""

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._loaded = False
        self._cache: Dict[str, Dict[str, Dict[str, object]]] = {}

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._storage_path.exists():
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache = {}
            self._loaded = True
            return
        text = await asyncio.to_thread(self._storage_path.read_text, encoding="utf-8")
        raw_cache: Dict[str, Dict[str, Dict[str, object]]] = {}
        if text.strip():
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                for guild_id, games in loaded.items():
                    if not isinstance(games, dict):
                        continue
                    bucket: Dict[str, Dict[str, object]] = {}
                    for key, payload in games.items():
                        if not isinstance(payload, dict):
                            continue
                        bucket[str(key)] = dict(payload)
                    raw_cache[str(guild_id)] = bucket
        self._cache = raw_cache
        self._loaded = True

    async def _persist(self) -> None:
        serialised: Dict[str, Dict[str, Dict[str, object]]] = {}
        for guild_id, games in self._cache.items():
            serialised[guild_id] = {
                key: dict(payload) for key, payload in games.items()
            }
        text = json.dumps(serialised, indent=2, sort_keys=True)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._storage_path.write_text, text, encoding="utf-8")
        self._loaded = True

    async def list_games(self, guild_id: int) -> Tuple[GuildGame, ...]:
        async with self._lock:
            await self._ensure_loaded()
            bucket = self._cache.get(str(guild_id), {})
            games = [
                GuildGame.from_payload(key, payload)
                for key, payload in bucket.items()
            ]
            games.sort(key=lambda game: game.title.casefold())
            return tuple(games)

    async def upsert_game(
        self,
        guild_id: int,
        *,
        name: str,
        image_url: Optional[str],
    ) -> GuildGame:
        key = _normalise_key(name)
        if not key:
            raise ValueError("Game name must not be empty")
        title = name.strip() or name
        async with self._lock:
            await self._ensure_loaded()
            bucket = self._cache.setdefault(str(guild_id), {})
            game = GuildGame(key=key, title=title, image_url=image_url)
            bucket[key] = game.to_payload()
            await self._persist()
            return game

    async def get_game(self, guild_id: int, identifier: str) -> Optional[GuildGame]:
        key = _normalise_key(identifier)
        async with self._lock:
            await self._ensure_loaded()
            bucket = self._cache.get(str(guild_id), {})
            payload = bucket.get(key)
            if payload is None:
                return None
            return GuildGame.from_payload(key, payload)

    async def remove_game(self, guild_id: int, identifier: str) -> bool:
        key = _normalise_key(identifier)
        async with self._lock:
            await self._ensure_loaded()
            bucket = self._cache.get(str(guild_id))
            if not bucket or key not in bucket:
                return False
            del bucket[key]
            if not bucket:
                del self._cache[str(guild_id)]
            await self._persist()
            return True

    async def replace_all(
        self, guild_id: int, games: Iterable[GuildGame]
    ) -> Tuple[GuildGame, ...]:
        async with self._lock:
            await self._ensure_loaded()
            bucket: Dict[str, Dict[str, object]] = {}
            for game in games:
                bucket[game.key] = game.to_payload()
            if bucket:
                self._cache[str(guild_id)] = bucket
            elif str(guild_id) in self._cache:
                del self._cache[str(guild_id)]
            await self._persist()
            stored = [
                GuildGame.from_payload(key, payload) for key, payload in bucket.items()
            ]
            stored.sort(key=lambda game: game.title.casefold())
            return tuple(stored)

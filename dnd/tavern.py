"""Persistence helpers for per-guild tavern configuration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple


@dataclass
class TavernConfig:
    """Stored information about a guild's tavern channel."""

    guild_id: int
    channel_id: int
    message_id: int | None = None


class TavernConfigStore:
    """Concurrency-safe storage for tavern configuration."""

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._loaded = False
        self._cache: Dict[str, TavernConfig] = {}

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
                cache: Dict[str, TavernConfig] = {}
                if isinstance(raw, dict):
                    for guild_id, payload in raw.items():
                        try:
                            numeric_id = int(guild_id)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        channel_id = payload.get("channel_id")
                        message_id = payload.get("message_id")
                        if not isinstance(channel_id, int):
                            continue
                        config = TavernConfig(
                            guild_id=numeric_id,
                            channel_id=channel_id,
                            message_id=message_id if isinstance(message_id, int) else None,
                        )
                        cache[str(numeric_id)] = config
                self._cache = cache
        self._loaded = True

    async def _persist(self) -> None:
        serialised = {
            guild_id: {
                "channel_id": config.channel_id,
                **({"message_id": config.message_id} if config.message_id is not None else {}),
            }
            for guild_id, config in self._cache.items()
        }
        text = json.dumps(serialised, indent=2, sort_keys=True)
        await asyncio.to_thread(self._storage_path.write_text, text, encoding="utf-8")

    async def get_config(self, guild_id: int) -> Optional[TavernConfig]:
        async with self._lock:
            await self._ensure_loaded()
            config = self._cache.get(str(guild_id))
            if config is None:
                return None
            return TavernConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                message_id=config.message_id,
            )

    async def set_channel(self, guild_id: int, channel_id: int) -> TavernConfig:
        async with self._lock:
            await self._ensure_loaded()
            config = TavernConfig(guild_id=guild_id, channel_id=channel_id)
            self._cache[str(guild_id)] = config
            await self._persist()
            return config

    async def update_message(self, guild_id: int, message_id: Optional[int]) -> Optional[TavernConfig]:
        async with self._lock:
            await self._ensure_loaded()
            config = self._cache.get(str(guild_id))
            if config is None:
                return None
            config.message_id = message_id
            await self._persist()
            return TavernConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                message_id=config.message_id,
            )

    async def all_configs(self) -> Tuple[TavernConfig, ...]:
        async with self._lock:
            await self._ensure_loaded()
            return tuple(
                TavernConfig(
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                    message_id=config.message_id,
                )
                for config in self._cache.values()
            )

    async def clear(self, guild_id: int) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            try:
                del self._cache[str(guild_id)]
            except KeyError:
                return False
            await self._persist()
            return True

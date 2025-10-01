"""Session management utilities for coordinating dungeon parties."""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, Generic, Optional, Tuple, TypeVar

SessionKey = Tuple[Optional[int], int]

T = TypeVar("T")


class SessionManager(Generic[T]):
    """Track active sessions keyed by guild and channel identifiers.

    The manager serialises access to the internal session mapping via an
    :class:`asyncio.Lock`. This prevents race conditions when multiple
    interactions try to mutate the same session concurrently (e.g. when
    several buttons are pressed at the same time).
    """

    __slots__ = ("_sessions", "_lock")

    def __init__(self) -> None:
        self._sessions: Dict[SessionKey, T] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(guild_id: Optional[int], channel_id: Optional[int]) -> SessionKey:
        """Build a stable key for a guild/channel pair."""

        if channel_id is None:
            raise ValueError("channel_id is required to build a session key")
        return (guild_id, channel_id)

    async def get(self, key: SessionKey) -> Optional[T]:
        """Return the session associated with ``key`` if it exists."""

        async with self._lock:
            return self._sessions.get(key)

    async def set(self, key: SessionKey, session: T) -> T:
        """Store or replace the ``session`` value for ``key``."""

        async with self._lock:
            self._sessions[key] = session
            return session

    async def pop(self, key: SessionKey) -> Optional[T]:
        """Remove and return the session for ``key`` if it exists."""

        async with self._lock:
            return self._sessions.pop(key, None)

    async def clear_guild(self, guild_id: int) -> int:
        """Remove all sessions associated with ``guild_id``.

        Returns the number of sessions removed.
        """

        async with self._lock:
            to_remove = [key for key in self._sessions if key[0] == guild_id]
            for key in to_remove:
                del self._sessions[key]
            return len(to_remove)

    async def update(self, key: SessionKey, mutator: Callable[[T], None]) -> Optional[T]:
        """Apply ``mutator`` to the session mapped to ``key``.

        The callable ``mutator`` is invoked while holding the internal lock and
        must therefore be synchronous.
        """

        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            mutator(session)
            return session

    async def keys(self) -> Tuple[SessionKey, ...]:
        """Return a snapshot of the active session keys."""

        async with self._lock:
            return tuple(self._sessions.keys())

    async def values(self) -> Tuple[T, ...]:
        """Return a snapshot of the active session objects."""

        async with self._lock:
            return tuple(self._sessions.values())

    @property
    def lock(self) -> asyncio.Lock:
        """Expose the internal lock for complex compound operations."""

        return self._lock

"""Registries for dungeon content."""

from __future__ import annotations

import random
from typing import Dict, Generic, Iterable, Iterator, Optional, Sequence, TypeVar

from .models import Item, Monster, Theme, Trap

__all__ = [
    "ItemRegistry",
    "MonsterRegistry",
    "ThemeRegistry",
    "TrapRegistry",
]

T = TypeVar("T")


class BaseRegistry(Generic[T]):
    """Utility container for validated content entries."""

    def __init__(self) -> None:
        self._entries: Dict[str, T] = {}
        self._aliases: Dict[str, str] = {}

    @staticmethod
    def _normalise(value: str) -> str:
        return value.strip().lower()

    def register(self, key: str, entry: T, *, aliases: Iterable[str] = ()) -> None:
        identifier = self._normalise(key)
        if identifier in self._entries:
            raise ValueError(f"Duplicate entry '{key}'")
        self._entries[identifier] = entry
        self._aliases[identifier] = identifier
        for alias in aliases:
            normalised = self._normalise(alias)
            self._aliases[normalised] = identifier

    def get(self, name: str) -> T:
        if not name:
            raise KeyError("Name must be provided")
        identifier = self._normalise(name)
        target = self._aliases.get(identifier, identifier)
        try:
            return self._entries[target]
        except KeyError as exc:
            raise KeyError(f"Unknown entry '{name}'") from exc

    def values(self) -> Sequence[T]:
        return tuple(self._entries.values())

    def keys(self) -> Sequence[str]:
        return tuple(self._entries.keys())

    def __iter__(self) -> Iterator[T]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def random_choice(self, rng: random.Random | None = None) -> T:
        if not self._entries:
            raise LookupError("Registry is empty")
        population = list(self._entries.values())
        if rng is None:
            return random.choice(population)
        return rng.choice(population)


class MonsterRegistry(BaseRegistry[Monster]):
    """Registry responsible for storing monsters."""

    def register(self, key: str, entry: Monster, *, aliases: Iterable[str] = ()) -> None:  # type: ignore[override]
        alias_set = list(aliases)
        alias_set.append(entry.name)
        alias_set.append(key)
        super().register(key, entry, aliases=alias_set)


class TrapRegistry(BaseRegistry[Trap]):
    """Registry responsible for storing traps."""

    def register(self, key: str, entry: Trap, *, aliases: Iterable[str] = ()) -> None:  # type: ignore[override]
        alias_set = list(aliases)
        alias_set.append(entry.name)
        alias_set.append(key)
        super().register(key, entry, aliases=alias_set)


class ItemRegistry(BaseRegistry[Item]):
    """Registry responsible for storing loot items."""

    def register(self, key: str, entry: Item, *, aliases: Iterable[str] = ()) -> None:  # type: ignore[override]
        alias_set = list(aliases)
        alias_set.append(entry.name)
        alias_set.append(key)
        super().register(key, entry, aliases=alias_set)


class ThemeRegistry(BaseRegistry[Theme]):
    """Registry responsible for storing dungeon themes."""

    def register(self, key: str, entry: Theme, *, aliases: Iterable[str] = ()) -> None:  # type: ignore[override]
        alias_set = list(aliases)
        alias_set.append(entry.name)
        alias_set.append(key)
        super().register(key, entry, aliases=alias_set)

    def first(self) -> Optional[Theme]:
        try:
            return next(iter(self))
        except StopIteration:
            return None

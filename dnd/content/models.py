"""Schema models for dungeon content."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Mapping, MutableMapping, Sequence

__all__ = [
    "EncounterTable",
    "Item",
    "Monster",
    "RoomTemplate",
    "Theme",
    "Trap",
]


class SchemaError(ValueError):
    """Raised when content data fails validation."""


def _coerce_mapping(name: str, value: object) -> MutableMapping[str, object]:
    if isinstance(value, MutableMapping):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise SchemaError(f"{name} must be a mapping")


def _coerce_sequence(name: str, value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise SchemaError(f"{name} must be a sequence")


@dataclass(frozen=True)
class Monster:
    """Static data describing a monster that can appear in encounters."""

    key: str
    name: str
    challenge: float
    armor_class: int
    hit_points: int
    attack_bonus: int
    damage: str
    ability_scores: Mapping[str, int] = field(default_factory=dict)
    tags: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, key: str, data: Mapping[str, object]) -> "Monster":
        mapping = _coerce_mapping("monster", data)
        name = str(mapping.get("name") or key)
        challenge = float(mapping.get("challenge", 0))
        armor_class = int(mapping.get("armor_class", 10))
        hit_points = int(mapping.get("hit_points", 1))
        attack_bonus = int(mapping.get("attack_bonus", 0))
        damage = str(mapping.get("damage", "1d6"))
        ability_raw = mapping.get("ability_scores", {})
        ability_scores: dict[str, int] = {}
        if ability_raw:
            ability_map = _coerce_mapping("ability_scores", ability_raw)
            for ability, score in ability_map.items():
                ability_scores[str(ability)] = int(score)
        tags_raw = mapping.get("tags", ())
        tags: tuple[str, ...] = ()
        if tags_raw:
            tags_seq = _coerce_sequence("tags", tags_raw)
            tags = tuple(str(tag) for tag in tags_seq)
        return cls(
            key=str(key).lower(),
            name=name,
            challenge=challenge,
            armor_class=armor_class,
            hit_points=hit_points,
            attack_bonus=attack_bonus,
            damage=damage,
            ability_scores=ability_scores,
            tags=tags,
        )


@dataclass(frozen=True)
class Trap:
    """Static data describing an interactable trap."""

    key: str
    name: str
    description: str
    saving_throw: Mapping[str, object] | None = None
    damage: str | None = None
    tags: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, key: str, data: Mapping[str, object]) -> "Trap":
        mapping = _coerce_mapping("trap", data)
        name = str(mapping.get("name") or key)
        description = str(mapping.get("description", ""))
        saving_throw_raw = mapping.get("saving_throw")
        saving_throw: Mapping[str, object] | None = None
        if saving_throw_raw is not None:
            saving_throw = _coerce_mapping("saving_throw", saving_throw_raw)
        damage_raw = mapping.get("damage")
        damage = str(damage_raw) if damage_raw is not None else None
        tags_raw = mapping.get("tags", ())
        tags: tuple[str, ...] = ()
        if tags_raw:
            tags_seq = _coerce_sequence("tags", tags_raw)
            tags = tuple(str(tag) for tag in tags_seq)
        return cls(
            key=str(key).lower(),
            name=name,
            description=description,
            saving_throw=saving_throw,
            damage=damage,
            tags=tags,
        )


@dataclass(frozen=True)
class Item:
    """Static data describing loot items."""

    key: str
    name: str
    rarity: str
    description: str | None = None
    tags: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, key: str, data: Mapping[str, object]) -> "Item":
        mapping = _coerce_mapping("item", data)
        name = str(mapping.get("name") or key)
        rarity = str(mapping.get("rarity", "Common"))
        description_raw = mapping.get("description")
        description = str(description_raw) if description_raw is not None else None
        tags_raw = mapping.get("tags", ())
        tags: tuple[str, ...] = ()
        if tags_raw:
            tags_seq = _coerce_sequence("tags", tags_raw)
            tags = tuple(str(tag) for tag in tags_seq)
        return cls(
            key=str(key).lower(),
            name=name,
            rarity=rarity,
            description=description,
            tags=tags,
        )


@dataclass(frozen=True)
class RoomTemplate:
    """Template used during room generation."""

    name: str
    description: str
    encounter_weights: Mapping[str, int] = field(default_factory=dict)
    weight: int = 1
    tags: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "RoomTemplate":
        mapping = _coerce_mapping("room_template", data)
        name = str(mapping.get("name", "Unknown Room"))
        description = str(mapping.get("description", ""))
        weights_raw = mapping.get("encounter_weights", {})
        weights: dict[str, int] = {}
        if weights_raw:
            weights_map = _coerce_mapping("encounter_weights", weights_raw)
            for key, value in weights_map.items():
                weights[str(key)] = int(value)
        weight = int(mapping.get("weight", 1))
        tags_raw = mapping.get("tags", ())
        tags: tuple[str, ...] = ()
        if tags_raw:
            tags_seq = _coerce_sequence("tags", tags_raw)
            tags = tuple(str(tag) for tag in tags_seq)
        return cls(
            name=name,
            description=description,
            encounter_weights=weights,
            weight=max(1, weight),
            tags=tags,
        )


class EncounterTable:
    """Weighted table used to select an encounter type or entry."""

    def __init__(self, entries: Mapping[str, int]) -> None:
        cleaned: dict[str, int] = {}
        for key, value in entries.items():
            weight = int(value)
            if weight > 0:
                cleaned[str(key)] = weight
        if not cleaned:
            raise SchemaError("Encounter table must contain at least one positive weight entry")
        self._entries = cleaned

    def roll(self, rng: random.Random) -> str:
        population = list(self._entries.keys())
        weights = [self._entries[key] for key in population]
        return rng.choices(population, weights=weights, k=1)[0]

    def entries(self) -> Mapping[str, int]:
        return dict(self._entries)


def _rarity_score(rarity: str) -> int:
    order = {
        "common": 0,
        "uncommon": 1,
        "rare": 2,
        "very rare": 3,
        "legendary": 4,
        "artifact": 5,
    }
    return order.get(rarity.strip().lower(), 0)


def _trap_dc(trap: Trap) -> float:
    if trap.saving_throw and isinstance(trap.saving_throw, Mapping):
        dc = trap.saving_throw.get("dc")
        if isinstance(dc, (int, float)):
            return float(dc)
    return 10.0


@dataclass(frozen=True)
class Theme:
    """Domain model describing a dungeon theme."""

    key: str
    name: str
    description: str
    room_templates: Sequence[RoomTemplate]
    monsters: Sequence[Monster]
    traps: Sequence[Trap]
    loot: Sequence[Item]
    encounter_table: EncounterTable

    def random_room_template(self, rng: random.Random) -> RoomTemplate:
        if not self.room_templates:
            raise SchemaError(f"Theme '{self.name}' has no room templates")
        weights = [max(1, template.weight) for template in self.room_templates]
        return rng.choices(list(self.room_templates), weights=weights, k=1)[0]

    def random_monsters(
        self,
        rng: random.Random,
        count: int,
        *,
        challenge_bias: float = 1.0,
        min_challenge: float | None = None,
        max_challenge: float | None = None,
    ) -> Sequence[Monster]:
        if not self.monsters or count <= 0:
            return ()
        population = list(self.monsters)
        filtered: list[Monster] = []
        for monster in population:
            challenge = float(monster.challenge)
            if min_challenge is not None and challenge < min_challenge:
                continue
            if max_challenge is not None and challenge > max_challenge:
                continue
            filtered.append(monster)
        if not filtered:
            if min_challenge is not None:
                highest = max(float(monster.challenge) for monster in population)
                filtered = [
                    monster
                    for monster in population
                    if float(monster.challenge) == highest
                ]
            elif max_challenge is not None:
                lowest = min(float(monster.challenge) for monster in population)
                filtered = [
                    monster
                    for monster in population
                    if float(monster.challenge) == lowest
                ]
            else:
                filtered = population
        weights = []
        for monster in filtered:
            base = max(0.0, float(monster.challenge)) + 1.0
            weight = base ** challenge_bias
            weights.append(max(weight, 1e-6))
        return tuple(rng.choices(filtered, weights=weights, k=count))

    def random_traps(
        self,
        rng: random.Random,
        count: int = 1,
        *,
        danger_bias: float = 1.0,
        min_dc: float | None = None,
        max_dc: float | None = None,
    ) -> Sequence[Trap]:
        if not self.traps or count <= 0:
            return ()
        population = list(self.traps)
        filtered: list[Trap] = []
        for trap in population:
            dc = _trap_dc(trap)
            if min_dc is not None and dc < min_dc:
                continue
            if max_dc is not None and dc > max_dc:
                continue
            filtered.append(trap)
        if not filtered:
            if min_dc is not None:
                highest = max(_trap_dc(trap) for trap in population)
                filtered = [trap for trap in population if _trap_dc(trap) == highest]
            elif max_dc is not None:
                lowest = min(_trap_dc(trap) for trap in population)
                filtered = [trap for trap in population if _trap_dc(trap) == lowest]
            else:
                filtered = population
        weights = []
        for trap in filtered:
            dc = _trap_dc(trap)
            base = max(1.0, dc)
            weight = base ** danger_bias
            weights.append(max(weight, 1e-6))
        return tuple(rng.choices(filtered, weights=weights, k=count))

    def random_trap(
        self,
        rng: random.Random,
        *,
        danger_bias: float = 1.0,
        min_dc: float | None = None,
        max_dc: float | None = None,
    ) -> Sequence[Trap]:
        return self.random_traps(
            rng,
            1,
            danger_bias=danger_bias,
            min_dc=min_dc,
            max_dc=max_dc,
        )

    def random_loot(
        self,
        rng: random.Random,
        count: int = 1,
        *,
        rarity_bias: float = 1.0,
    ) -> Sequence[Item]:
        if not self.loot or count <= 0:
            return ()
        population = list(self.loot)
        weights = []
        for item in population:
            base = float(_rarity_score(item.rarity)) + 1.0
            weight = base ** rarity_bias
            weights.append(max(weight, 1e-6))
        return tuple(rng.choices(population, weights=weights, k=count))

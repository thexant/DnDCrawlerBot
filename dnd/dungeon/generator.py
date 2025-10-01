"""Procedural dungeon generation utilities."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

__all__ = [
    "Corridor",
    "Dungeon",
    "DungeonGenerator",
    "EncounterResult",
    "EncounterTable",
    "LootDefinition",
    "MonsterDefinition",
    "Room",
    "RoomTemplate",
    "Theme",
    "ThemeRegistry",
    "TrapDefinition",
]


@dataclass(frozen=True)
class MonsterDefinition:
    """Static data describing a monster that can appear in encounters."""

    name: str
    challenge: float
    armor_class: int
    hit_points: int
    attack_bonus: int
    damage: str
    ability_scores: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TrapDefinition:
    """Static data describing a trap."""

    name: str
    description: str
    saving_throw: Mapping[str, object] | None = None
    damage: str | None = None


@dataclass(frozen=True)
class LootDefinition:
    """Static data describing potential loot."""

    name: str
    rarity: str
    description: str | None = None


@dataclass(frozen=True)
class RoomTemplate:
    """Template used during room generation."""

    name: str
    description: str
    encounter_weights: Mapping[str, int] = field(default_factory=dict)
    weight: int = 1
    tags: Sequence[str] = field(default_factory=tuple)


class EncounterTable:
    """Weighted table used to select an encounter type or entry."""

    def __init__(self, entries: Mapping[str, int]) -> None:
        self._entries: Dict[str, int] = {
            key: int(value)
            for key, value in entries.items()
            if int(value) > 0
        }
        if not self._entries:
            raise ValueError("Encounter table must contain at least one positive weight entry")

    def roll(self, rng: random.Random) -> str:
        population = list(self._entries.keys())
        weights = [self._entries[key] for key in population]
        return rng.choices(population, weights=weights, k=1)[0]

    def entries(self) -> Mapping[str, int]:
        return dict(self._entries)


@dataclass
class EncounterResult:
    """Result of generating a single encounter for a room."""

    kind: str
    summary: str
    monsters: Sequence[MonsterDefinition] = field(default_factory=tuple)
    traps: Sequence[TrapDefinition] = field(default_factory=tuple)
    loot: Sequence[LootDefinition] = field(default_factory=tuple)


@dataclass
class Room:
    """Generated room with descriptive text and encounter details."""

    id: int
    name: str
    description: str
    encounter: EncounterResult
    exits: Sequence[int] = field(default_factory=tuple)


@dataclass
class Corridor:
    """Connection between two rooms."""

    from_room: int
    to_room: int
    description: str


@dataclass
class Dungeon:
    """Generated dungeon consisting of rooms and connecting corridors."""

    name: str
    seed: int | None
    theme: "Theme"
    rooms: Sequence[Room]
    corridors: Sequence[Corridor]

    def get_room(self, room_id: int) -> Room:
        for room in self.rooms:
            if room.id == room_id:
                return room
        raise KeyError(room_id)


@dataclass(frozen=True)
class Theme:
    """Domain model describing a dungeon theme."""

    name: str
    description: str
    room_templates: Sequence[RoomTemplate]
    monsters: Sequence[MonsterDefinition]
    traps: Sequence[TrapDefinition]
    loot: Sequence[LootDefinition]
    encounter_table: EncounterTable

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Theme":
        name = str(data["name"])
        description = str(data.get("description", ""))

        templates: List[RoomTemplate] = []
        for template_data in data.get("room_templates", []):
            template = RoomTemplate(
                name=str(template_data.get("name", "Unknown Room")),
                description=str(template_data.get("description", "")),
                encounter_weights={
                    str(key): int(value)
                    for key, value in dict(template_data.get("encounter_weights", {})).items()
                },
                weight=int(template_data.get("weight", 1)),
                tags=tuple(template_data.get("tags", [])),
            )
            templates.append(template)

        monster_defs: List[MonsterDefinition] = []
        for monster_data in data.get("monsters", []):
            monster_defs.append(
                MonsterDefinition(
                    name=str(monster_data.get("name", "Mysterious Entity")),
                    challenge=float(monster_data.get("challenge", 0)),
                    armor_class=int(monster_data.get("armor_class", 10)),
                    hit_points=int(monster_data.get("hit_points", 5)),
                    attack_bonus=int(monster_data.get("attack_bonus", 0)),
                    damage=str(monster_data.get("damage", "1d6")),
                    ability_scores=dict(monster_data.get("ability_scores", {})),
                )
            )

        trap_defs: List[TrapDefinition] = []
        for trap_data in data.get("traps", []):
            trap_defs.append(
                TrapDefinition(
                    name=str(trap_data.get("name", "Hidden Trap")),
                    description=str(trap_data.get("description", "")),
                    saving_throw=dict(trap_data.get("saving_throw")) if trap_data.get("saving_throw") else None,
                    damage=(str(trap_data.get("damage")) if trap_data.get("damage") else None),
                )
            )

        loot_defs: List[LootDefinition] = []
        for loot_data in data.get("loot", []):
            loot_defs.append(
                LootDefinition(
                    name=str(loot_data.get("name", "Treasure")),
                    rarity=str(loot_data.get("rarity", "Common")),
                    description=(str(loot_data.get("description")) if loot_data.get("description") else None),
                )
            )

        encounter_data = dict(data.get("encounters", {}))
        if not encounter_data:
            encounter_data = {"combat": 3, "trap": 1, "treasure": 1, "empty": 1}

        encounter_table = EncounterTable(
            {
                str(key): int(value)
                for key, value in encounter_data.items()
            }
        )

        return cls(
            name=name,
            description=description,
            room_templates=tuple(templates),
            monsters=tuple(monster_defs),
            traps=tuple(trap_defs),
            loot=tuple(loot_defs),
            encounter_table=encounter_table,
        )

    def random_room_template(self, rng: random.Random) -> RoomTemplate:
        if not self.room_templates:
            raise ValueError(f"Theme '{self.name}' has no room templates")
        weights = [max(1, template.weight) for template in self.room_templates]
        return rng.choices(list(self.room_templates), weights=weights, k=1)[0]

    def random_monsters(self, rng: random.Random, count: int) -> Sequence[MonsterDefinition]:
        if not self.monsters:
            return ()
        return tuple(rng.choices(list(self.monsters), k=count))

    def random_trap(self, rng: random.Random) -> Sequence[TrapDefinition]:
        if not self.traps:
            return ()
        return (rng.choice(list(self.traps)),)

    def random_loot(self, rng: random.Random, count: int = 1) -> Sequence[LootDefinition]:
        if not self.loot:
            return ()
        return tuple(rng.choices(list(self.loot), k=count))


class ThemeRegistry:
    """Registry responsible for loading and providing access to themes."""

    def __init__(self) -> None:
        self._themes: Dict[str, Theme] = {}

    def register(self, theme: Theme) -> None:
        self._themes[theme.name.lower()] = theme

    def get(self, name: str) -> Theme:
        try:
            return self._themes[name.lower()]
        except KeyError as exc:
            raise KeyError(f"Theme '{name}' is not registered") from exc

    def values(self) -> Sequence[Theme]:
        return tuple(self._themes.values())

    @classmethod
    def load_from_path(cls, path: Path) -> "ThemeRegistry":
        registry = cls()
        if not path.exists():
            return registry
        for file_path in sorted(path.glob("*.json")):
            with file_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            theme = Theme.from_dict(data)
            registry.register(theme)
        return registry


class DungeonGenerator:
    """Generate dungeons from theme data using deterministic RNG."""

    def __init__(self, theme: Theme, seed: int | None = None) -> None:
        self.theme = theme
        self.seed = seed
        self._rng = random.Random(seed)

    def generate(self, *, room_count: int = 5, name: str | None = None) -> Dungeon:
        if room_count <= 0:
            raise ValueError("room_count must be positive")

        rooms: List[Room] = []
        corridors: List[Corridor] = []

        for index in range(room_count):
            room = self._generate_room(index)
            rooms.append(room)
            if index > 0:
                corridor = self._generate_corridor(rooms[index - 1], room)
                corridors.append(corridor)

        dungeon_name = name or f"{self.theme.name} Expedition"
        return Dungeon(
            name=dungeon_name,
            seed=self.seed,
            theme=self.theme,
            rooms=tuple(rooms),
            corridors=tuple(corridors),
        )

    def _generate_room(self, room_index: int) -> Room:
        template = self.theme.random_room_template(self._rng)
        encounter_kind = self._select_encounter_kind(template)
        encounter = self._build_encounter(encounter_kind)

        description_parts = [template.description]
        if encounter.summary:
            description_parts.append(encounter.summary)
        description = "\n\n".join(part.strip() for part in description_parts if part)

        exits = () if room_index == 0 else (room_index - 1,)
        return Room(
            id=room_index,
            name=template.name,
            description=description,
            encounter=encounter,
            exits=exits,
        )

    def _generate_corridor(self, from_room: Room, to_room: Room) -> Corridor:
        length_descriptor = self._rng.choice(["short", "winding", "shadowy", "ancient"])
        adornment = self._rng.choice(["etched runes", "broken statues", "hanging roots", "flickering torches"])
        description = f"A {length_descriptor} corridor lined with {adornment}."
        return Corridor(from_room=from_room.id, to_room=to_room.id, description=description)

    def _select_encounter_kind(self, template: RoomTemplate) -> str:
        if template.encounter_weights:
            table = EncounterTable(template.encounter_weights)
        else:
            table = self.theme.encounter_table
        return table.roll(self._rng)

    def _build_encounter(self, kind: str) -> EncounterResult:
        if kind == "combat":
            monster_count = self._rng.randint(1, max(1, min(3, len(self.theme.monsters) or 1)))
            monsters = self.theme.random_monsters(self._rng, monster_count)
            monster_names = ", ".join(monster.name for monster in monsters)
            summary = f"Hostile presence detected: {monster_names}."
            loot = self.theme.random_loot(self._rng, self._rng.randint(0, 2))
            return EncounterResult(kind=kind, summary=summary, monsters=monsters, loot=loot)
        if kind == "trap":
            traps = self.theme.random_trap(self._rng)
            trap_names = ", ".join(trap.name for trap in traps) if traps else "Subtle hazard"
            summary = f"A trap awaits: {trap_names}."
            return EncounterResult(kind=kind, summary=summary, traps=traps)
        if kind == "treasure":
            loot = self.theme.random_loot(self._rng, self._rng.randint(1, 3))
            summary = "Hidden cache discovered." if loot else "Dusty alcoves hold no treasure."
            return EncounterResult(kind=kind, summary=summary, loot=loot)
        if kind == "empty":
            summary = "The chamber is eerily silent, devoid of immediate threats."
            return EncounterResult(kind=kind, summary=summary)
        # Fallback: treat as narrative flavor
        summary = f"An unusual phenomenon tied to {self.theme.name.lower()} energies occurs."
        return EncounterResult(kind=kind, summary=summary)

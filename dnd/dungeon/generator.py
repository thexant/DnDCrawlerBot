"""Procedural dungeon generation utilities."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence

from dnd.content import EncounterTable, Item, Monster, RoomTemplate, Theme, Trap

__all__ = [
    "Corridor",
    "Dungeon",
    "DungeonGenerator",
    "EncounterResult",
    "EncounterTable",
    "Item",
    "Monster",
    "Room",
    "RoomTemplate",
    "Theme",
    "Trap",
]


@dataclass
class EncounterResult:
    """Result of generating a single encounter for a room."""

    kind: str
    summary: str
    monsters: Sequence[Monster] = field(default_factory=tuple)
    traps: Sequence[Trap] = field(default_factory=tuple)
    loot: Sequence[Item] = field(default_factory=tuple)


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
    theme: Theme
    rooms: Sequence[Room]
    corridors: Sequence[Corridor]

    def get_room(self, room_id: int) -> Room:
        for room in self.rooms:
            if room.id == room_id:
                return room
        raise KeyError(room_id)


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

"""Procedural dungeon generation utilities."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from dnd.content import EncounterTable, Item, Monster, RoomTemplate, Theme, Trap

__all__ = [
    "Corridor",
    "Dungeon",
    "DungeonGenerator",
    "EncounterResult",
    "EncounterTable",
    "Item",
    "Monster",
    "RoomExit",
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
    exits: Sequence["RoomExit"] = field(default_factory=tuple)
    position: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class RoomExit:
    """A named passage that links one room to another."""

    key: str
    label: str
    destination: int
    description: str | None = None


@dataclass
class Corridor:
    """Connection between two rooms."""

    from_room: int
    to_room: int
    description: str
    from_label: str
    to_label: str


@dataclass
class Dungeon:
    """Generated dungeon consisting of rooms and connecting corridors."""

    name: str
    seed: int | None
    theme: Theme
    difficulty: str
    rooms: Sequence[Room]
    corridors: Sequence[Corridor]
    room_positions: Dict[int, tuple[int, int]] = field(default_factory=dict)

    def get_room(self, room_id: int) -> Room:
        for room in self.rooms:
            if room.id == room_id:
                return room
        raise KeyError(room_id)


@dataclass(frozen=True)
class DifficultyProfile:
    """Configuration tweaks applied when generating encounters."""

    monster_count: tuple[int, int]
    challenge_bias: float
    trap_count: tuple[int, int]
    trap_danger_bias: float
    trap_min_dc: float | None
    trap_max_dc: float | None
    loot_combat: tuple[int, int]
    loot_treasure: tuple[int, int]
    loot_rarity_bias: float
    min_monster_challenge: float | None = None
    max_monster_challenge: float | None = None


DIFFICULTY_PROFILES: Dict[str, DifficultyProfile] = {
    "story": DifficultyProfile(
        monster_count=(0, 0),
        challenge_bias=0.2,
        trap_count=(1, 1),
        trap_danger_bias=0.3,
        trap_min_dc=None,
        trap_max_dc=11,
        loot_combat=(0, 1),
        loot_treasure=(0, 1),
        loot_rarity_bias=0.2,
    ),
    "easy": DifficultyProfile(
        monster_count=(1, 2),
        challenge_bias=0.6,
        max_monster_challenge=2.0,
        trap_count=(1, 1),
        trap_danger_bias=0.5,
        trap_min_dc=None,
        trap_max_dc=13,
        loot_combat=(0, 1),
        loot_treasure=(1, 2),
        loot_rarity_bias=0.5,
    ),
    "standard": DifficultyProfile(
        monster_count=(1, 3),
        challenge_bias=1.0,
        min_monster_challenge=0.5,
        trap_count=(1, 2),
        trap_danger_bias=1.0,
        trap_min_dc=None,
        trap_max_dc=None,
        loot_combat=(0, 2),
        loot_treasure=(1, 3),
        loot_rarity_bias=1.0,
    ),
    "challenging": DifficultyProfile(
        monster_count=(2, 4),
        challenge_bias=1.3,
        min_monster_challenge=1.0,
        trap_count=(1, 2),
        trap_danger_bias=1.3,
        trap_min_dc=13,
        trap_max_dc=None,
        loot_combat=(1, 2),
        loot_treasure=(2, 4),
        loot_rarity_bias=1.5,
    ),
    "hard": DifficultyProfile(
        monster_count=(3, 5),
        challenge_bias=1.7,
        min_monster_challenge=2.0,
        trap_count=(2, 3),
        trap_danger_bias=1.8,
        trap_min_dc=15,
        trap_max_dc=None,
        loot_combat=(1, 3),
        loot_treasure=(3, 5),
        loot_rarity_bias=2.4,
    ),
    "deadly": DifficultyProfile(
        monster_count=(5, 7),
        challenge_bias=2.6,
        min_monster_challenge=3.0,
        trap_count=(3, 4),
        trap_danger_bias=2.6,
        trap_min_dc=17,
        trap_max_dc=None,
        loot_combat=(2, 4),
        loot_treasure=(4, 6),
        loot_rarity_bias=3.4,
    ),
}


class DungeonGenerator:
    """Generate dungeons from theme data using deterministic RNG."""

    def __init__(self, theme: Theme, seed: int | None = None, *, difficulty: str = "standard") -> None:
        self.theme = theme
        self.seed = seed
        self._rng = random.Random(seed)
        self.difficulty = self._normalise_difficulty(difficulty)

    def _normalise_difficulty(self, difficulty: str | None) -> str:
        if not difficulty:
            return "standard"
        lowered = difficulty.lower()
        if lowered not in DIFFICULTY_PROFILES:
            return "standard"
        return lowered

    def _get_profile(self, difficulty: str) -> DifficultyProfile:
        return DIFFICULTY_PROFILES.get(difficulty, DIFFICULTY_PROFILES["standard"])

    def generate(
        self,
        *,
        room_count: int = 5,
        name: str | None = None,
        difficulty: str | None = None,
    ) -> Dungeon:
        if room_count <= 0:
            raise ValueError("room_count must be positive")

        active_difficulty = self._normalise_difficulty(difficulty) if difficulty else self.difficulty

        rooms: List[Room] = []
        corridors: List[Corridor] = []
        positions: Dict[int, tuple[int, int]] = {}
        occupied: set[tuple[int, int]] = set()
        adjacency: Dict[int, set[int]] = defaultdict(set)

        def add_corridor_between(first: int, second: int) -> None:
            if first == second:
                return
            if second in adjacency[first]:
                return
            corridor = self._generate_corridor(rooms[first], rooms[second])
            corridors.append(corridor)
            adjacency[first].add(second)
            adjacency[second].add(first)

        def allocate_position(room_id: int, *, anchor: int | None = None) -> tuple[int, int]:
            if not positions:
                origin = (0, 0)
                positions[room_id] = origin
                occupied.add(origin)
                return origin

            candidates = list(range(room_id)) if anchor is None else [anchor]
            if not candidates:
                origin = (0, 0)
                positions[room_id] = origin
                occupied.add(origin)
                return origin

            self._rng.shuffle(candidates)
            offsets = [(1, 0), (-1, 0), (0, 1), (0, -1)]

            for candidate_anchor in candidates:
                anchor_position = positions[candidate_anchor]
                shuffled_offsets = offsets[:]
                self._rng.shuffle(shuffled_offsets)
                for dx, dy in shuffled_offsets:
                    candidate_position = (anchor_position[0] + dx, anchor_position[1] + dy)
                    if candidate_position not in occupied:
                        positions[room_id] = candidate_position
                        occupied.add(candidate_position)
                        return candidate_position

            # Fallback: expand search outward from chosen anchor until a space is found.
            fallback_anchor = candidates[0]
            anchor_position = positions[fallback_anchor]
            radius = 2
            while True:
                potential: list[tuple[int, int]] = []
                for dx in range(-radius, radius + 1):
                    for dy in range(-radius, radius + 1):
                        if abs(dx) + abs(dy) != radius:
                            continue
                        candidate_position = (anchor_position[0] + dx, anchor_position[1] + dy)
                        if candidate_position not in occupied:
                            potential.append(candidate_position)
                if potential:
                    choice = self._rng.choice(potential)
                    positions[room_id] = choice
                    occupied.add(choice)
                    return choice
                radius += 1

        for index in range(room_count):
            room = self._generate_room(index, difficulty=active_difficulty)
            rooms.append(room)
            adjacency[room.id]  # ensure key initialised
            if index == 0:
                room.position = allocate_position(room.id)
                continue

            previous_indices = list(range(index))
            primary_candidates = previous_indices[:]
            self._rng.shuffle(primary_candidates)
            primary = primary_candidates[0]
            for candidate in primary_candidates:
                anchor_position = positions[candidate]
                neighbors = [
                    (anchor_position[0] + dx, anchor_position[1] + dy)
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                ]
                if any(neighbor not in occupied for neighbor in neighbors):
                    primary = candidate
                    break
            room.position = allocate_position(room.id, anchor=primary)
            add_corridor_between(primary, index)

            alternate_candidates = [candidate for candidate in previous_indices if candidate != primary]
            if alternate_candidates and self._rng.random() < 0.35:
                secondary = self._rng.choice(alternate_candidates)
                add_corridor_between(secondary, index)

        if room_count > 2:
            potential_pairs: list[tuple[int, int]] = []
            for first in range(room_count):
                for second in range(first + 1, room_count):
                    if second not in adjacency[first]:
                        potential_pairs.append((first, second))
            self._rng.shuffle(potential_pairs)
            extra_corridors = self._rng.randint(0, max(1, room_count // 2))
            added = 0
            for first, second in potential_pairs:
                if added >= extra_corridors:
                    break
                add_corridor_between(first, second)
                added += 1

        exits_map: dict[int, list[RoomExit]] = defaultdict(list)
        for corridor_index, corridor in enumerate(corridors):
            corridor_key = f"c{corridor_index}"
            exits_map[corridor.from_room].append(
                RoomExit(
                    key=f"{corridor_key}:r{corridor.from_room}->r{corridor.to_room}",
                    label=corridor.from_label,
                    destination=corridor.to_room,
                    description=corridor.description,
                )
            )
            exits_map[corridor.to_room].append(
                RoomExit(
                    key=f"{corridor_key}:r{corridor.to_room}->r{corridor.from_room}",
                    label=corridor.to_label,
                    destination=corridor.from_room,
                    description=corridor.description,
                )
            )

        for room in rooms:
            room.exits = tuple(exits_map.get(room.id, ()))
            if room.id in positions:
                room.position = positions[room.id]

        dungeon_name = name or f"{self.theme.name} Expedition"
        return Dungeon(
            name=dungeon_name,
            seed=self.seed,
            theme=self.theme,
            difficulty=active_difficulty,
            rooms=tuple(rooms),
            corridors=tuple(corridors),
            room_positions=dict(positions),
        )

    def _generate_room(self, room_index: int, *, difficulty: str) -> Room:
        template = self.theme.random_room_template(self._rng)
        encounter_kind = self._select_encounter_kind(template)
        encounter = self._build_encounter(encounter_kind, difficulty)

        description_parts = [template.description]
        if encounter.summary:
            description_parts.append(encounter.summary)
        description = "\n\n".join(part.strip() for part in description_parts if part)

        return Room(
            id=room_index,
            name=template.name,
            description=description,
            encounter=encounter,
        )

    def _generate_corridor(self, from_room: Room, to_room: Room) -> Corridor:
        length_descriptor = self._rng.choice(["short", "winding", "shadowy", "ancient"])
        adornment = self._rng.choice(["etched runes", "broken statues", "hanging roots", "flickering torches"])
        description = f"A {length_descriptor} corridor lined with {adornment}."
        label_pairs = [
            ("Northern passage", "Southern passage"),
            ("Eastern archway", "Western archway"),
            ("Ascending stair", "Descending stair"),
            ("Ironbound door", "Rearward arch"),
            ("Glowing hallway", "Shadowed hallway"),
            ("Left-hand path", "Right-hand path"),
        ]
        from_label, to_label = self._rng.choice(label_pairs)
        return Corridor(
            from_room=from_room.id,
            to_room=to_room.id,
            description=description,
            from_label=from_label,
            to_label=to_label,
        )

    def _select_encounter_kind(self, template: RoomTemplate) -> str:
        if template.encounter_weights:
            table = EncounterTable(template.encounter_weights)
        else:
            table = self.theme.encounter_table
        return table.roll(self._rng)

    def _build_encounter(self, kind: str, difficulty: str) -> EncounterResult:
        profile = self._get_profile(difficulty)
        if kind == "combat":
            min_monsters, max_monsters = profile.monster_count
            if max_monsters < min_monsters:
                max_monsters = min_monsters
            monster_count = self._rng.randint(max(0, min_monsters), max(0, max_monsters))
            monsters = self.theme.random_monsters(
                self._rng,
                monster_count,
                challenge_bias=profile.challenge_bias,
                min_challenge=profile.min_monster_challenge,
                max_challenge=profile.max_monster_challenge,
            )
            if monsters:
                monster_names = ", ".join(monster.name for monster in monsters)
                summary = f"Hostile presence detected ({difficulty.title()}): {monster_names}."
            else:
                summary = (
                    "The chamber was primed for battle, but no foes answered the call."
                )
            loot_count = self._rng.randint(
                max(0, profile.loot_combat[0]),
                max(0, max(profile.loot_combat[0], profile.loot_combat[1])),
            )
            loot = self.theme.random_loot(
                self._rng,
                loot_count,
                rarity_bias=profile.loot_rarity_bias,
            )
            return EncounterResult(kind=kind, summary=summary, monsters=monsters, loot=loot)
        if kind == "trap":
            min_traps, max_traps = profile.trap_count
            if max_traps < min_traps:
                max_traps = min_traps
            trap_count = self._rng.randint(max(1, min_traps), max(1, max_traps))
            traps = self.theme.random_traps(
                self._rng,
                trap_count,
                danger_bias=profile.trap_danger_bias,
                min_dc=profile.trap_min_dc,
                max_dc=profile.trap_max_dc,
            )
            if traps:
                trap_names = ", ".join(trap.name for trap in traps)
            else:
                trap_names = "Subtle hazard"
            summary = f"A trap calibrated for {difficulty.title()} adventurers: {trap_names}."
            return EncounterResult(kind=kind, summary=summary, traps=traps)
        if kind == "treasure":
            min_loot, max_loot = profile.loot_treasure
            if max_loot < min_loot:
                max_loot = min_loot
            loot_count = self._rng.randint(max(0, min_loot), max(0, max_loot))
            loot = self.theme.random_loot(
                self._rng,
                loot_count,
                rarity_bias=profile.loot_rarity_bias,
            )
            summary = (
                "Hidden cache discovered." if loot else "Dusty alcoves hold no treasure."
            )
            return EncounterResult(kind=kind, summary=summary, loot=loot)
        if kind == "empty":
            summary = "The chamber is eerily silent, devoid of immediate threats."
            return EncounterResult(kind=kind, summary=summary)
        # Fallback: treat as narrative flavor
        summary = f"An unusual phenomenon tied to {self.theme.name.lower()} energies occurs."
        return EncounterResult(kind=kind, summary=summary)

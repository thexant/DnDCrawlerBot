"""Domain models and helpers for Dungeons & Dragons characters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping

ABILITY_NAMES: tuple[str, ...] = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
STANDARD_ARRAY: tuple[int, ...] = (15, 14, 13, 12, 10, 8)
POINT_BUY_COSTS: Mapping[int, int] = {
    8: 0,
    9: 1,
    10: 2,
    11: 3,
    12: 4,
    13: 5,
    14: 7,
    15: 9,
}
POINT_BUY_BUDGET: int = 27


@dataclass(frozen=True)
class Race:
    """Represents a D&D playable race."""

    name: str
    description: str


@dataclass(frozen=True)
class CharacterClass:
    """Represents a D&D character class."""

    name: str
    hit_die: int
    primary_ability: str


AVAILABLE_RACES: Dict[str, Race] = {
    race.name: race
    for race in (
        Race("Human", "Adaptable and ambitious, humans thrive in any environment."),
        Race("Elf", "Graceful spellcasters with keen senses and a love for nature."),
        Race("Dwarf", "Stout warriors renowned for their resilience and craftsmanship."),
        Race("Halfling", "Nimble travelers with uncanny luck and warm hearts."),
        Race("Dragonborn", "Descendants of dragons who channel elemental power."),
        Race("Tiefling", "Planetouched folk wielding innate infernal magic."),
    )
}

AVAILABLE_CLASSES: Dict[str, CharacterClass] = {
    c.name: c
    for c in (
        CharacterClass("Fighter", hit_die=10, primary_ability="STR"),
        CharacterClass("Wizard", hit_die=6, primary_ability="INT"),
        CharacterClass("Rogue", hit_die=8, primary_ability="DEX"),
        CharacterClass("Cleric", hit_die=8, primary_ability="WIS"),
        CharacterClass("Paladin", hit_die=10, primary_ability="CHA"),
        CharacterClass("Ranger", hit_die=10, primary_ability="DEX"),
    )
}


@dataclass
class AbilityScores:
    """Ability scores for a D&D character."""

    values: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = set(ABILITY_NAMES) - set(self.values)
        if missing:
            raise ValueError(f"Missing ability scores for: {', '.join(sorted(missing))}")

    @classmethod
    def from_assignments(
        cls,
        assignments: Mapping[str, int],
        *,
        method: str = "standard_array",
    ) -> "AbilityScores":
        """Validate and create ability scores using the chosen method."""

        assignments = {key.upper(): int(value) for key, value in assignments.items()}
        _validate_assignment_keys(assignments)

        if method == "standard_array":
            _validate_standard_array(assignments.values())
        elif method == "point_buy":
            _validate_point_buy(assignments.values())
        else:
            raise ValueError("Unknown ability assignment method: %s" % method)

        return cls(dict(assignments))

    def as_lines(self) -> Iterable[str]:
        return (f"{ability}: {self.values[ability]}" for ability in ABILITY_NAMES)

    def to_dict(self) -> Dict[str, int]:
        return dict(self.values)

    @classmethod
    def from_dict(cls, data: Mapping[str, int]) -> "AbilityScores":
        return cls(dict(data))


@dataclass
class Character:
    """Persistent representation of a created character."""

    guild_id: int
    user_id: int
    race: Race
    character_class: CharacterClass
    ability_scores: AbilityScores
    name: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "race": self.race.name,
            "class": self.character_class.name,
            "ability_scores": self.ability_scores.to_dict(),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Character":
        race_name = str(data["race"])
        class_name = str(data["class"])
        race = AVAILABLE_RACES[race_name]
        character_class = AVAILABLE_CLASSES[class_name]
        ability_scores = AbilityScores.from_dict(
            {k: int(v) for k, v in dict(data["ability_scores"]).items()}
        )
        return cls(
            guild_id=int(data["guild_id"]),
            user_id=int(data["user_id"]),
            race=race,
            character_class=character_class,
            ability_scores=ability_scores,
            name=str(data.get("name", "Unnamed Adventurer")),
        )


def _validate_assignment_keys(assignments: Mapping[str, int]) -> None:
    unknown = set(assignments) - set(ABILITY_NAMES)
    if unknown:
        raise ValueError(f"Unknown ability names: {', '.join(sorted(unknown))}")


def _validate_standard_array(values: Iterable[int]) -> None:
    sorted_values = sorted(values)
    if sorted_values != sorted(STANDARD_ARRAY):
        raise ValueError(
            "Ability scores must match the standard array: "
            f"{', '.join(map(str, STANDARD_ARRAY))}"
        )


def _validate_point_buy(values: Iterable[int]) -> None:
    total_cost = 0
    for value in values:
        if value < 8 or value > 15:
            raise ValueError("Point buy values must be between 8 and 15 inclusive.")
        if value not in POINT_BUY_COSTS:
            raise ValueError(f"Point buy does not support the score {value}.")
        total_cost += POINT_BUY_COSTS[value]
    if total_cost > POINT_BUY_BUDGET:
        raise ValueError(
            "Point buy total exceeds budget of "
            f"{POINT_BUY_BUDGET} points (used {total_cost})."
        )

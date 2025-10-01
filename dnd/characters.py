"""Domain models and helpers for Dungeons & Dragons characters."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping, Sequence

import yaml

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

_SRD_PATH = Path(__file__).with_name("content") / "srd"


class SRDLoadError(RuntimeError):
    """Raised when SRD data fails validation."""


@dataclass(frozen=True)
class AbilityBonus:
    ability: str
    bonus: int


@dataclass(frozen=True)
class Feature:
    level: int
    name: str
    description: str


@dataclass(frozen=True)
class ProficiencyGrant:
    category: str
    name: str


@dataclass(frozen=True)
class LanguageProfile:
    fixed: tuple[str, ...] = field(default_factory=tuple)
    choices: int = 0


@dataclass(frozen=True)
class EquipmentItem:
    key: str
    name: str
    category: str | None = None


@dataclass(frozen=True)
class EquipmentStack:
    item: EquipmentItem
    quantity: int = 1

    def as_label(self) -> str:
        return f"{self.quantity} x {self.item.name}" if self.quantity > 1 else self.item.name


@dataclass(frozen=True)
class EquipmentChoiceOption:
    key: str
    name: str
    items: tuple[EquipmentStack, ...]

    def as_summary(self) -> str:
        parts = [stack.as_label() for stack in self.items]
        return f"{self.name}: " + ", ".join(parts)


@dataclass(frozen=True)
class EquipmentChoice:
    key: str
    choose: int
    options: tuple[EquipmentChoiceOption, ...]


@dataclass(frozen=True)
class SkillSelection:
    count: int
    options: tuple[str, ...]


@dataclass(frozen=True)
class Race:
    key: str
    name: str
    description: str
    ability_bonuses: tuple[AbilityBonus, ...]
    speed: int
    proficiencies: tuple[ProficiencyGrant, ...]
    languages: LanguageProfile
    traits: tuple[Feature, ...]


@dataclass(frozen=True)
class CharacterClass:
    key: str
    name: str
    hit_die: int
    primary_abilities: tuple[str, ...]
    saving_throws: tuple[str, ...]
    armor_proficiencies: tuple[str, ...]
    weapon_proficiencies: tuple[str, ...]
    tool_proficiencies: tuple[str, ...]
    skill_proficiency_options: SkillSelection
    equipment_choices: tuple[EquipmentChoice, ...]
    fixed_equipment: tuple[EquipmentStack, ...]
    features: tuple[Feature, ...]


@dataclass(frozen=True)
class Background:
    key: str
    name: str
    description: str
    skill_proficiencies: tuple[str, ...]
    tool_proficiencies: tuple[str, ...]
    language_choices: int
    equipment: tuple[EquipmentStack, ...]


@dataclass
class AbilityScores:
    """Ability scores for a D&D character."""

    values: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = set(ABILITY_NAMES) - set(self.values)
        if missing:
            raise ValueError(f"Missing ability scores for: {', '.join(sorted(missing))}")
        for ability, value in self.values.items():
            if ability not in ABILITY_NAMES:
                raise ValueError(f"Unknown ability '{ability}'")
            self.values[ability] = int(value)

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

    def with_bonuses(self, bonuses: Mapping[str, int]) -> "AbilityScores":
        updated = {ability: self.values[ability] for ability in ABILITY_NAMES}
        for ability, bonus in bonuses.items():
            upper = ability.upper()
            if upper not in ABILITY_NAMES:
                raise ValueError(f"Unknown ability '{ability}' in bonuses")
            updated[upper] = updated.get(upper, 0) + int(bonus)
        return AbilityScores(updated)

    def as_lines(self) -> Iterable[str]:
        return (f"{ability}: {self.values[ability]}" for ability in ABILITY_NAMES)

    def to_dict(self) -> Dict[str, int]:
        return dict(self.values)

    @classmethod
    def from_dict(cls, data: Mapping[str, int]) -> "AbilityScores":
        return cls(dict(data))

    def point_buy_total(self) -> int:
        total = 0
        for value in self.values.values():
            if value not in POINT_BUY_COSTS:
                raise ValueError("Point buy costs undefined for score %s" % value)
            total += POINT_BUY_COSTS[value]
        return total


@dataclass
class Character:
    """Persistent representation of a created character."""

    guild_id: int
    user_id: int
    race_key: str
    class_key: str
    background_key: str | None
    ability_method: str
    base_ability_scores: AbilityScores
    ability_scores: AbilityScores
    racial_bonuses: Dict[str, int]
    proficiencies: tuple[str, ...]
    equipment: tuple[str, ...]
    name: str = "Unnamed Adventurer"

    @property
    def race(self) -> Race:
        return AVAILABLE_RACES[self.race_key]

    @property
    def character_class(self) -> CharacterClass:
        return AVAILABLE_CLASSES[self.class_key]

    @property
    def background(self) -> Background | None:
        if self.background_key is None:
            return None
        return AVAILABLE_BACKGROUNDS[self.background_key]

    def to_dict(self) -> Dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "race": self.race_key,
            "class": self.class_key,
            "background": self.background_key,
            "ability_method": self.ability_method,
            "base_ability_scores": self.base_ability_scores.to_dict(),
            "ability_scores": self.ability_scores.to_dict(),
            "racial_bonuses": dict(self.racial_bonuses),
            "proficiencies": list(self.proficiencies),
            "equipment": list(self.equipment),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Character":
        race_key = str(data["race"]).lower()
        class_key = str(data["class"]).lower()
        background_value = data.get("background")
        background_key = str(background_value).lower() if background_value else None
        base_source = data.get("base_ability_scores") or data.get("ability_scores")
        if base_source is None:
            raise KeyError("base_ability_scores")
        base_scores = AbilityScores.from_dict({k: int(v) for k, v in dict(base_source).items()})
        ability_source = data.get("ability_scores") or base_source
        ability_scores = AbilityScores.from_dict(
            {k: int(v) for k, v in dict(ability_source).items()}
        )
        racial_bonuses = {k.upper(): int(v) for k, v in dict(data.get("racial_bonuses", {})).items()}
        proficiencies = tuple(str(value) for value in data.get("proficiencies", []))
        equipment = tuple(str(value) for value in data.get("equipment", []))
        return cls(
            guild_id=int(data["guild_id"]),
            user_id=int(data["user_id"]),
            race_key=race_key,
            class_key=class_key,
            background_key=background_key,
            ability_method=str(data.get("ability_method", "standard_array")),
            base_ability_scores=base_scores,
            ability_scores=ability_scores,
            racial_bonuses=racial_bonuses,
            proficiencies=proficiencies,
            equipment=equipment,
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


def _load_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        raise SRDLoadError(f"Unable to read SRD data from {path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise SRDLoadError(f"Failed to parse SRD data from {path}") from exc
    return data or []


def _require_mapping(name: str, value: object) -> MutableMapping[str, object]:
    if isinstance(value, MutableMapping):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise SRDLoadError(f"Expected mapping for {name}")


def _require_sequence(name: str, value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise SRDLoadError(f"Expected sequence for {name}")


def _load_equipment() -> Dict[str, EquipmentItem]:
    path = _SRD_PATH / "equipment.yaml"
    raw = _load_yaml(path)
    if not isinstance(raw, Mapping):
        raise SRDLoadError("equipment.yaml must contain a mapping of items")
    items_section = raw.get("items", {})
    items_mapping = _require_mapping("equipment.items", items_section)
    equipment: Dict[str, EquipmentItem] = {}
    for key, value in items_mapping.items():
        item_map = _require_mapping(f"equipment item {key}", value)
        name = str(item_map.get("name") or key)
        category_raw = item_map.get("category")
        category = str(category_raw) if category_raw is not None else None
        equipment_key = str(key).lower()
        equipment[equipment_key] = EquipmentItem(key=equipment_key, name=name, category=category)
    return equipment


def _load_races() -> Dict[str, Race]:
    path = _SRD_PATH / "races.yaml"
    raw = _load_yaml(path)
    races: Dict[str, Race] = {}
    for entry in _require_sequence("races", raw):
        mapping = _require_mapping("race entry", entry)
        key = str(mapping.get("key") or mapping.get("id") or mapping.get("name")).lower()
        if not key:
            raise SRDLoadError("Race entry missing key")
        name = str(mapping.get("name") or key.title())
        description = str(mapping.get("description", ""))
        ability_raw = mapping.get("ability_bonuses", {})
        ability_map = _require_mapping(f"{key}.ability_bonuses", ability_raw)
        ability_bonuses = tuple(
            AbilityBonus(ability=str(ability).upper(), bonus=int(value))
            for ability, value in ability_map.items()
        )
        speed = int(mapping.get("speed", 30))
        proficiencies_raw = mapping.get("proficiencies", [])
        proficiencies: list[ProficiencyGrant] = []
        for prof in _require_sequence(f"{key}.proficiencies", proficiencies_raw):
            prof_map = _require_mapping(f"{key}.proficiency", prof)
            category = str(prof_map.get("category") or "general").lower()
            name_value = prof_map.get("name")
            if not name_value:
                raise SRDLoadError(f"Proficiency entry for race {key} missing name")
            proficiencies.append(ProficiencyGrant(category=category, name=str(name_value)))
        languages_raw = mapping.get("languages", {})
        languages_map = _require_mapping(f"{key}.languages", languages_raw)
        fixed_raw = languages_map.get("fixed", [])
        fixed_languages = tuple(str(lang) for lang in _require_sequence(f"{key}.languages.fixed", fixed_raw))
        choices = int(languages_map.get("choices", 0))
        traits_raw = mapping.get("traits", [])
        traits: list[Feature] = []
        for trait in _require_sequence(f"{key}.traits", traits_raw):
            trait_map = _require_mapping(f"{key}.trait", trait)
            level = int(trait_map.get("level", 1))
            trait_name = str(trait_map.get("name", "Trait"))
            trait_description = str(trait_map.get("description", ""))
            traits.append(Feature(level=level, name=trait_name, description=trait_description))
        races[key] = Race(
            key=key,
            name=name,
            description=description,
            ability_bonuses=tuple(ability_bonuses),
            speed=speed,
            proficiencies=tuple(proficiencies),
            languages=LanguageProfile(fixed=fixed_languages, choices=choices),
            traits=tuple(traits),
        )
    return races


def _parse_equipment_list(
    owner_key: str,
    raw: object,
    equipment_lookup: Mapping[str, EquipmentItem],
) -> tuple[EquipmentStack, ...]:
    if raw is None:
        return ()
    sequence = _require_sequence(f"{owner_key}.equipment", raw)
    order: list[tuple[str, int]] = []
    for element in sequence:
        if isinstance(element, str):
            key = element
            quantity = 1
        elif isinstance(element, Mapping):
            elem_map = _require_mapping(f"{owner_key}.equipment entry", element)
            key_value = elem_map.get("item") or elem_map.get("key") or elem_map.get("id")
            if not key_value:
                raise SRDLoadError(f"Equipment entry for {owner_key} missing item key")
            key = str(key_value)
            quantity = int(elem_map.get("quantity", 1))
        else:
            raise SRDLoadError(f"Invalid equipment entry in {owner_key}")
        equipment_key = str(key).lower()
        if quantity < 1:
            raise SRDLoadError(f"Invalid quantity for {owner_key} equipment {equipment_key}")
        order.append((equipment_key, quantity))
    aggregated: "OrderedDict[str, int]" = OrderedDict()
    for key, quantity in order:
        aggregated[key] = aggregated.get(key, 0) + quantity
    stacks: list[EquipmentStack] = []
    for key, quantity in aggregated.items():
        try:
            item = equipment_lookup[key]
        except KeyError as exc:
            raise SRDLoadError(f"Unknown equipment '{key}' referenced in {owner_key}") from exc
        stacks.append(EquipmentStack(item=item, quantity=quantity))
    return tuple(stacks)


def _load_classes(equipment_lookup: Mapping[str, EquipmentItem]) -> Dict[str, CharacterClass]:
    path = _SRD_PATH / "classes.yaml"
    raw = _load_yaml(path)
    classes: Dict[str, CharacterClass] = {}
    for entry in _require_sequence("classes", raw):
        mapping = _require_mapping("class entry", entry)
        key = str(mapping.get("key") or mapping.get("id") or mapping.get("name")).lower()
        if not key:
            raise SRDLoadError("Class entry missing key")
        name = str(mapping.get("name") or key.title())
        hit_die = int(mapping.get("hit_die", 6))
        primary = tuple(str(v).upper() for v in mapping.get("primary_abilities", []))
        saving = tuple(str(v).upper() for v in mapping.get("saving_throws", []))
        armor = tuple(str(v) for v in mapping.get("armor_proficiencies", []))
        weapons = tuple(str(v) for v in mapping.get("weapon_proficiencies", []))
        tools = tuple(str(v) for v in mapping.get("tool_proficiencies", []))
        skill_raw = mapping.get("skill_proficiency_options", {})
        skill_map = _require_mapping(f"{key}.skill_proficiency_options", skill_raw)
        count = int(skill_map.get("count", 0))
        options = tuple(str(v) for v in _require_sequence(f"{key}.skill options", skill_map.get("options", [])))
        skill_selection = SkillSelection(count=count, options=options)
        equipment_data = _require_mapping(f"{key}.equipment", mapping.get("equipment", {}))
        fixed_equipment = _parse_equipment_list(f"{key}.equipment.fixed", equipment_data.get("fixed", []), equipment_lookup)
        choices_raw = equipment_data.get("choices", [])
        choices: list[EquipmentChoice] = []
        for choice_entry in _require_sequence(f"{key}.equipment.choices", choices_raw):
            choice_map = _require_mapping(f"{key}.equipment.choice", choice_entry)
            choice_key = str(choice_map.get("key") or f"{key}_choice_{len(choices)}").lower()
            choose = int(choice_map.get("choose", 1))
            options_raw = choice_map.get("options", [])
            option_values: list[EquipmentChoiceOption] = []
            for option in _require_sequence(f"{choice_key}.options", options_raw):
                option_map = _require_mapping(f"{choice_key}.option", option)
                option_key = str(option_map.get("key") or f"{choice_key}_option_{len(option_values)}").lower()
                option_name = str(option_map.get("name", option_key.title()))
                option_items = _parse_equipment_list(
                    f"{choice_key}.option.{option_key}", option_map.get("items", []), equipment_lookup
                )
                option_values.append(
                    EquipmentChoiceOption(key=option_key, name=option_name, items=option_items)
                )
            choices.append(EquipmentChoice(key=choice_key, choose=max(1, choose), options=tuple(option_values)))
        features_raw = mapping.get("features", [])
        features: list[Feature] = []
        for feature in _require_sequence(f"{key}.features", features_raw):
            feature_map = _require_mapping(f"{key}.feature", feature)
            level = int(feature_map.get("level", 1))
            feature_name = str(feature_map.get("name", "Feature"))
            feature_description = str(feature_map.get("description", ""))
            features.append(Feature(level=level, name=feature_name, description=feature_description))
        classes[key] = CharacterClass(
            key=key,
            name=name,
            hit_die=hit_die,
            primary_abilities=primary,
            saving_throws=saving,
            armor_proficiencies=armor,
            weapon_proficiencies=weapons,
            tool_proficiencies=tools,
            skill_proficiency_options=skill_selection,
            equipment_choices=tuple(choices),
            fixed_equipment=fixed_equipment,
            features=tuple(features),
        )
    return classes


def _load_backgrounds(equipment_lookup: Mapping[str, EquipmentItem]) -> Dict[str, Background]:
    path = _SRD_PATH / "backgrounds.yaml"
    raw = _load_yaml(path)
    backgrounds: Dict[str, Background] = {}
    for entry in _require_sequence("backgrounds", raw):
        mapping = _require_mapping("background entry", entry)
        key = str(mapping.get("key") or mapping.get("id") or mapping.get("name")).lower()
        if not key:
            raise SRDLoadError("Background entry missing key")
        name = str(mapping.get("name") or key.title())
        description = str(mapping.get("description", ""))
        skill_profs = tuple(str(v) for v in mapping.get("skill_proficiencies", []))
        tool_profs = tuple(str(v) for v in mapping.get("tool_proficiencies", []))
        language_choices = int(mapping.get("language_choices", 0))
        equipment = _parse_equipment_list(f"{key}.equipment", mapping.get("equipment", []), equipment_lookup)
        backgrounds[key] = Background(
            key=key,
            name=name,
            description=description,
            skill_proficiencies=skill_profs,
            tool_proficiencies=tool_profs,
            language_choices=language_choices,
            equipment=equipment,
        )
    return backgrounds


EQUIPMENT: Dict[str, EquipmentItem] = _load_equipment()
AVAILABLE_RACES: Dict[str, Race] = _load_races()
AVAILABLE_CLASSES: Dict[str, CharacterClass] = _load_classes(EQUIPMENT)
AVAILABLE_BACKGROUNDS: Dict[str, Background] = _load_backgrounds(EQUIPMENT)


__all__ = [
    "ABILITY_NAMES",
    "STANDARD_ARRAY",
    "POINT_BUY_COSTS",
    "POINT_BUY_BUDGET",
    "AbilityBonus",
    "Feature",
    "ProficiencyGrant",
    "LanguageProfile",
    "EquipmentItem",
    "EquipmentStack",
    "EquipmentChoice",
    "EquipmentChoiceOption",
    "SkillSelection",
    "Race",
    "CharacterClass",
    "Background",
    "AbilityScores",
    "Character",
    "AVAILABLE_RACES",
    "AVAILABLE_CLASSES",
    "AVAILABLE_BACKGROUNDS",
    "EQUIPMENT",
]

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from cogs.character_creation import CreationState, CreationStateError
from dnd.characters import ABILITY_NAMES, AbilityScores, Character


def test_point_buy_validation() -> None:
    assignments = {ability: 8 for ability in ABILITY_NAMES}
    scores = AbilityScores.from_assignments(assignments, method="point_buy")
    assert scores.point_buy_total() == 0
    with pytest.raises(ValueError):
        AbilityScores.from_assignments({"STR": 18, **{ability: 8 for ability in ABILITY_NAMES if ability != "STR"}}, method="point_buy")


def test_character_serialization_round_trip() -> None:
    state = CreationState()
    base_assignments = {
        "STR": 15,
        "DEX": 14,
        "CON": 13,
        "INT": 12,
        "WIS": 10,
        "CHA": 8,
    }
    state.assign_scores(base_assignments, method="standard_array")
    state.apply_race("human")
    state.set_race_languages(["Dwarvish"])
    state.set_class("fighter")
    state.set_class_skills(["Athletics", "Perception"])
    state.set_equipment_choice("fighter_weapon", ["fighter_defense"])
    state.set_background("acolyte")
    state.set_background_languages(["Giant", "Gnomish"])
    assert state.current_step() == 6
    assert state.is_ready()

    character = state.build_character(guild_id=123, user_id=456, name="Test Hero")
    payload = character.to_dict()
    restored = Character.from_dict(payload)

    assert restored.ability_scores.values["STR"] == 16  # human bonus applied
    assert restored.base_ability_scores.values["STR"] == 15
    assert restored.racial_bonuses["STR"] == 1
    assert any("Class Fighter: Skill - Athletics" in entry for entry in restored.proficiencies)
    assert any("Background Acolyte: Skill - Insight" in entry for entry in restored.proficiencies)
    assert restored.equipment


def test_creation_state_validations() -> None:
    state = CreationState()
    with pytest.raises(CreationStateError):
        state.apply_race("human")
    state.assign_scores({ability: 8 for ability in ABILITY_NAMES}, method="point_buy")
    state.apply_race("elf")
    state.set_class("wizard")
    with pytest.raises(CreationStateError):
        state.set_class_skills(["Athletics"])  # not in wizard options
    state.set_class_skills(["Arcana", "History"])
    state.set_equipment_choice("wizard_focus", ["wizard_component"])
    state.set_background("soldier")
    assert state.needs_background_languages() is False
    assert state.needs_equipment() is False
    assert state.current_step() == 6

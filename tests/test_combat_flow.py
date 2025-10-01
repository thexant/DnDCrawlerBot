import random

import pytest

import dnd.combat as combat_utils
from cogs.dungeon import CombatantState, CombatState, DungeonCog, DungeonSession
from dnd.content.models import EncounterTable, Theme
from dnd.dungeon.generator import Dungeon, EncounterResult, Room


def _make_session() -> DungeonSession:
    encounter = EncounterResult(kind="combat", summary="Test encounter", monsters=(), traps=(), loot=())
    room = Room(id=0, name="Test Room", description="", encounter=encounter, exits=())
    theme = Theme(
        key="test",
        name="Test Theme",
        description="",
        room_templates=(),
        monsters=(),
        traps=(),
        loot=(),
        encounter_table=EncounterTable({"none": 1}),
    )
    dungeon = Dungeon(name="Test Dungeon", seed=None, theme=theme, difficulty="standard", rooms=[room], corridors=())
    session = DungeonSession(dungeon=dungeon, guild_id=None, channel_id=1)
    return session


def _make_cog() -> DungeonCog:
    return DungeonCog.__new__(DungeonCog)


def test_monster_multiattack_respects_resistances_and_advantage(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    player = CombatantState(
        identifier="player:1",
        name="Hero",
        initiative_roll=15,
        initiative_total=18,
        max_hp=30,
        current_hp=30,
        is_player=True,
        user_id=1,
        metadata={"armor_class": 13, "resistances": ["slashing"]},
    )
    monster = CombatantState(
        identifier="monster:1",
        name="Ogre",
        initiative_roll=12,
        initiative_total=12,
        max_hp=60,
        current_hp=60,
        is_player=False,
        metadata={
            "armor_class": 14,
            "attack_bonus": 6,
            "actions": [
                {
                    "key": "claw",
                    "name": "Claw",
                    "type": "melee",
                    "attack_bonus": 6,
                    "damage": "2d6+3",
                    "damage_type": "slashing",
                    "advantage": ["pack tactics"],
                },
                {
                    "key": "bite",
                    "name": "Bite",
                    "type": "melee",
                    "attack_bonus": 6,
                    "damage": "1d8+3",
                    "damage_type": "piercing",
                },
            ],
            "multiattack": [
                {"ref": "claw", "count": 2},
                "bite",
            ],
        },
    )
    state = CombatState(order=[monster, player], log=[], active=True)

    damage_values = iter([9, 7, 8])
    monkeypatch.setattr(DungeonCog, "_roll_damage", lambda self, *_, **__: next(damage_values))
    rolls = iter([5, 20, 12, 14, 11])
    monkeypatch.setattr(combat_utils, "roll_d20", lambda rng=None: next(rolls))

    cog._resolve_monster_action(state, monster)

    assert player.current_hp == 11
    assert "flurry" in state.log[-1]
    assert "Critical hit!" in state.log[-1]


def test_monster_save_action_applies_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    player = CombatantState(
        identifier="player:2",
        name="Target",
        initiative_roll=10,
        initiative_total=12,
        max_hp=25,
        current_hp=25,
        is_player=True,
        user_id=2,
        metadata={"armor_class": 12, "saving_throws": {"CON": 0}},
    )
    monster = CombatantState(
        identifier="monster:2",
        name="Poisoner",
        initiative_roll=9,
        initiative_total=9,
        max_hp=30,
        current_hp=30,
        is_player=False,
        metadata={
            "armor_class": 13,
            "attack_bonus": 5,
            "actions": [
                {
                    "key": "breath",
                    "name": "Poison Breath",
                    "type": "save",
                    "damage": "4d6",
                    "damage_type": "poison",
                    "save_dc": 12,
                    "save_ability": "CON",
                    "half_on_success": True,
                    "fail_conditions": ["Poisoned"],
                }
            ],
        },
    )
    state = CombatState(order=[monster, player], log=[], active=True)

    monkeypatch.setattr(DungeonCog, "_roll_damage", lambda self, *_, **__: 14)
    monkeypatch.setattr(combat_utils, "roll_d20", lambda rng=None: 2)

    cog._resolve_monster_action(state, monster)

    assert player.current_hp == 11
    assert "Poisoned" in player.conditions
    assert "fails the DC" in state.log[-1]


def test_player_death_saves_and_target_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    downed = CombatantState(
        identifier="player:3",
        name="Downed",
        initiative_roll=8,
        initiative_total=10,
        max_hp=18,
        current_hp=0,
        is_player=True,
        user_id=3,
        metadata={"armor_class": 12},
    )
    conscious = CombatantState(
        identifier="player:4",
        name="Ally",
        initiative_roll=14,
        initiative_total=16,
        max_hp=22,
        current_hp=22,
        is_player=True,
        user_id=4,
        metadata={"armor_class": 13},
    )
    monster = CombatantState(
        identifier="monster:3",
        name="Bandit",
        initiative_roll=16,
        initiative_total=16,
        max_hp=20,
        current_hp=20,
        is_player=False,
        metadata={"armor_class": 12, "attack_bonus": 4, "damage": "1d6+2"},
    )
    state = CombatState(order=[monster, downed, conscious], log=[], active=True)

    monkeypatch.setattr(random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(DungeonCog, "_roll_damage", lambda self, *_, **__: 5)
    monkeypatch.setattr(combat_utils, "roll_d20", lambda rng=None: 19)

    cog._resolve_monster_action(state, monster)

    assert conscious.current_hp == 17
    assert downed.current_hp == 0
    assert "Ally" in state.log[-1]

    downed.death_save_successes = 0
    downed.death_save_failures = 0
    downed.stable = False
    monkeypatch.setattr(DungeonCog, "_death_save_roll", lambda self=None: 15)
    assert cog._handle_player_zero_hp_turn(state, downed) is True
    assert downed.death_save_successes == 1
    assert "15" in state.log[-1]

    monkeypatch.setattr(DungeonCog, "_death_save_roll", lambda self=None: 1)
    cog._handle_player_zero_hp_turn(state, downed)
    assert downed.death_save_failures == 2


def test_player_spell_consumes_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog()
    session = _make_session()
    monster = CombatantState(
        identifier="monster:4",
        name="Goblin",
        initiative_roll=7,
        initiative_total=7,
        max_hp=12,
        current_hp=12,
        is_player=False,
        metadata={"armor_class": 11},
    )
    resources = {"spell_slots": {"1": {"max": 2, "available": 2}}}
    player = CombatantState(
        identifier="player:5",
        name="Mage",
        initiative_roll=12,
        initiative_total=14,
        max_hp=18,
        current_hp=18,
        is_player=True,
        user_id=5,
        metadata={
            "armor_class": 12,
            "combat_options": {
                "spells": [
                    {
                        "name": "Magic Missile",
                        "type": "auto",
                        "damage": "3d4+3",
                        "damage_type": "force",
                        "consumes": {"type": "spell_slot", "level": 1, "amount": 1},
                    }
                ]
            },
            "spell_attack_bonus": 5,
            "spell_save_dc": 13,
            "resources": resources,
        },
        resources=resources,
    )
    state = CombatState(order=[player, monster], log=[], active=True)

    monkeypatch.setattr(DungeonCog, "_roll_damage", lambda self, *_, **__: 10)

    summary = cog._player_cast_spell(session, state, player, selection=None)

    assert "automatically dealing" in summary
    assert resources["spell_slots"]["1"]["available"] == 1
    assert "Magic Missile" in state.log[-1]

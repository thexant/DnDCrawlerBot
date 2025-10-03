import asyncio
import random
from typing import Optional
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import dnd.combat as combat_utils
from cogs.dungeon import CombatantState, CombatState, DungeonCog, DungeonSession
from dnd.content.models import EncounterTable, Monster, Theme
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


def test_unique_monster_labels_assign_suffixes() -> None:
    monsters = (
        Monster(
            key="skeleton_a",
            name="Skeleton",
            challenge=0.25,
            armor_class=13,
            hit_points=13,
            attack_bonus=4,
            damage="1d6+2",
        ),
        Monster(
            key="skeleton_b",
            name="Skeleton",
            challenge=0.25,
            armor_class=13,
            hit_points=13,
            attack_bonus=4,
            damage="1d6+2",
        ),
        Monster(
            key="zombie",
            name="Zombie",
            challenge=0.25,
            armor_class=8,
            hit_points=22,
            attack_bonus=3,
            damage="1d6+1",
        ),
    )
    labels = DungeonCog._unique_monster_labels(monsters)
    assert labels == ["Skeleton 1", "Skeleton 2", "Zombie"]


def test_select_player_target_prefers_existing_selection() -> None:
    cog = _make_cog()
    player = CombatantState(
        identifier="player:hero",
        name="Hero",
        initiative_roll=10,
        initiative_total=15,
        max_hp=20,
        current_hp=20,
        is_player=True,
        user_id=1,
        metadata={},
    )
    enemy_a = CombatantState(
        identifier="monster:0",
        name="Skeleton 1",
        initiative_roll=5,
        initiative_total=7,
        max_hp=13,
        current_hp=13,
        is_player=False,
        metadata={"armor_class": 13},
    )
    enemy_b = CombatantState(
        identifier="monster:1",
        name="Skeleton 2",
        initiative_roll=4,
        initiative_total=6,
        max_hp=13,
        current_hp=13,
        is_player=False,
        metadata={"armor_class": 13},
    )
    state = CombatState(order=[player, enemy_a, enemy_b], active=True)

    target = cog._select_player_target(state, player)
    assert target is enemy_a
    assert player.selected_target == enemy_a.identifier

    player.selected_target = enemy_b.identifier
    target = cog._select_player_target(state, player)
    assert target is enemy_b

    enemy_b.current_hp = 0
    target = cog._select_player_target(state, player)
    assert target is enemy_a
    assert player.selected_target == enemy_a.identifier

    enemy_a.current_hp = 0
    target = cog._select_player_target(state, player)
    assert target is None
    assert player.selected_target is None


def test_combat_embed_highlights_player_action() -> None:
    cog = _make_cog()
    session = _make_session()
    player = CombatantState(
        identifier="player:hero",
        name="Hero",
        initiative_roll=15,
        initiative_total=18,
        max_hp=20,
        current_hp=18,
        is_player=True,
        user_id=1,
        metadata={"armor_class": 13},
    )
    monster = CombatantState(
        identifier="monster:goblin",
        name="Goblin",
        initiative_roll=10,
        initiative_total=12,
        max_hp=12,
        current_hp=12,
        is_player=False,
        metadata={"armor_class": 12},
    )
    state = CombatState(order=[player, monster], log=[], active=True)
    state.turn_index = 0
    state.current_action = {
        "actor": player.name,
        "state": "weapon attack",
        "summary": "Striking Goblin with Longsword",
        "detail": "You hit Goblin for 7 damage!",
        "emoji": "⚔️",
        "team": "player",
    }
    session.combat_state = state

    embed = cog._build_combat_embed(session)

    assert embed is not None
    action_field = next(field for field in embed.fields if field.name.endswith("Player Action"))
    assert action_field.name == "⚔️ Player Action"
    assert "**Hero**" in action_field.value
    assert "Striking Goblin with Longsword" in action_field.value
    assert "*Weapon Attack*" in action_field.value
    assert "You hit Goblin for 7 damage!" in action_field.value


def test_combat_embed_shows_enemy_thinking() -> None:
    cog = _make_cog()
    session = _make_session()
    player = CombatantState(
        identifier="player:hero",
        name="Hero",
        initiative_roll=15,
        initiative_total=18,
        max_hp=20,
        current_hp=18,
        is_player=True,
        user_id=1,
        metadata={"armor_class": 13},
    )
    monster = CombatantState(
        identifier="monster:goblin",
        name="Goblin",
        initiative_roll=19,
        initiative_total=22,
        max_hp=12,
        current_hp=12,
        is_player=False,
        metadata={"armor_class": 12},
    )
    state = CombatState(order=[monster, player], log=[], active=True)
    state.turn_index = 0
    state.current_action = {
        "actor": monster.name,
        "state": "thinking",
        "summary": "Plotting their next move...",
        "detail": "The goblin eyes the party, biding its time.",
        "emoji": "🤔",
        "team": "enemy",
    }
    session.combat_state = state

    embed = cog._build_combat_embed(session)

    assert embed is not None
    action_field = next(field for field in embed.fields if field.name.endswith("Enemy Turn"))
    assert action_field.name == "🤔 Enemy Turn"
    assert "**Goblin**" in action_field.value
    assert "Plotting their next move" in action_field.value
    assert "*Thinking*" in action_field.value
    assert "The goblin eyes the party" in action_field.value


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


def test_player_death_announcement_clears_character_and_offers_return() -> None:
    async def _run() -> None:
        cog = _make_cog()
        session = _make_session()
        session.guild_id = 123
        player = CombatantState(
            identifier="player:fallen",
            name="Fallen Hero",
            initiative_roll=10,
            initiative_total=12,
            max_hp=20,
            current_hp=0,
            is_player=True,
            user_id=42,
            metadata={"armor_class": 12},
            death_save_failures=3,
        )
        combat = CombatState(order=[player], log=[], active=True)
        session.combat_state = combat

        clear_mock = AsyncMock()
        cog.characters = SimpleNamespace(clear=clear_mock)

        class DummyChannel:
            def __init__(self) -> None:
                self.sent_messages: list[dict[str, object]] = []

            async def send(
                self, *, embed: discord.Embed, view: discord.ui.View
            ) -> SimpleNamespace:
                self.sent_messages.append({"embed": embed, "view": view})
                return SimpleNamespace(id=987654321)

        dummy_channel = DummyChannel()

        class DummyBot:
            def __init__(self, channel: DummyChannel) -> None:
                self._channel = channel
                self.registered: list[tuple[discord.ui.View, int]] = []

            def get_channel(self, channel_id: int) -> DummyChannel:
                return self._channel

            def add_view(
                self, view: discord.ui.View, message_id: Optional[int] = None
            ) -> None:
                self.registered.append((view, message_id))

        cog.bot = DummyBot(dummy_channel)

        await cog._announce_player_death(session, player)

        assert clear_mock.await_count == 1
        assert clear_mock.await_args_list[0].args == (session.guild_id, player.user_id)
        assert dummy_channel.sent_messages, "Death announcement was not sent"

        sent_payload = dummy_channel.sent_messages[0]
        embed = sent_payload["embed"]
        assert isinstance(embed, discord.Embed)
        assert player.name in (embed.title or "")
        assert "has fallen" in (embed.description or "")

        view = sent_payload["view"]
        assert isinstance(view, discord.ui.View)
        buttons = [child for child in view.children if isinstance(child, discord.ui.Button)]
        assert any(button.label == "Return to Tavern" for button in buttons)

        return_button = next(button for button in buttons if button.label == "Return to Tavern")
        fake_tavern = SimpleNamespace(mention="#tavern")
        cog._find_tavern_channel = AsyncMock(return_value=fake_tavern)

        class DummyResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, bool]] = []

            async def send_message(self, content: str, *, ephemeral: bool) -> None:
                self.messages.append((content, ephemeral))

        interaction = SimpleNamespace(response=DummyResponse())
        await return_button.callback(interaction)

        assert interaction.response.messages
        message_content, ephemeral = interaction.response.messages[0]
        assert ephemeral is True
        assert fake_tavern.mention in message_content

    asyncio.run(_run())

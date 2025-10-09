"""Dungeon crawling commands and persistent interaction views."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import re
import secrets
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import discord
from discord import app_commands
from discord.ext import commands

from dnd.combat import (
    Attack,
    DamagePacket,
    SavingThrowResult,
    ability_modifier,
    apply_damage,
    attack_roll,
    resolve_attack,
    resolve_multiattack,
    saving_throw,
)
from dnd.characters import EQUIPMENT, Character
from dnd.repository import CharacterRepository
from dnd.content import ContentLibrary, ContentLoadError, Item, Trap
from dnd.dungeon import (
    DIFFICULTY_PROFILES,
    Dungeon,
    DungeonGenerator,
    MonsterDefinition,
    Room,
    RoomExit,
    Theme,
    ThemeRegistry,
)
from dnd.dungeon.map_render import render_dungeon_map
from dnd.dungeon.state import DungeonMetadataStore, StoredDungeon
from dnd.dungeon.rewards import (
    RewardShare,
    allocate_loot,
    eligible_order,
    format_item_label,
    split_gold,
    trap_reward_value,
)
from dnd.sessions import SessionKey, SessionManager


log = logging.getLogger(__name__)


DEFAULT_PLAYER_HP = 20
DEFAULT_PLAYER_ARMOR_CLASS = 13
DEFAULT_PLAYER_ATTACK_BONUS = 5
DEFAULT_PLAYER_DAMAGE = "1d8+3"
PROFICIENCY_BONUS = 2
MONSTER_THINKING_DELAY_RANGE = (5, 10)
MONSTER_ACTION_PAUSE_RANGE = (3, 5)
SPELLCASTING_ABILITIES: Dict[str, str] = {
    "wizard": "INT",
}

TrapStatus = Literal["hidden", "discovered", "disarmed", "sprung"]


def _format_difficulty_label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_"))


DEFAULT_PERCEPTION_DC = 15
PERCEPTION_DC_INCREASE_CHANCE = 0.25
HIDDEN_EXIT_BASE_CHANCE = 0.25
ABILITY_NAME_OVERRIDES = {
    "STR": "Strength",
    "DEX": "Dexterity",
    "CON": "Constitution",
    "INT": "Intelligence",
    "WIS": "Wisdom",
    "CHA": "Charisma",
}


DIFFICULTY_CHOICES = [
    app_commands.Choice(name=_format_difficulty_label(key), value=key)
    for key in DIFFICULTY_PROFILES.keys()
][:25]
DEFAULT_DIFFICULTY = "standard"


@dataclass(frozen=True)
class ArmorDefinition:
    base_ac: int
    dex_cap: Optional[int] = None


@dataclass(frozen=True)
class WeaponDefinition:
    damage_die: str
    categories: Sequence[str]
    finesse: bool = False
    ranged: bool = False
    name: Optional[str] = None
    quantity: int = 1


ARMOR_DEFINITIONS: Dict[str, ArmorDefinition] = {
    "chain_mail": ArmorDefinition(base_ac=16, dex_cap=0),
    "leather_armor": ArmorDefinition(base_ac=11, dex_cap=None),
    "scale_mail": ArmorDefinition(base_ac=14, dex_cap=2),
}

SHIELD_BONUSES: Dict[str, int] = {
    "shield": 2,
}

WEAPON_DEFINITIONS: Dict[str, WeaponDefinition] = {
    "dagger": WeaponDefinition(
        damage_die="1d4",
        categories=("simple weapons", "daggers"),
        finesse=True,
        name="Dagger",
    ),
    "light_crossbow": WeaponDefinition(
        damage_die="1d8",
        categories=("simple weapons", "light crossbows"),
        ranged=True,
        name="Light Crossbow",
    ),
    "longbow": WeaponDefinition(
        damage_die="1d8",
        categories=("martial weapons", "longbows"),
        ranged=True,
        name="Longbow",
    ),
    "longsword": WeaponDefinition(
        damage_die="1d8",
        categories=("martial weapons", "longswords"),
        name="Longsword",
    ),
    "mace": WeaponDefinition(
        damage_die="1d6",
        categories=("simple weapons", "maces"),
        name="Mace",
    ),
    "quarterstaff": WeaponDefinition(
        damage_die="1d6",
        categories=("simple weapons", "quarterstaffs"),
        name="Quarterstaff",
    ),
    "rapier": WeaponDefinition(
        damage_die="1d8",
        categories=("martial weapons", "rapiers"),
        finesse=True,
        name="Rapier",
    ),
    "shortbow": WeaponDefinition(
        damage_die="1d6",
        categories=("simple weapons", "shortbows"),
        ranged=True,
        name="Shortbow",
    ),
    "shortsword_pair": WeaponDefinition(
        damage_die="1d6",
        categories=("martial weapons", "shortswords"),
        finesse=True,
        name="Shortsword",
        quantity=2,
    ),
}
MAX_COMBAT_LOG_ENTRIES = 12
_DAMAGE_ROLL_PATTERN = re.compile(r"(?i)(?P<count>\d+)d(?P<sides>\d+)(?P<modifier>[+-]\d+)?")

# Basic spell data used when character sheets do not provide richer metadata.
DEFAULT_SPELL_OPTIONS: Dict[str, List[Dict[str, object]]] = {
    "wizard": [
        {
            "name": "Fire Bolt",
            "level": 0,
            "type": "attack",
            "damage": "1d10",
            "damage_type": "fire",
            "critical_extra_dice": [],
            "description": "A mote of flame streaks toward a creature you can see within range.",
        },
        {
            "name": "Magic Missile",
            "level": 1,
            "type": "auto",
            "damage": "3d4+3",
            "damage_type": "force",
            "consumes": {"type": "spell_slot", "level": 1, "amount": 1},
            "description": "Three glowing darts strike creatures of your choice that you can see within range.",
        },
    ],
}

DEFAULT_SPELL_SLOTS: Dict[str, Dict[int, int]] = {
    "wizard": {1: 2},
}



def _default_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


@dataclass
class DungeonSession:
    """State container for an active dungeon crawl."""

    dungeon: Dungeon
    guild_id: Optional[int]
    channel_id: int
    current_room: int = 0
    seed: Optional[int] = None
    party_ids: set[int] = field(default_factory=set)
    party_health: Dict[int, Dict[str, int]] = field(default_factory=dict)
    room_damage_log: Dict[int, Dict[str, int]] = field(default_factory=dict)
    fallen_players: set[object] = field(default_factory=set)
    message_id: Optional[int] = None
    combat_state: Optional["CombatState"] = None
    breadcrumbs: list[int] = field(default_factory=list)
    exit_history: list[str] = field(default_factory=list)
    last_exit_taken: Optional[str] = None
    last_travel_description: Optional[str] = None
    last_travel_note: Optional[str] = None
    loot_cursor: int = 0
    stealthed: bool = False
    trap_states: Dict[int, Dict[str, TrapStatus]] = field(default_factory=dict)
    trap_catalog: Dict[int, Dict[str, Trap]] = field(default_factory=dict)
    discovered_traps: Dict[int, Set[str]] = field(default_factory=dict)
    discovered_loot: Dict[int, Set[str]] = field(default_factory=dict)
    discovered_exits: Dict[int, Set[str]] = field(default_factory=dict)
    exit_visibility: Dict[int, Dict[str, bool]] = field(default_factory=dict)
    perception_attempts: Dict[int, Dict[int, int]] = field(default_factory=dict)
    perception_difficulties: Dict[int, Dict[tuple[str, str], int]] = field(
        default_factory=dict
    )
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    monsters_defeated: int = 0
    traps_disarmed: int = 0
    traps_triggered: int = 0
    treasure_items_claimed: int = 0
    treasure_gold_claimed: int = 0
    party_fall_announced: bool = False

    @property
    def room(self) -> Room:
        return self.dungeon.rooms[self.current_room]

    @property
    def at_final_room(self) -> bool:
        return self.current_room >= len(self.dungeon.rooms) - 1

    def travel_description(self) -> Optional[str]:
        return self.last_travel_description

    def __post_init__(self) -> None:
        if not self.breadcrumbs:
            self.breadcrumbs.append(self.current_room)
        self.room_damage_log.setdefault(self.current_room, {"monsters": 0, "traps": 0})


@dataclass
class SessionEmbedPayload:
    """Container for embeds and files representing a dungeon session update."""

    embeds: List[discord.Embed]
    files: List[discord.File] = field(default_factory=list)


@dataclass
class CombatantState:
    """Mutable state tracked for each combatant during combat."""

    identifier: str
    name: str
    initiative_roll: int
    initiative_total: int
    max_hp: int
    current_hp: int
    is_player: bool
    user_id: Optional[int] = None
    metadata: Dict[str, object] = field(default_factory=dict)
    conditions: Set[str] = field(default_factory=set)
    concentration: Optional[str] = None
    death_save_successes: int = 0
    death_save_failures: int = 0
    resources: Dict[str, object] = field(default_factory=dict)
    stable: bool = False
    selected_target: Optional[str] = None

    @property
    def defeated(self) -> bool:
        if not self.is_player:
            return self.current_hp <= 0
        if self.current_hp > 0:
            return False
        return self.is_dead or self.stable

    @property
    def is_dead(self) -> bool:
        if not self.is_player:
            return self.current_hp <= 0
        return self.current_hp <= 0 and self.death_save_failures >= 3


@dataclass
class CombatState:
    """Encapsulates the turn order and ongoing combat flow."""

    order: List[CombatantState] = field(default_factory=list)
    turn_index: int = 0
    waiting_for: Optional[int] = None
    active: bool = True
    round_number: int = 1
    log: List[str] = field(default_factory=list)
    current_action: Optional[Dict[str, str]] = None

    def current_combatant(self) -> Optional[CombatantState]:
        if not self.order:
            return None
        return self.order[self.turn_index]

    def advance_turn(self) -> Optional[CombatantState]:
        if not self.order:
            return None
        for _ in range(len(self.order)):
            self.turn_index = (self.turn_index + 1) % len(self.order)
            if self.turn_index == 0:
                self.round_number += 1
            combatant = self.order[self.turn_index]
            if not combatant.defeated:
                return combatant
        return None

    def living_combatants(self, *, players: Optional[bool] = None) -> List[CombatantState]:
        results: List[CombatantState] = []
        for combatant in self.order:
            if combatant.defeated:
                continue
            if players is None or combatant.is_player is players:
                results.append(combatant)
        return results


class DungeonNavigationView(discord.ui.View):
    """Button controls for navigating dungeon sessions."""

    def __init__(
        self,
        cog: "DungeonCog",
        session: DungeonSession,
        *,
        disable_search: bool = False,
        disable_disarm: bool = False,
        disable_engage: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self._session = session
        self.cog._ensure_room_trap_state(session, session.room)
        self._add_status_indicator(session)
        self._add_exit_controls(session)
        trap_detected = self.cog._room_has_discovered_traps(session, session.room)
        trap_label = (
            "Disarm Trap"
            if trap_detected
            else "Survey for Traps"
        )
        trap_style = (
            discord.ButtonStyle.danger
            if trap_label == "Disarm Trap"
            else discord.ButtonStyle.secondary
        )
        self._add_action_button(
            label="Search",
            style=discord.ButtonStyle.secondary,
            custom_id="dungeon:search",
            disabled=disable_search,
            handler=self._handle_search,
        )
        self._add_action_button(
            label="Perception",
            style=discord.ButtonStyle.secondary,
            custom_id="dungeon:perception",
            disabled=False,
            handler=self._handle_perception,
        )
        self._add_action_button(
            label=trap_label,
            style=trap_style,
            custom_id="dungeon:disarm",
            disabled=disable_disarm or not trap_detected,
            handler=self._handle_disarm,
        )
        self._add_action_button(
            label="Ambush" if session.stealthed else "Engage",
            style=discord.ButtonStyle.success,
            custom_id="dungeon:engage",
            disabled=disable_engage,
            handler=self._handle_engage,
        )

    def _add_status_indicator(self, session: DungeonSession) -> None:
        status_label = "Hidden" if session.stealthed else "Spotted"
        style = discord.ButtonStyle.success if session.stealthed else discord.ButtonStyle.secondary
        button = discord.ui.Button(
            label=f"Status: {status_label}",
            style=style,
            custom_id=f"dungeon:status:{session.channel_id}",
            disabled=True,
        )
        self.add_item(button)

    def _add_exit_controls(self, session: DungeonSession) -> None:
        discovered = session.discovered_exits.get(session.room.id, set())
        for exit_option in session.room.exits:
            if exit_option.key not in discovered:
                continue
            custom_id = f"dungeon:exit:{session.channel_id}:{exit_option.key}"
            button = discord.ui.Button(
                label=exit_option.label,
                style=discord.ButtonStyle.primary,
                custom_id=custom_id,
            )
            button.callback = self._make_exit_callback(exit_option.key)
            self.add_item(button)

    def _add_action_button(
        self,
        *,
        label: str,
        style: discord.ButtonStyle,
        custom_id: str,
        disabled: bool,
        handler: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=custom_id,
            disabled=disabled,
        )
        button.callback = handler
        self.add_item(button)

    def _make_exit_callback(
        self, exit_key: str
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_exit(interaction, exit_key)

        return _callback

    async def _handle_search(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_search(interaction)

    async def _handle_disarm(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_disarm(interaction)

    async def _handle_engage(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_engage(interaction)

    async def _handle_perception(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_perception(interaction)


class CombatActionView(discord.ui.View):
    """Interaction controls shown while combat is active."""

    def __init__(self, cog: "DungeonCog", session: DungeonSession) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        combat = session.combat_state
        current = combat.current_combatant() if combat else None
        self._combat_active = bool(combat and combat.active)
        self._add_status_indicator(session)
        self._add_target_select(combat, current)
        self._configure_controls(combat, current)
        if not self._combat_active:
            self._disable_children()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D401
        key = self.cog._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.cog.sessions.get(key)
        if session is None:
            await interaction.response.send_message("No active combat encounter here.", ephemeral=True)
            return False
        combat = session.combat_state
        if combat is None or not combat.active:
            await interaction.response.send_message("Combat has already ended.", ephemeral=True)
            return False
        current = combat.current_combatant()
        if current is None:
            await interaction.response.send_message("No combatants are ready to act.", ephemeral=True)
            return False
        if not current.is_player:
            await interaction.response.send_message("Please wait for the monsters to finish their turn.", ephemeral=True)
            return False
        if current.user_id != interaction.user.id:
            await interaction.response.send_message("It isn't your turn yet!", ephemeral=True)
            return False
        return True

    def _add_status_indicator(self, session: DungeonSession) -> None:
        status_label = "Hidden" if session.stealthed else "Spotted"
        style = discord.ButtonStyle.success if session.stealthed else discord.ButtonStyle.secondary
        button = discord.ui.Button(
            label=f"Status: {status_label}",
            style=style,
            custom_id=f"dungeon:combat:status:{session.channel_id}",
            disabled=True,
        )
        self.add_item(button)

    def _configure_controls(
        self, combat: Optional[CombatState], current: Optional[CombatantState]
    ) -> None:
        if not combat or current is None or not current.is_player or current.defeated:
            self._add_weapon_select(None)
            self._add_spell_select(None)
            self._add_feature_select(None)
            self._add_common_buttons(disabled=not self._combat_active)
            return
        if current.current_hp <= 0 and not current.is_dead and not current.stable:
            self._add_death_save_button(disabled=not self._combat_active)
            return
        self._add_weapon_select(current)
        self._add_spell_select(current)
        self._add_feature_select(current)
        self._add_common_buttons(disabled=not self._combat_active)

    def _disable_children(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

    def _target_options(
        self,
        combat: Optional[CombatState],
        current: Optional[CombatantState],
    ) -> Tuple[List[discord.SelectOption], bool]:
        if combat is None:
            return ([
                discord.SelectOption(
                    label="No combat in progress",
                    value="target:none",
                    description="There is no active encounter.",
                )
            ], True)
        enemies = [
            combatant
            for combatant in combat.order
            if not combatant.is_player and not combatant.defeated
        ]
        if not enemies:
            return ([
                discord.SelectOption(
                    label="No targets available",
                    value="target:none",
                    description="All enemies have been defeated.",
                )
            ], True)
        disabled = (
            current is None
            or not current.is_player
            or current.defeated
            or (current.current_hp <= 0 if current else False)
        ) or not self._combat_active
        selected_identifier = (
            current.selected_target if current and current.is_player else None
        )
        options: List[discord.SelectOption] = []
        for enemy in enemies:
            ac_value = enemy.metadata.get("armor_class")
            ac_text = f"AC {ac_value}" if ac_value is not None else "AC ?"
            hp_text = f"HP {max(enemy.current_hp, 0)}/{enemy.max_hp}"
            description = f"{ac_text} • {hp_text}"[:100]
            options.append(
                discord.SelectOption(
                    label=enemy.name[:100],
                    value=f"target:{enemy.identifier}",
                    description=description,
                    default=enemy.identifier == selected_identifier,
                )
            )
        return options, disabled

    def _add_target_select(
        self, combat: Optional[CombatState], current: Optional[CombatantState]
    ) -> None:
        options, disabled = self._target_options(combat, current)
        select = self._ActionSelect(
            self.cog,
            action="target",
            placeholder="Choose a target",
            options=options,
            disabled=disabled,
            custom_id="dungeon:combat:target",
        )
        self.add_item(select)

    class _ActionSelect(discord.ui.Select):
        def __init__(
            self,
            cog: "DungeonCog",
            *,
            action: str,
            placeholder: str,
            options: List[discord.SelectOption],
            disabled: bool,
            custom_id: str,
        ) -> None:
            super().__init__(
                custom_id=custom_id,
                placeholder=placeholder,
                min_values=1,
                max_values=1,
                options=options,
                disabled=disabled,
            )
            self._cog = cog
            self._action = action

        async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
            value = self.values[0] if self.values else None
            await self._cog.handle_combat_action(interaction, self._action, value)

    def _weapon_options(self, combatant: Optional[CombatantState]) -> List[discord.SelectOption]:
        if combatant is None:
            return [
                discord.SelectOption(
                    label="No weapons available",
                    value="weapon:0",
                    description="No weapon attacks are configured.",
                )
            ]
        combat_options = combatant.metadata.get("combat_options", {})
        weapons = combat_options.get("weapons") if isinstance(combat_options, Mapping) else None
        if not isinstance(weapons, list) or not weapons:
            return [
                discord.SelectOption(
                    label="No weapons available",
                    value="weapon:0",
                    description="No weapon attacks are configured.",
                )
            ]
        options: List[discord.SelectOption] = []
        for index, weapon in enumerate(weapons):
            name = str(weapon.get("name", f"Weapon {index + 1}"))
            attack_bonus = weapon.get("attack_bonus")
            damage_expr = weapon.get("damage")
            desc_parts: List[str] = []
            if attack_bonus is not None:
                desc_parts.append(f"+{attack_bonus} to hit")
            if damage_expr:
                desc_parts.append(str(damage_expr))
            description = ", ".join(desc_parts)[:100] or "Standard attack option"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=f"weapon:{index}",
                    description=description,
                )
            )
        return options

    def _spell_options(
        self, combatant: Optional[CombatantState]
    ) -> Tuple[List[discord.SelectOption], bool]:
        if combatant is None:
            return ([
                discord.SelectOption(
                    label="No spells prepared",
                    value="spell:0",
                    description="You have no prepared spells.",
                )
            ], True)
        combat_options = combatant.metadata.get("combat_options", {})
        spells = combat_options.get("spells") if isinstance(combat_options, Mapping) else None
        if not isinstance(spells, list) or not spells:
            return ([
                discord.SelectOption(
                    label="No spells prepared",
                    value="spell:0",
                    description="You have no prepared spells.",
                )
            ], True)
        options: List[discord.SelectOption] = []
        for index, spell in enumerate(spells):
            name = str(spell.get("name", f"Spell {index + 1}"))
            level = spell.get("level")
            level_text = f" (Lvl {level})" if level is not None else ""
            effect_type = str(spell.get("type", "Attack")).title()
            damage_expr = spell.get("damage")
            damage_type = spell.get("damage_type")
            desc_parts: List[str] = [effect_type]
            if damage_expr:
                damage_text = str(damage_expr)
                if damage_type:
                    damage_text += f" {str(damage_type).title()}"
                desc_parts.append(damage_text)
            requirement = spell.get("consumes")
            if isinstance(requirement, Mapping):
                requirement_type = str(requirement.get("type", "")).lower()
                if requirement_type == "spell_slot":
                    level_value = requirement.get("level", 1)
                    desc_parts.append(f"Uses slot lvl {level_value}")
            description = ", ".join(desc_parts)[:100]
            options.append(
                discord.SelectOption(
                    label=f"{name}{level_text}"[:100],
                    value=f"spell:{index}",
                    description=description or "Spell action",
                )
            )
        return options, False

    def _feature_options(
        self, combatant: Optional[CombatantState]
    ) -> Tuple[List[discord.SelectOption], bool]:
        if combatant is None:
            return ([
                discord.SelectOption(
                    label="No features available",
                    value="feature:0",
                    description="No combat features are ready.",
                )
            ], True)
        combat_options = combatant.metadata.get("combat_options", {})
        features = combat_options.get("features") if isinstance(combat_options, Mapping) else None
        if not isinstance(features, list) or not features:
            return ([
                discord.SelectOption(
                    label="No features available",
                    value="feature:0",
                    description="No combat features are ready.",
                )
            ], True)
        options: List[discord.SelectOption] = []
        for index, feature in enumerate(features):
            name = str(feature.get("name", f"Feature {index + 1}"))
            effects = feature.get("effects") if isinstance(feature, Mapping) else None
            effect_desc = "Feature action"
            if isinstance(effects, Mapping):
                effect_type = str(effects.get("type", "")).title()
                detail = effects.get("dice") or effects.get("condition")
                if detail:
                    effect_desc = f"{effect_type}: {detail}"
                else:
                    effect_desc = effect_type or effect_desc
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=f"feature:{index}",
                    description=str(effect_desc)[:100],
                )
            )
        return options, False

    def _add_weapon_select(self, combatant: Optional[CombatantState]) -> None:
        options = self._weapon_options(combatant)
        disabled = combatant is None or not combatant.metadata.get("combat_options", {}).get("weapons")
        select = self._ActionSelect(
            self.cog,
            action="weapon",
            placeholder="Choose a weapon attack",
            options=options,
            disabled=disabled,
            custom_id="dungeon:combat:weapon",
        )
        self.add_item(select)

    def _add_spell_select(self, combatant: Optional[CombatantState]) -> None:
        options, disabled = self._spell_options(combatant)
        select = self._ActionSelect(
            self.cog,
            action="spell",
            placeholder="Cast a spell",
            options=options,
            disabled=disabled,
            custom_id="dungeon:combat:spell",
        )
        self.add_item(select)

    def _add_feature_select(self, combatant: Optional[CombatantState]) -> None:
        options, disabled = self._feature_options(combatant)
        select = self._ActionSelect(
            self.cog,
            action="feature",
            placeholder="Use a feature",
            options=options,
            disabled=disabled,
            custom_id="dungeon:combat:feature",
        )
        self.add_item(select)

    def _add_death_save_button(self, *, disabled: bool) -> None:
        button = discord.ui.Button(
            label="Roll Death Save",
            style=discord.ButtonStyle.danger,
            custom_id="dungeon:combat:death_save",
            disabled=disabled,
        )
        button.callback = self._make_action_callback("death_save")
        self.add_item(button)

    def _add_common_buttons(self, *, disabled: bool) -> None:
        defend_button = discord.ui.Button(
            label="Defend",
            style=discord.ButtonStyle.secondary,
            custom_id="dungeon:combat:defend",
            disabled=disabled,
        )
        defend_button.callback = self._make_action_callback("defend")
        end_button = discord.ui.Button(
            label="End Turn",
            style=discord.ButtonStyle.primary,
            custom_id="dungeon:combat:end",
            disabled=disabled,
        )
        end_button.callback = self._make_action_callback("end")
        self.add_item(defend_button)
        self.add_item(end_button)

    def _make_action_callback(self, action: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_combat_action(interaction, action)

        return _callback


class DungeonDeleteConfirmation(discord.ui.View):
    """Confirmation dialog for deleting stored dungeons."""

    def __init__(
        self,
        cog: "DungeonCog",
        *,
        requester_id: int,
        guild_id: int,
        dungeon: StoredDungeon,
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.dungeon = dungeon

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D401
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the administrator who requested this deletion can respond.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        deleted = await self.cog.metadata_store.delete_dungeon(
            self.guild_id, self.dungeon.name
        )
        if deleted:
            message = f"Deleted stored dungeon **{self.dungeon.name}**."
        else:
            message = (
                f"Stored dungeon **{self.dungeon.name}** was already removed."
            )
        await interaction.response.edit_message(content=message, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await interaction.response.edit_message(
            content="Deletion cancelled.", view=None
        )
        self.stop()


class ReturnToTavernView(discord.ui.View):
    def __init__(self, cog: "DungeonCog", session: DungeonSession) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session

    @discord.ui.button(
        label="Return to Tavern",
        style=discord.ButtonStyle.primary,
        custom_id="dungeon:return_to_tavern",
    )
    async def return_to_tavern(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # noqa: D401
        guild_id = interaction.guild_id or self.session.guild_id
        user_id = getattr(interaction.user, "id", None)
        hero_removed = False

        def mutator(session: DungeonSession) -> None:
            nonlocal hero_removed
            if user_id is None:
                return
            if user_id in session.party_ids:
                session.party_ids.discard(user_id)
                hero_removed = True

        session_key = self.cog._session_key(guild_id, self.session.channel_id)
        updated_session = await self.cog.sessions.update(session_key, mutator)
        session_exists = updated_session is not None
        if updated_session is None:
            updated_session = self.session
            if user_id is not None and user_id in updated_session.party_ids:
                updated_session.party_ids.discard(user_id)
                hero_removed = True

        await self.cog._refresh_session_view(updated_session)

        if guild_id is not None and hero_removed and session_exists:
            try:
                await self.cog._handle_party_membership_change(guild_id, updated_session)
            except Exception:  # pragma: no cover - defensive logging
                log.debug(
                    "Failed to update party membership for guild %s after death.",
                    guild_id,
                )

        tavern_channel = await self.cog._find_tavern_channel(guild_id)
        if tavern_channel is not None:
            await interaction.response.send_message(
                f"Head back to {tavern_channel.mention} to regroup and recover.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "No tavern channel is currently configured. Coordinate with your party to recover.",
            ephemeral=True,
        )


class DungeonCog(commands.Cog):
    """Slash commands to generate and explore procedural dungeons."""

    dungeon_group = app_commands.Group(name="dungeon", description="Procedural dungeon exploration")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_path = _default_data_path()
        self.theme_registry = ThemeRegistry()
        self.content_library: ContentLibrary | None = None
        self._content_error: ContentLoadError | None = None
        self.sessions: SessionManager[DungeonSession] = SessionManager()
        self._guild_theme_cache: Dict[int, Optional[str]] = {}
        metadata_path = self.data_path / "sessions" / "metadata.json"
        self.metadata_store = DungeonMetadataStore(metadata_path)
        self.characters = CharacterRepository(Path("data") / "characters.json")
        self._load_content(silent=True)

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        try:
            self.bot.tree.remove_command(
                self.dungeon_group.name,
                type=discord.AppCommandType.chat_input,
            )
        except (app_commands.CommandTreeException, KeyError):
            pass

    # ------------------------------------------------------------------
    def _load_content(self, *, silent: bool = False) -> None:
        try:
            library = ContentLibrary.load_from_path(self.data_path)
        except ContentLoadError as exc:
            self._content_error = exc
            if not silent:
                raise
        else:
            self.content_library = library
            self.theme_registry = library.themes
            self._content_error = None

    @staticmethod
    def _fallen_identifier(combatant: CombatantState) -> object:
        return combatant.user_id if combatant.user_id is not None else combatant.identifier

    def _identify_newly_fallen(
        self, session: DungeonSession, combat: CombatState
    ) -> list[CombatantState]:
        newly_fallen: list[CombatantState] = []
        for combatant in combat.order:
            if not combatant.is_player or not combatant.is_dead:
                continue
            key = self._fallen_identifier(combatant)
            if key in session.fallen_players:
                continue
            session.fallen_players.add(key)
            newly_fallen.append(combatant)
        return newly_fallen

    def _build_player_death_embed(
        self, session: DungeonSession, combatant: CombatantState
    ) -> discord.Embed:
        room = session.room
        embed = discord.Embed(
            colour=discord.Colour.dark_red(),
            title=f"A Hero Falls — {combatant.name}",
            description=(
                f"{combatant.name} has fallen in the depths of {session.dungeon.name}."
            ),
        )
        if combatant.user_id is not None:
            embed.add_field(
                name="Adventurer",
                value=f"<@{combatant.user_id}>",
                inline=False,
            )
        embed.add_field(
            name="Final Stand",
            value=f"Room {room.id + 1}: {room.name}",
            inline=False,
        )
        embed.add_field(
            name="Last Known Vitality",
            value=f"{max(combatant.current_hp, 0)}/{combatant.max_hp} HP",
            inline=False,
        )
        embed.set_footer(text="Raise a toast at the tavern to honour the fallen.")
        return embed

    async def _announce_player_death(
        self, session: DungeonSession, combatant: CombatantState
    ) -> None:
        guild_id = session.guild_id
        user_id = combatant.user_id
        if guild_id is not None and user_id is not None:
            try:
                await self.characters.clear(guild_id, user_id)
                await self._update_tavern_access(guild_id)
            except Exception as exc:  # pragma: no cover - defensive logging
                log.warning(
                    "Failed to clear character %s in guild %s after death: %s",
                    user_id,
                    guild_id,
                    exc,
                )
        channel = self.bot.get_channel(session.channel_id)
        if channel is None:
            return
        send = getattr(channel, "send", None)
        if not callable(send):
            return
        embed = self._build_player_death_embed(session, combatant)
        view = ReturnToTavernView(self, session)
        try:
            message = await send(embed=embed, view=view)
        except Exception:  # pragma: no cover - defensive logging
            log.warning(
                "Failed to deliver death announcement for %s in channel %s",
                combatant.identifier,
                session.channel_id,
            )
            return
        message_id = getattr(message, "id", None)
        if message_id is not None:
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception:  # pragma: no cover - defensive logging
                log.debug("Failed to register death view for message %s", message_id)

        await self._send_player_to_manage_channel(session, combatant)
        await self._handle_party_failure_if_needed(session)

    def _session_key(self, guild_id: Optional[int], channel_id: Optional[int]) -> SessionKey:
        return SessionManager.make_key(guild_id, channel_id)

    async def _send_player_to_manage_channel(
        self, session: DungeonSession, combatant: CombatantState
    ) -> None:
        guild_id = session.guild_id
        user_id = combatant.user_id
        if guild_id is None or user_id is None:
            return

        manage_channel = await self._find_manage_channel(guild_id)
        if manage_channel is None:
            return

        room = session.room
        embed = discord.Embed(
            colour=discord.Colour.blurple(),
            title="Forge a New Legend",
            description=(
                f"{combatant.name} has fallen within {session.dungeon.name}. "
                "Create a new character with `/character create` to rejoin the adventure."
            ),
        )
        embed.add_field(
            name="Final Stand",
            value=f"Room {room.id + 1}: {room.name}",
            inline=False,
        )
        embed.set_footer(text="Your story continues with a new hero.")

        try:
            await manage_channel.send(content=f"<@{user_id}>", embed=embed)
        except discord.HTTPException:  # pragma: no cover - network guard
            log.debug(
                "Failed to send manage-channel prompt for %s in guild %s",
                user_id,
                guild_id,
            )

    async def _handle_party_failure_if_needed(self, session: DungeonSession) -> None:
        if session.party_fall_announced:
            return
        state = session.combat_state
        if state is None:
            return
        players = [c for c in state.order if c.is_player]
        if not players:
            return
        if any(not combatant.is_dead for combatant in players):
            return
        await self._handle_party_failure(session)

    async def _handle_party_failure(self, session: DungeonSession) -> None:
        session.party_fall_announced = True
        key = self._session_key(session.guild_id, session.channel_id)
        sessions = getattr(self, "sessions", None)
        removed: Optional[DungeonSession] = None
        if sessions is not None and hasattr(sessions, "pop"):
            removed = await sessions.pop(key)
        if removed is None:
            removed = session
        else:
            removed.party_fall_announced = True
        removed.party_ids.update(session.party_ids)

        get_guild = getattr(self.bot, "get_guild", None)
        if removed.guild_id is not None and callable(get_guild):
            guild = get_guild(removed.guild_id)
        else:
            guild = None

        if removed.message_id is not None and guild is not None:
            channel = guild.get_channel(removed.channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(removed.message_id)
                except (discord.HTTPException, AttributeError):
                    message = None
                else:
                    try:
                        await message.edit(view=None)
                    except discord.HTTPException:
                        pass

        announcement_channel = await self._find_tavern_channel(removed.guild_id)
        targets: list[discord.TextChannel] = []
        if announcement_channel is not None:
            targets.append(announcement_channel)
        elif guild is not None:
            channel = guild.get_channel(removed.channel_id)
            if isinstance(channel, discord.TextChannel):
                targets.append(channel)

        embed = self._build_party_failure_embed(removed)
        fallen_text = (
            f"The party has fallen in {removed.dungeon.name}! No survivors returned."
        )

        delivered: set[int] = set()
        for target in targets:
            if target.id in delivered:
                continue
            try:
                await target.send(content=fallen_text, embed=embed)
            except discord.HTTPException:
                continue
            delivered.add(target.id)

        asyncio.create_task(self._run_delayed_party_cleanup(removed))
        await self._update_tavern_access(removed.guild_id)

    def _normalise_party_channel_name(self, dungeon_name: str) -> str:
        slug = re.sub(r"[^a-z0-9\-\s]", "", dungeon_name.lower())
        slug = re.sub(r"\s+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not slug:
            slug = "delve"
        base = f"delve-{slug}"
        return base[:95]

    def _compose_party_channel_name(self, base_name: str, suffix: str) -> str:
        suffix = suffix.lower()
        if not suffix:
            return base_name[:95]
        max_length = 95
        trimmed_base = base_name
        extra_length = len(suffix) + 1
        if len(trimmed_base) + extra_length > max_length:
            allowed = max_length - extra_length
            if allowed <= 0:
                trimmed_base = "delve"
            else:
                trimmed_base = trimmed_base[:allowed].rstrip("-") or "delve"
        return f"{trimmed_base}-{suffix}"

    async def _preferred_delve_category(
        self, guild: discord.Guild
    ) -> Optional[discord.CategoryChannel]:
        tavern_cog = self._get_tavern_cog()
        if tavern_cog is not None:
            config = await tavern_cog.config_store.get_config(guild.id)
            if config and config.category_id:
                category = guild.get_channel(config.category_id)
                if isinstance(category, discord.CategoryChannel):
                    return category

        category_id = await self.metadata_store.get_delve_category(guild.id)
        if category_id is None:
            return None
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category
        await self.metadata_store.set_delve_category(guild.id, None)
        return None

    async def _ensure_party_channel(
        self, guild: discord.Guild, *, dungeon_name: str
    ) -> discord.TextChannel:
        """Return an available party channel for the guild.

        Reuses an existing text channel if it is not currently associated with an
        active dungeon session. Otherwise, attempts to create a new text channel
        with a derived name.
        """

        base_name = self._normalise_party_channel_name(dungeon_name)
        try:
            active_keys = await self.sessions.keys()
        except Exception:  # pragma: no cover - defensive
            active_keys = ()
        active_channels = {
            channel_id for guild_id, channel_id in active_keys if guild_id == guild.id
        }

        prefix = base_name
        for channel in guild.text_channels:
            if channel.id in active_channels:
                continue
            if channel.name == prefix or channel.name.startswith(f"{prefix}-"):
                if isinstance(channel, discord.TextChannel):
                    return channel

        topic = f"Dungeon party channel for {dungeon_name}"[:1024]
        existing_names = {channel.name for channel in guild.text_channels}
        category = await self._preferred_delve_category(guild)
        for _ in range(20):
            suffix = secrets.token_hex(2)
            name = self._compose_party_channel_name(prefix, suffix)
            if name in existing_names:
                continue
            try:
                channel = await guild.create_text_channel(
                    name,
                    topic=topic,
                    category=category,
                    reason="Dungeon party channel created for a delve",
                )
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                log.warning(
                    "Failed to create party channel %s in %s: %s", name, guild, exc
                )
                continue
            else:
                existing_names.add(name)
                return channel

        raise RuntimeError("Unable to provision a party channel")

    async def _sync_party_channel_access(self, session: DungeonSession) -> None:
        if session.guild_id is None:
            return
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(session.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        default_role = guild.default_role
        overwrites = channel.overwrites_for(default_role)
        if overwrites.view_channel is not False:
            try:
                await channel.set_permissions(default_role, view_channel=False)
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Failed to restrict default role access in %s", channel)

        allowed_ids = set(session.party_ids)
        for member_id in allowed_ids:
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    log.debug(
                        "Failed to fetch party member %s for guild %s", member_id, guild.id
                    )
                    continue
            overwrite = channel.overwrites.get(member)
            if (
                overwrite is not None
                and overwrite.view_channel is True
                and overwrite.send_messages is True
                and overwrite.read_message_history is True
            ):
                continue
            permission = discord.PermissionOverwrite()
            permission.view_channel = True
            permission.send_messages = True
            permission.read_message_history = True
            try:
                await channel.set_permissions(member, overwrite=permission)
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Failed to grant party access to %s in %s", member, channel)

        for target, _ in list(channel.overwrites.items()):
            if not isinstance(target, discord.Member):
                continue
            if target.id in allowed_ids:
                continue
            if target.guild_permissions.manage_channels:
                continue
            try:
                await channel.set_permissions(target, overwrite=None)
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Failed to revoke party access from %s in %s", target, channel)

    async def _clear_party_channel_access(self, session: DungeonSession) -> None:
        if session.guild_id is None:
            return
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(session.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        default_role = guild.default_role
        overwrites = channel.overwrites_for(default_role)
        if overwrites.view_channel is not False:
            try:
                await channel.set_permissions(default_role, view_channel=False)
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Failed to restrict default role access in %s", channel)

        for target in list(channel.overwrites):
            if not isinstance(target, discord.Member):
                continue
            if target.guild_permissions.manage_channels:
                continue
            try:
                await channel.set_permissions(target, overwrite=None)
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Failed to clear party access for %s in %s", target, channel)

    async def _run_delayed_party_cleanup(self, session: DungeonSession) -> None:
        await asyncio.sleep(60)

        if session.guild_id is None:
            return

        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(session.channel_id)
        if channel is None:
            return

        try:
            await self._clear_party_channel_access(session)
        except Exception:  # pragma: no cover - defensive safeguard
            log.exception(
                "Unexpected error while clearing party channel access for %s", channel
            )

    async def _ensure_character_available(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.guild_id is None:
            await self._send_ephemeral_message(
                interaction,
                "Dungeon expeditions can only be joined from within a server.",
            )
            return False
        try:
            has_character = await self.characters.exists(
                interaction.guild_id, interaction.user.id
            )
        except Exception:
            has_character = False
        if not has_character:
            await self._send_ephemeral_message(
                interaction,
                "You need a character before venturing forth. Visit the tavern and run /character create first.",
            )
            return False
        return True

    async def _handle_party_membership_change(
        self, guild_id: int, session: DungeonSession
    ) -> None:
        await self._update_tavern_access(guild_id)
        await self._sync_party_channel_access(session)

    async def _load_party_characters(
        self, guild_id: int, party_ids: Iterable[int]
    ) -> Dict[int, Character]:
        characters: Dict[int, Character] = {}
        for user_id in party_ids:
            try:
                character = await self.characters.get(guild_id, user_id)
            except Exception as exc:
                log.warning(
                    "Failed to load character for user %s in guild %s: %s",
                    user_id,
                    guild_id,
                    exc,
                )
                continue
            if character is not None:
                characters[user_id] = character
        return characters

    async def _apply_reward_shares(
        self,
        guild_id: int,
        characters: MutableMapping[int, Character],
        shares: Sequence[RewardShare],
    ) -> tuple[list[str], list[str], tuple[int, int]]:
        item_lines: list[str] = []
        gold_lines: list[str] = []
        delivered_items = 0
        delivered_gold = 0
        for share in shares:
            character = characters.get(share.user_id)
            if character is None:
                continue
            new_inventory = list(character.inventory)
            new_items: list[str] = []
            for item in share.items:
                label = format_item_label(item)
                new_inventory.append(label)
                new_items.append(label)
            updated_character = replace(
                character,
                inventory=tuple(new_inventory),
                gold_coins=character.gold_coins + share.gold,
            )
            try:
                await self.characters.save(updated_character)
            except Exception as exc:
                log.warning(
                    "Failed to persist rewards for user %s in guild %s: %s",
                    share.user_id,
                    guild_id,
                    exc,
                )
                continue
            characters[share.user_id] = updated_character
            if new_items:
                item_lines.append(
                    f"• <@{share.user_id}> gains {', '.join(new_items)}"
                )
                delivered_items += len(new_items)
            if share.gold:
                gold_lines.append(
                    f"• <@{share.user_id}> gains {share.gold} gold coins"
                )
                delivered_gold += share.gold
        return item_lines, gold_lines, (delivered_items, delivered_gold)

    def _get_tavern_cog(self) -> Optional["Tavern"]:
        get_cog = getattr(self.bot, "get_cog", None)
        if get_cog is None:
            return None
        cog = get_cog("Tavern")
        if cog is None:
            return None
        try:
            from cogs.tavern import Tavern
        except ImportError:  # pragma: no cover - defensive
            return None
        return cog if isinstance(cog, Tavern) else None

    async def _update_tavern_access(self, guild_id: Optional[int]) -> None:
        if guild_id is None:
            return
        tavern_cog = self._get_tavern_cog()
        if tavern_cog is None:
            return
        try:
            await tavern_cog.refresh_tavern_access(guild_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Failed to refresh tavern access for %s: %s", guild_id, exc)

    async def _resolve_theme(self, theme_name: Optional[str], guild_id: Optional[int]) -> Theme:
        if theme_name:
            return self.theme_registry.get(theme_name)
        if guild_id is not None:
            cached = self._guild_theme_cache.get(guild_id)
            if cached:
                return self.theme_registry.get(cached)
            stored = await self.metadata_store.get_default_theme(guild_id)
            if stored:
                try:
                    theme = self.theme_registry.get(stored)
                except KeyError:
                    await self.metadata_store.set_default_theme(guild_id, None)
                    self._guild_theme_cache[guild_id] = None
                else:
                    self._guild_theme_cache[guild_id] = theme.key
                    return theme
            self._guild_theme_cache[guild_id] = None
        theme = self.theme_registry.first()
        if theme is None:
            raise RuntimeError("No dungeon themes are available")
        return theme

    async def _attempt_room_stealth(
        self,
        interaction: discord.Interaction,
        session: DungeonSession,
        party_snapshot: Sequence[int],
    ) -> tuple[bool, str]:
        if not party_snapshot:
            return False, "No adventurers are present to attempt a stealth approach."

        characters: Dict[int, Character] = {}
        if session.guild_id is not None:
            characters = await self._load_party_characters(session.guild_id, party_snapshot)

        rolls: list[tuple[int, int, int, int]] = []
        for user_id in party_snapshot:
            dex_mod = 0
            character = characters.get(user_id)
            if character is not None:
                ability_scores = getattr(character, "ability_scores", None)
                values = getattr(ability_scores, "values", None)
                if isinstance(values, Mapping):
                    dex_value = None
                    for ability_key, score in values.items():
                        key_upper = str(ability_key).upper()
                        if key_upper in {"DEX", "DEXTERITY"}:
                            dex_value = score
                            break
                    if dex_value is not None:
                        try:
                            dex_mod = ability_modifier(int(dex_value))
                        except (TypeError, ValueError):
                            dex_mod = 0
            roll = random.randint(1, 20)
            total = roll + dex_mod
            rolls.append((user_id, roll, dex_mod, total))

        if not rolls:
            return False, "No adventurers are present to attempt a stealth approach."

        _, best_roll, best_mod, best_total = max(rolls, key=lambda entry: entry[3])

        passive_entries: list[tuple[str, int]] = []
        monster_labels = self._unique_monster_labels(session.room.encounter.monsters)
        for monster, label in zip(session.room.encounter.monsters, monster_labels):
            ability_scores = monster.ability_scores or {}
            wisdom_value: Optional[int] = None
            for ability_key, score in ability_scores.items():
                key_upper = str(ability_key).upper()
                if key_upper in {"WIS", "WISDOM"}:
                    wisdom_value = int(score)
                    break
            if wisdom_value is None:
                wisdom_value = 10
            passive = 10 + ability_modifier(wisdom_value)
            passive_entries.append((label, passive))

        if not passive_entries:
            return True, "No creatures are present to oppose the party's approach."

        success = all(best_total >= passive for _, passive in passive_entries)
        roll_text = f"{best_roll}{best_mod:+d}" if best_mod else str(best_roll)
        passive_text = ", ".join(f"{name} {value}" for name, value in passive_entries)
        if success:
            summary = (
                f"The party remains hidden (Stealth {best_total} "
                f"— roll {roll_text} vs passive {passive_text})."
            )
        else:
            summary = (
                f"The monsters spot the party (Stealth {best_total} "
                f"— roll {roll_text} vs passive {passive_text})."
            )
        return success, summary

    def _party_display(
        self, interaction: Optional[discord.Interaction], session: DungeonSession
    ) -> str:
        if not session.party_ids:
            return "No adventurers yet."

        entries: list[str] = []
        guild: Optional[discord.Guild]
        if interaction is not None and interaction.guild is not None:
            guild = interaction.guild
        elif session.guild_id is not None:
            guild = self.bot.get_guild(session.guild_id)
        else:
            guild = None
        for user_id in sorted(session.party_ids):
            name = self._display_name_for_user(
                user_id, interaction=interaction, guild=guild
            )
            record = session.party_health.get(user_id)
            max_hp_value = DEFAULT_PLAYER_HP
            current_hp_value = DEFAULT_PLAYER_HP
            if isinstance(record, Mapping):
                try:
                    max_hp_value = int(record.get("max", max_hp_value))
                except (TypeError, ValueError):
                    max_hp_value = DEFAULT_PLAYER_HP
                try:
                    current_hp_value = int(record.get("current", current_hp_value))
                except (TypeError, ValueError):
                    current_hp_value = max_hp_value
            max_hp_value = max(1, max_hp_value)
            current_hp_value = max(0, min(current_hp_value, max_hp_value))
            entries.append(f"• {name} [{current_hp_value}/{max_hp_value}]")
        return "\n".join(entries)

    def _display_name_for_user(
        self,
        user_id: int,
        *,
        interaction: Optional[discord.Interaction] = None,
        guild: Optional[discord.Guild] = None,
    ) -> str:
        name: Optional[str] = None
        lookup_guild = guild
        if lookup_guild is None and interaction is not None:
            lookup_guild = interaction.guild
        if lookup_guild is not None:
            member = lookup_guild.get_member(user_id)
            if member is not None:
                name = member.display_name
        if name is None:
            user = self.bot.get_user(user_id)
            if user is not None:
                name = user.display_name
        if name is None:
            name = f"<@{user_id}>"
        return name

    def _ensure_room_trap_state(self, session: DungeonSession, room: Room) -> None:
        self._ensure_room_discovery_state(session, room)
        catalog = session.trap_catalog.setdefault(room.id, {})
        states = session.trap_states.setdefault(room.id, {})
        for trap in room.encounter.traps:
            if trap.key not in catalog:
                catalog[trap.key] = trap
            states.setdefault(trap.key, "hidden")

    def _ensure_room_discovery_state(self, session: DungeonSession, room: Room) -> None:
        session.discovered_traps.setdefault(room.id, set())
        session.discovered_loot.setdefault(room.id, set())
        discovered_exits = session.discovered_exits.setdefault(room.id, set())
        visibility_map = session.exit_visibility.setdefault(room.id, {})
        session.perception_attempts.setdefault(room.id, {})
        session.perception_difficulties.setdefault(room.id, {})

        for exit_option in room.exits:
            if exit_option.key in discovered_exits:
                visibility_map[exit_option.key] = True

        previous_room = session.breadcrumbs[-2] if len(session.breadcrumbs) >= 2 else None

        for exit_option in room.exits:
            if exit_option.destination == previous_room:
                discovered_exits.add(exit_option.key)
                visibility_map[exit_option.key] = True

        if not discovered_exits and session.dungeon.rooms:
            starting_room_id = session.dungeon.rooms[0].id
            if room.id == starting_room_id and room.exits:
                first_exit = room.exits[0]
                destination = getattr(first_exit, "destination", None)
                if len(room.exits) > 1 or (
                    destination is not None and destination != room.id
                ):
                    discovered_exits.add(first_exit.key)
                    visibility_map[first_exit.key] = True
        for exit_option in room.exits:
            if getattr(exit_option, "completes_delve", False):
                discovered_exits.add(exit_option.key)
                visibility_map[exit_option.key] = True
                continue

            if exit_option.key not in visibility_map:
                hidden = self._exit_starts_hidden(session, room, exit_option)
                visibility_map[exit_option.key] = not hidden

            if visibility_map[exit_option.key]:
                discovered_exits.add(exit_option.key)

    def _exit_starts_hidden(
        self, session: DungeonSession, room: Room, exit_option: RoomExit
    ) -> bool:
        label = (exit_option.label or "").lower()
        key = exit_option.key.lower()
        if "secret" in label or "hidden" in label or "secret" in key or "hidden" in key:
            return True
        return random.random() < HIDDEN_EXIT_BASE_CHANCE

    def _get_trap_status(
        self, session: DungeonSession, room_id: int, trap_key: str
    ) -> TrapStatus:
        return session.trap_states.get(room_id, {}).get(trap_key, "hidden")

    def _set_trap_status(
        self, session: DungeonSession, room_id: int, trap_key: str, status: TrapStatus
    ) -> None:
        states = session.trap_states.setdefault(room_id, {})
        states[trap_key] = status
        discovered = session.discovered_traps.setdefault(room_id, set())
        if status in {"discovered", "disarmed", "sprung"}:
            discovered.add(trap_key)
        elif status == "hidden":
            discovered.discard(trap_key)

    def _room_has_discovered_traps(
        self, session: DungeonSession, room: Optional[Room] = None
    ) -> bool:
        target = room or session.room
        self._ensure_room_trap_state(session, target)
        discovered = session.discovered_traps.get(target.id, set())
        for trap in target.encounter.traps:
            if trap.key in discovered:
                return True
        return False

    def _parse_trap_damage(self, trap: Trap) -> tuple[str, Optional[str]]:
        damage = trap.damage
        if not damage:
            return "0", None
        text = str(damage).strip()
        if not text:
            return "0", None
        parts = text.split()
        expression = parts[0]
        damage_type = " ".join(parts[1:]) if len(parts) > 1 else None
        return expression, damage_type or None

    async def _resolve_trap_trigger(
        self,
        interaction: discord.Interaction,
        session: DungeonSession,
        *,
        trap: Trap,
        party_snapshot: tuple[int, ...],
    ) -> list[str]:
        saving_throw_data = trap.saving_throw or {}
        ability_value = saving_throw_data.get("ability", "DEX")
        ability = str(ability_value).upper()
        dc_raw = saving_throw_data.get("dc", 15)
        try:
            dc = int(dc_raw)
        except (TypeError, ValueError):
            dc = 15

        ordered_party = party_snapshot or tuple(sorted(session.party_ids))
        characters: Dict[int, Character] = {}
        if session.guild_id is not None and ordered_party:
            characters = await self._load_party_characters(
                session.guild_id, ordered_party
            )

        damage_expression, damage_type = self._parse_trap_damage(trap)
        damage_amount = 0
        if damage_expression and damage_expression != "0":
            rolled_damage = self._roll_damage(damage_expression)
            damage_amount = apply_damage(
                (
                    DamagePacket(
                        amount=rolled_damage,
                        damage_type=damage_type.lower() if damage_type else None,
                    ),
                )
            )

        session.traps_triggered += 1
        lines: list[str] = [f"The {trap.name} is sprung!"]
        if trap.damage:
            description = trap.damage
            lines.append(f"It unleashes {description}.")
        if not ordered_party:
            return lines

        lines.append(f"DC {dc} {ability} saving throws:")
        pending_deaths: list[CombatantState] = []
        for user_id in ordered_party:
            bonus = 0
            character = characters.get(user_id)
            if character is not None:
                score = character.ability_scores.values.get(ability, 10)
                bonus = ability_modifier(score)
            result = saving_throw(bonus, dc)
            outcome = "success" if result.success else "failure"
            name = self._display_name_for_user(
                user_id, interaction=interaction
            )
            stored_health = session.party_health.get(user_id)

            def _coerce_int(value: object, default: int) -> int:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default

            max_hp_value = DEFAULT_PLAYER_HP
            current_hp_value = DEFAULT_PLAYER_HP
            if isinstance(stored_health, Mapping):
                max_hp_value = _coerce_int(stored_health.get("max"), DEFAULT_PLAYER_HP)
                current_hp_value = _coerce_int(
                    stored_health.get("current"), max_hp_value
                )
            elif character is not None:
                try:
                    hit_die = int(character.character_class.hit_die)
                except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
                    hit_die = DEFAULT_PLAYER_HP
                try:
                    constitution = int(character.ability_scores.values.get("CON", 10))
                except (TypeError, ValueError):
                    constitution = 10
                max_hp_value = max(1, hit_die + ability_modifier(constitution))
                current_hp_value = max_hp_value
            max_hp_value = max(1, max_hp_value)
            current_hp_value = max(0, min(current_hp_value, max_hp_value))
            record = session.party_health.setdefault(
                user_id, {"current": current_hp_value, "max": max_hp_value}
            )
            record["max"] = max_hp_value
            record["current"] = current_hp_value

            damage_taken = 0
            fatal = False
            post_hp = current_hp_value
            actual_damage = 0
            if damage_amount:
                damage_taken = damage_amount if not result.success else damage_amount // 2
                damage_taken = max(0, damage_taken)
                if damage_taken:
                    post_hp = max(0, current_hp_value - damage_taken)
                    actual_damage = current_hp_value - post_hp
                    record["current"] = post_hp
                    if actual_damage > 0:
                        self._record_room_damage(session, actual_damage, source="trap")
                    if post_hp <= 0:
                        overkill = damage_taken - current_hp_value
                        fatal = current_hp_value > 0 and overkill >= max_hp_value
                        if fatal:
                            death_state = CombatantState(
                                identifier=f"player:{user_id}",
                                name=name,
                                initiative_roll=0,
                                initiative_total=0,
                                max_hp=max_hp_value,
                                current_hp=post_hp,
                                is_player=True,
                                user_id=user_id,
                            )
                            death_state.conditions.add("Dead")
                            death_state.death_save_failures = 3
                            key = self._fallen_identifier(death_state)
                            if key not in session.fallen_players:
                                session.fallen_players.add(key)
                                pending_deaths.append(death_state)
                else:
                    post_hp = current_hp_value
            line = f"• {name}: {result.total} ({outcome})"
            if damage_amount:
                if damage_taken:
                    line += f" — {damage_taken} damage"
                else:
                    line += " — no damage"
                line += f" ({post_hp}/{max_hp_value} HP)"
                if fatal:
                    line += " — slain!"
                elif damage_taken and post_hp <= 0:
                    line += " — knocked unconscious!"
            else:
                line += f" — {post_hp}/{max_hp_value} HP"
            lines.append(line)

        for fallen in pending_deaths:
            await self._announce_player_death(session, fallen)
        return lines

    def _roll_damage(
        self,
        expression: str,
        *,
        critical: bool = False,
        extra_dice: Optional[Sequence[str]] = None,
    ) -> int:
        match = _DAMAGE_ROLL_PATTERN.fullmatch(expression.strip())
        if match:
            count = max(1, int(match.group("count")))
            sides = max(1, int(match.group("sides")))
            modifier = int(match.group("modifier") or 0)
            rolls = [
                random.randint(1, sides)
                for _ in range(count * (2 if critical else 1))
            ]
            total = sum(rolls) + modifier
        else:
            total = random.randint(1, 8)
        if extra_dice:
            for dice_expression in extra_dice:
                text = str(dice_expression)
                extra_match = _DAMAGE_ROLL_PATTERN.fullmatch(text.strip())
                if not extra_match:
                    continue
                extra_count = max(1, int(extra_match.group("count")))
                extra_sides = max(1, int(extra_match.group("sides")))
                extra_modifier = int(extra_match.group("modifier") or 0)
                extra_rolls = [
                    random.randint(1, extra_sides)
                    for _ in range(extra_count * (2 if critical else 1))
                ]
                total += sum(extra_rolls) + extra_modifier
        return max(0, total)

    def _trim_combat_log(self, state: CombatState) -> None:
        excess = len(state.log) - MAX_COMBAT_LOG_ENTRIES
        if excess > 0:
            del state.log[0:excess]

    @staticmethod
    def _ensure_room_damage_entry(
        session: DungeonSession, room_id: Optional[int] = None
    ) -> Dict[str, int]:
        room_key = session.current_room if room_id is None else room_id
        entry = session.room_damage_log.setdefault(room_key, {})
        entry.setdefault("monsters", 0)
        entry.setdefault("traps", 0)
        return entry

    def _update_party_health_tracking(
        self, session: DungeonSession, combatant: CombatantState
    ) -> None:
        if not combatant.is_player or combatant.user_id is None:
            return
        record = session.party_health.setdefault(
            combatant.user_id, {"current": combatant.max_hp, "max": combatant.max_hp}
        )
        record["max"] = combatant.max_hp
        record["current"] = max(0, min(combatant.max_hp, combatant.current_hp))

    def _record_room_damage(
        self,
        session: DungeonSession,
        amount: int,
        *,
        source: Literal["monster", "trap", "other"] = "other",
    ) -> None:
        if amount <= 0:
            return
        if source not in {"monster", "trap"}:
            return
        entry = self._ensure_room_damage_entry(session)
        key = "monsters" if source == "monster" else "traps"
        entry[key] = entry.get(key, 0) + amount

    def _sync_combatant_state(
        self,
        combatant: CombatantState,
        session: Optional[DungeonSession] = None,
    ) -> None:
        metadata = combatant.metadata
        metadata["current_hp"] = combatant.current_hp
        metadata["max_hp"] = combatant.max_hp
        metadata["conditions"] = sorted(combatant.conditions)
        metadata["concentration"] = combatant.concentration
        metadata["resources"] = combatant.resources
        metadata["death_saves"] = {
            "successes": combatant.death_save_successes,
            "failures": combatant.death_save_failures,
            "stable": combatant.stable,
        }
        metadata["is_dead"] = combatant.is_dead
        if session is not None:
            self._update_party_health_tracking(session, combatant)

    def _apply_damage_to_combatant(
        self,
        session: Optional[DungeonSession],
        combatant: CombatantState,
        amount: int,
        *,
        critical: bool = False,
        source: Literal["monster", "trap", "other"] = "other",
    ) -> int:
        if amount <= 0:
            return 0
        previous = combatant.current_hp
        combatant.current_hp = max(0, combatant.current_hp - amount)
        if combatant.current_hp <= 0:
            combatant.conditions.add("Unconscious")
            combatant.concentration = None
            if combatant.is_player and previous > 0:
                combatant.death_save_successes = 0
                combatant.death_save_failures = 0
                combatant.stable = False
        else:
            combatant.conditions.discard("Unconscious")
        dealt = previous - combatant.current_hp
        if combatant.is_player and previous <= 0 and dealt > 0:
            combatant.stable = False
            failures = 2 if critical else 1
            combatant.death_save_failures = min(3, combatant.death_save_failures + failures)
        if combatant.is_player and combatant.death_save_failures >= 3:
            combatant.conditions.add("Dead")
            combatant.conditions.discard("Unconscious")
        if combatant.is_player and combatant.current_hp > 0:
            combatant.death_save_successes = 0
            combatant.death_save_failures = 0
            combatant.stable = False
            combatant.conditions.discard("Dead")
        self._sync_combatant_state(combatant, session)
        if session is not None and combatant.is_player:
            self._record_room_damage(session, dealt, source=source)
        return dealt

    def _apply_healing_to_combatant(
        self,
        session: Optional[DungeonSession],
        combatant: CombatantState,
        amount: int,
    ) -> int:
        if amount <= 0:
            return 0
        previous = combatant.current_hp
        combatant.current_hp = min(combatant.max_hp, combatant.current_hp + amount)
        if combatant.current_hp > 0:
            combatant.conditions.discard("Unconscious")
            combatant.conditions.discard("Dead")
            combatant.death_save_successes = 0
            combatant.death_save_failures = 0
            combatant.stable = False
        healed = combatant.current_hp - previous
        self._sync_combatant_state(combatant, session)
        return healed

    @staticmethod
    def _death_save_roll() -> int:
        return random.randint(1, 20)

    def _resolve_player_death_save(
        self, session: Optional[DungeonSession], player: CombatantState
    ) -> str:
        roll = self._death_save_roll()
        message: list[str] = [f"{player.name} rolls a {roll} on their death save."]
        if roll == 20:
            player.current_hp = max(1, player.current_hp)
            player.death_save_successes = 0
            player.death_save_failures = 0
            player.stable = False
            player.conditions.discard("Unconscious")
            player.conditions.discard("Dead")
            message.append(f"{player.name} surges back to life with 1 HP!")
        elif roll == 1:
            player.death_save_failures = min(3, player.death_save_failures + 2)
            player.stable = False
            message.append("Critical failure—two death save failures recorded.")
        elif roll >= 10:
            player.death_save_successes = min(3, player.death_save_successes + 1)
            message.append("Success!")
        else:
            player.death_save_failures = min(3, player.death_save_failures + 1)
            player.stable = False
            message.append("Failure.")
        if player.death_save_failures >= 3:
            player.conditions.add("Dead")
            player.conditions.discard("Unconscious")
            message.append(f"{player.name} succumbs to their wounds.")
        elif roll != 20 and player.death_save_successes >= 3:
            player.stable = True
            message.append(f"{player.name} stabilises but remains unconscious.")
        self._sync_combatant_state(player, session)
        return " ".join(message)

    def _handle_player_zero_hp_turn(
        self,
        state: CombatState,
        player: CombatantState,
        *,
        session: Optional[DungeonSession] = None,
    ) -> bool:
        if player.current_hp > 0:
            return False
        if player.is_dead or player.stable:
            state.waiting_for = None
            return True
        if player.user_id is None:
            state.waiting_for = None
            message = self._resolve_player_death_save(session, player)
            state.log.append(message)
            self._trim_combat_log(state)
            return True
        state.waiting_for = player.user_id
        state.current_action = {
            "actor": player.name,
            "state": "awaiting death save",
            "summary": "Awaiting a death saving throw...",
            "detail": "Roll a death saving throw to cling to life.",
            "emoji": "🩸",
            "team": "player",
        }
        return False

    @staticmethod
    def _resolve_selection_index(selection: Optional[str], prefix: str, default: int = 0) -> int:
        if selection is None:
            return default
        value = str(selection)
        if value.startswith(f"{prefix}:"):
            value = value.split(":", 1)[1]
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _resolve_target_identifier(selection: Optional[str]) -> Optional[str]:
        if selection is None:
            return None
        value = str(selection)
        if value.startswith("target:"):
            value = value.split(":", 1)[1]
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _find_combatant_by_identifier(
        state: CombatState, identifier: str
    ) -> Optional[CombatantState]:
        for combatant in state.order:
            if combatant.identifier == identifier:
                return combatant
        return None

    @staticmethod
    def _unique_monster_labels(monsters: Sequence[MonsterDefinition]) -> List[str]:
        counts: Counter[str] = Counter(monster.name for monster in monsters)
        seen: Dict[str, int] = {}
        labels: List[str] = []
        for monster in monsters:
            base_name = monster.name
            if counts[base_name] > 1:
                seen[base_name] = seen.get(base_name, 0) + 1
                labels.append(f"{base_name} {seen[base_name]}")
            else:
                labels.append(base_name)
        return labels

    def _select_player_target(
        self,
        state: CombatState,
        player: CombatantState,
        *,
        update: bool = True,
    ) -> Optional[CombatantState]:
        enemies = [
            combatant
            for combatant in state.order
            if not combatant.is_player and not combatant.defeated
        ]
        if not enemies:
            if update:
                player.selected_target = None
            return None
        chosen: Optional[CombatantState] = None
        if player.selected_target:
            for enemy in enemies:
                if enemy.identifier == player.selected_target:
                    chosen = enemy
                    break
        if chosen is None and update:
            chosen = enemies[0]
        if chosen is not None and update:
            player.selected_target = chosen.identifier
        return chosen

    def _consume_resource(
        self, player: CombatantState, requirement: Optional[Mapping[str, object]]
    ) -> Tuple[bool, Optional[str]]:
        if not requirement:
            return True, None
        if not isinstance(requirement, Mapping):
            return False, "Invalid resource requirement provided."
        requirement_type = str(requirement.get("type", "")).lower()
        if requirement_type == "spell_slot":
            level_value = requirement.get("level", 1)
            level_key = str(level_value)
            try:
                amount = max(1, int(requirement.get("amount", 1)))
            except (TypeError, ValueError):
                amount = 1
            slots = player.resources.setdefault("spell_slots", {})
            slot_entry = slots.get(level_key)
            if slot_entry is None and level_key.isdigit():
                slot_entry = slots.get(int(level_key))
            if not isinstance(slot_entry, MutableMapping):
                return False, f"You have no level {level_value} spell slots remaining."
            available = int(slot_entry.get("available", slot_entry.get("remaining", 0)))
            if available < amount:
                return False, f"You have no level {level_value} spell slots remaining."
            available -= amount
            slot_entry["available"] = available
            if "remaining" in slot_entry:
                slot_entry["remaining"] = available
            return True, None
        if requirement_type == "pool":
            pool_name = str(requirement.get("pool", "feature_uses"))
            key = str(requirement.get("key", ""))
            if not key:
                return False, "This ability is not linked to a usable resource."
            try:
                amount = max(1, int(requirement.get("amount", 1)))
            except (TypeError, ValueError):
                amount = 1
            pools = player.resources.setdefault(pool_name, {})
            entry = pools.get(key)
            if not isinstance(entry, MutableMapping):
                max_uses = int(requirement.get("max", requirement.get("amount", amount)))
                entry = {"max": max_uses, "available": max_uses}
                pools[key] = entry
            available = int(entry.get("available", entry.get("remaining", entry.get("max", 0))))
            if available < amount:
                return False, f"You have no uses of {key} remaining."
            available -= amount
            entry["available"] = available
            if "remaining" in entry:
                entry["remaining"] = available
            return True, None
        return False, "This action cannot be resolved because its resource type is unknown."

    def _resource_status_text(
        self, player: CombatantState, requirement: Optional[Mapping[str, object]]
    ) -> Optional[str]:
        if not requirement or not isinstance(requirement, Mapping):
            return None
        requirement_type = str(requirement.get("type", "")).lower()
        if requirement_type == "spell_slot":
            level_value = requirement.get("level", 1)
            slots = player.resources.get("spell_slots", {})
            slot_entry = slots.get(str(level_value))
            if slot_entry is None and str(level_value).isdigit():
                slot_entry = slots.get(int(level_value))
            if isinstance(slot_entry, Mapping):
                available = slot_entry.get("available")
                max_uses = slot_entry.get("max")
                if available is not None:
                    if max_uses is not None:
                        return f"Level {level_value} slots remaining: {available}/{max_uses}."
                    return f"Level {level_value} slots remaining: {available}."
        if requirement_type == "pool":
            pool_name = str(requirement.get("pool", "feature_uses"))
            key = str(requirement.get("key", ""))
            pools = player.resources.get(pool_name, {})
            entry = pools.get(key)
            if isinstance(entry, Mapping):
                available = entry.get("available")
                max_uses = entry.get("max")
                if available is not None and max_uses is not None:
                    return f"{key}: {available}/{max_uses} uses remaining."
                if available is not None:
                    return f"{key}: {available} uses remaining."
        return None

    def _summarise_combatant_resources(self, combatant: CombatantState) -> Optional[str]:
        if not combatant.resources:
            return None
        parts: List[str] = []
        spell_slots = combatant.resources.get("spell_slots")
        if isinstance(spell_slots, Mapping):
            slot_parts: List[str] = []
            for level_key, payload in sorted(spell_slots.items(), key=lambda item: str(item[0])):
                if not isinstance(payload, Mapping):
                    continue
                available = payload.get("available")
                max_uses = payload.get("max")
                if available is None or max_uses is None:
                    continue
                slot_parts.append(f"L{level_key}:{available}/{max_uses}")
            if slot_parts:
                parts.append("Slots " + ", ".join(slot_parts))
        feature_uses = combatant.resources.get("feature_uses")
        if isinstance(feature_uses, Mapping):
            for name, payload in feature_uses.items():
                if not isinstance(payload, Mapping):
                    continue
                available = payload.get("available")
                max_uses = payload.get("max")
                if available is None or max_uses is None:
                    continue
                parts.append(f"{name}: {available}/{max_uses}")
        return " | ".join(parts) if parts else None

    def _any_players_alive(self, state: CombatState) -> bool:
        return any(combatant.is_player and not combatant.defeated for combatant in state.order)

    def _any_monsters_alive(self, state: CombatState) -> bool:
        return any((not combatant.is_player) and not combatant.defeated for combatant in state.order)

    def _ensure_current_combatant(self, state: CombatState) -> Optional[CombatantState]:
        current = state.current_combatant()
        if current is None:
            return None
        if current.defeated:
            return state.advance_turn()
        return current

    def _finish_combat(self, session: DungeonSession, state: CombatState, *, victory: bool) -> None:
        state.active = False
        state.waiting_for = None
        state.current_action = None
        session.stealthed = False
        self._ensure_room_damage_entry(session)
        for combatant in state.order:
            if combatant.is_player:
                self._update_party_health_tracking(session, combatant)
        if victory:
            state.log.append("The party is victorious!")
            encounter = session.room.encounter
            session.monsters_defeated += len(encounter.monsters)
            session.room.encounter = replace(
                encounter,
                monsters=tuple(),
            )
        else:
            state.log.append("The party has fallen...")
        self._trim_combat_log(state)

    def _evaluate_combat_state(self, session: DungeonSession, state: CombatState) -> None:
        if not state.active:
            return
        if not self._any_monsters_alive(state):
            self._finish_combat(session, state, victory=True)
        elif not self._any_players_alive(state):
            self._finish_combat(session, state, victory=False)

    @staticmethod
    def _normalise_damage_traits(value: object) -> Set[str]:
        traits: Set[str] = set()
        if not value:
            return traits
        if isinstance(value, str):
            cleaned = value.strip().lower()
            if cleaned:
                traits.add(cleaned)
            return traits
        if isinstance(value, Mapping):
            iterable = value.values()
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            iterable = value
        else:
            return traits
        for entry in iterable:
            if not entry:
                continue
            cleaned = str(entry).strip().lower()
            if cleaned:
                traits.add(cleaned)
        return traits

    @staticmethod
    def _monster_action_key(action: Mapping[str, object]) -> str:
        key = action.get("key") or action.get("name")
        return str(key).lower() if key is not None else ""

    def _monster_actions(self, monster: CombatantState) -> List[Mapping[str, object]]:
        actions_raw = monster.metadata.get("actions")
        actions: List[Mapping[str, object]] = []
        if isinstance(actions_raw, Sequence) and not isinstance(actions_raw, (str, bytes)):
            for entry in actions_raw:
                if isinstance(entry, Mapping):
                    actions.append(entry)
        return actions

    def _find_monster_action(
        self, actions: Sequence[Mapping[str, object]], reference: object
    ) -> Optional[Mapping[str, object]]:
        if reference is None:
            return None
        key = str(reference).lower()
        for action in actions:
            if self._monster_action_key(action) == key:
                return action
        return None

    def _expand_multiattack_actions(
        self, monster: CombatantState, actions: Sequence[Mapping[str, object]]
    ) -> List[Mapping[str, object]]:
        multiattack_raw = monster.metadata.get("multiattack")
        if not multiattack_raw:
            return []
        if isinstance(multiattack_raw, Mapping):
            payload = multiattack_raw.get("attacks") or multiattack_raw.get("actions")
        else:
            payload = multiattack_raw
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            return []
        expanded: List[Mapping[str, object]] = []
        for entry in payload:
            if isinstance(entry, Mapping):
                count = entry.get("count", 1)
                try:
                    repeats = max(1, int(count))
                except (TypeError, ValueError):
                    repeats = 1
                if "ref" in entry:
                    ref = self._find_monster_action(actions, entry.get("ref"))
                    if ref is None:
                        continue
                    for _ in range(repeats):
                        expanded.append(ref)
                else:
                    for _ in range(repeats):
                        expanded.append(entry)
            elif isinstance(entry, str):
                ref = self._find_monster_action(actions, entry)
                if ref is not None:
                    expanded.append(ref)
        return expanded

    def _create_monster_attack(
        self,
        monster: CombatantState,
        action: Optional[Mapping[str, object]],
    ) -> tuple[Attack, Tuple[str, ...]]:
        metadata = monster.metadata
        action_data: Mapping[str, object] = action or {}
        name = str(
            action_data.get(
                "name",
                metadata.get("attack_name")
                or metadata.get("weapon_name")
                or f"{monster.name} Attack",
            )
        )
        try:
            attack_bonus = int(action_data.get("attack_bonus", metadata.get("attack_bonus", 0)))
        except (TypeError, ValueError):
            attack_bonus = int(metadata.get("attack_bonus", 0))
        damage_expr = str(action_data.get("damage", metadata.get("damage", "1d6+1")))
        damage_type_raw = action_data.get("damage_type", metadata.get("damage_type"))
        damage_type = str(damage_type_raw).lower() if damage_type_raw else None
        base_damage = self._roll_damage(damage_expr)
        packet = DamagePacket(amount=base_damage, damage_type=damage_type)
        advantage_sources = ()
        disadvantage_sources = ()
        advantage_raw = action_data.get("advantage")
        if isinstance(advantage_raw, Sequence) and not isinstance(advantage_raw, (str, bytes)):
            advantage_sources = tuple(str(value) for value in advantage_raw)
        disadvantage_raw = action_data.get("disadvantage")
        if isinstance(disadvantage_raw, Sequence) and not isinstance(disadvantage_raw, (str, bytes)):
            disadvantage_sources = tuple(str(value) for value in disadvantage_raw)
        critical_double = bool(action_data.get("critical_double", True))
        extra_raw = action_data.get("critical_extra_dice")
        if isinstance(extra_raw, Sequence) and not isinstance(extra_raw, (str, bytes)):
            extra_dice = tuple(str(value) for value in extra_raw)
        else:
            extra_dice = ()
        attack = Attack(
            name=name,
            attack_bonus=attack_bonus,
            damage_packets=(packet,),
            advantage_sources=advantage_sources,
            disadvantage_sources=disadvantage_sources,
            critical_double=critical_double,
        )
        return attack, extra_dice

    def _apply_conditions_to_target(
        self, target: CombatantState, conditions: object
    ) -> Optional[str]:
        if not conditions:
            return None
        applied: List[str] = []
        if isinstance(conditions, str):
            cleaned = conditions.strip()
            if cleaned:
                target.conditions.add(cleaned)
                applied.append(cleaned)
        elif isinstance(conditions, Iterable) and not isinstance(conditions, (str, bytes)):
            for condition in conditions:
                if not condition:
                    continue
                cleaned = str(condition).strip()
                if cleaned:
                    target.conditions.add(cleaned)
                    applied.append(cleaned)
        if applied:
            self._sync_combatant_state(target)
            return ", ".join(applied)
        return None

    @staticmethod
    def _choose_monster_action(
        actions: Sequence[Mapping[str, object]]
    ) -> Optional[Mapping[str, object]]:
        if not actions:
            return None
        priority = {"melee": 0, "attack": 0, "ranged": 1, "spell": 2, "save": 3, "auto": 4}
        return min(
            actions,
            key=lambda action: priority.get(str(action.get("type", "attack")).lower(), 5),
        )

    def _resolve_monster_action(
        self,
        state: CombatState,
        monster: CombatantState,
        *,
        session: Optional[DungeonSession] = None,
    ) -> Optional[str]:
        potential_targets = [
            combatant
            for combatant in state.order
            if combatant.is_player and not combatant.is_dead
        ]
        if not potential_targets:
            state.current_action = {
                "actor": monster.name,
                "state": "idle",
                "summary": "Searching for a target",
                "detail": f"{monster.name} finds no adventurers to strike.",
                "emoji": "👹",
                "team": "enemy",
            }
            return None
        conscious_targets = [target for target in potential_targets if target.current_hp > 0]
        if conscious_targets:
            target = random.choice(conscious_targets)
        else:
            target = random.choice(potential_targets)
        actions = self._monster_actions(monster)
        multiattack = self._expand_multiattack_actions(monster, actions)
        target_ac = int(target.metadata.get("armor_class", DEFAULT_PLAYER_ARMOR_CLASS))
        resistances = self._normalise_damage_traits(target.metadata.get("resistances"))
        vulnerabilities = self._normalise_damage_traits(target.metadata.get("vulnerabilities"))
        immunities = self._normalise_damage_traits(target.metadata.get("immunities"))
        message: Optional[str]
        action_payload = {
            "actor": monster.name,
            "state": "monster action",
            "summary": f"Strikes at {target.name}",
            "emoji": "👹",
            "team": "enemy",
        }
        if multiattack:
            action_payload["summary"] = f"Unleashing a flurry against {target.name}"
            message = self._execute_monster_multiattack(
                session,
                monster,
                target,
                multiattack,
                target_ac=target_ac,
                resistances=resistances,
                vulnerabilities=vulnerabilities,
                immunities=immunities,
            )
        else:
            action = self._choose_monster_action(actions)
            action_type = str(action.get("type", "attack")).lower() if action else "attack"
            if action_type == "save":
                message = self._execute_monster_save_action(
                    session,
                    monster,
                    target,
                    action,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
            elif action_type == "auto":
                message = self._execute_monster_auto_action(
                    session,
                    monster,
                    target,
                    action,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
            else:
                message = self._execute_monster_attack_action(
                    session,
                    monster,
                    target,
                    action,
                    target_ac=target_ac,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
            action_name = str(action.get("name", action_type.title())) if action else action_type.title()
            if action_type == "save":
                action_payload["summary"] = f"Forcing {target.name} to resist {action_name}"
            elif action_type == "auto":
                action_payload["summary"] = f"Unleashing {action_name} on {target.name}"
            else:
                action_payload["summary"] = f"Striking {target.name} with {action_name}"
        if not message:
            message = f"{monster.name} hesitates, accomplishing nothing."
        action_payload["detail"] = message
        state.current_action = action_payload
        state.log.append(message)
        self._trim_combat_log(state)
        return message

    def _execute_monster_multiattack(
        self,
        session: Optional[DungeonSession],
        monster: CombatantState,
        target: CombatantState,
        actions: Sequence[Mapping[str, object]],
        *,
        target_ac: int,
        resistances: Iterable[str],
        vulnerabilities: Iterable[str],
        immunities: Iterable[str],
    ) -> str:
        parts: List[str] = []
        for action in actions:
            attack, extra_dice = self._create_monster_attack(monster, action)
            outcome = resolve_attack(
                attack,
                target_ac,
                resistances=resistances,
                vulnerabilities=vulnerabilities,
                immunities=immunities,
            )
            pending_damage = outcome.damage
            if outcome.roll_result.hits and extra_dice:
                damage_type = attack.damage_packets[0].damage_type
                extra_packets = [
                    DamagePacket(
                        amount=self._roll_damage(str(dice)),
                        damage_type=damage_type,
                    )
                    for dice in extra_dice
                ]
                if extra_packets:
                    pending_damage += apply_damage(
                        extra_packets,
                        resistances=resistances,
                        vulnerabilities=vulnerabilities,
                        immunities=immunities,
                    )
            dealt = 0
            if outcome.roll_result.hits and pending_damage > 0:
                was_unconscious = target.current_hp <= 0
                dealt = self._apply_damage_to_combatant(
                    session,
                    target,
                    pending_damage,
                    critical=outcome.roll_result.is_critical_hit or was_unconscious,
                    source="monster",
                )
            if outcome.roll_result.hits:
                text = (
                    f"{attack.name} hits {target.name} for {dealt} damage "
                    f"(Attack {outcome.roll_result.total} vs AC {target_ac})."
                )
                if outcome.roll_result.is_critical_hit:
                    text += " Critical hit!"
            else:
                text = (
                    f"{attack.name} misses {target.name} "
                    f"(Attack {outcome.roll_result.total} vs AC {target_ac})."
                )
            parts.append(text)
            if target.is_dead:
                break
        if not parts:
            summary = f"{monster.name} struggles to land a blow on {target.name}."
        else:
            summary = f"{monster.name} unleashes a flurry on {target.name}: " + " ".join(parts)
            if target.defeated:
                summary += f" {target.name} is defeated!"
        return summary

    def _execute_monster_attack_action(
        self,
        session: Optional[DungeonSession],
        monster: CombatantState,
        target: CombatantState,
        action: Optional[Mapping[str, object]],
        *,
        target_ac: int,
        resistances: Iterable[str],
        vulnerabilities: Iterable[str],
        immunities: Iterable[str],
    ) -> str:
        attack, extra_dice = self._create_monster_attack(monster, action)
        outcome = resolve_attack(
            attack,
            target_ac,
            resistances=resistances,
            vulnerabilities=vulnerabilities,
            immunities=immunities,
        )
        pending_damage = outcome.damage
        if outcome.roll_result.hits and extra_dice:
            damage_type = attack.damage_packets[0].damage_type
            extra_packets = [
                DamagePacket(
                    amount=self._roll_damage(str(dice)),
                    damage_type=damage_type,
                )
                for dice in extra_dice
            ]
            if extra_packets:
                pending_damage += apply_damage(
                    extra_packets,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
        dealt = 0
        if outcome.roll_result.hits and pending_damage > 0:
            was_unconscious = target.current_hp <= 0
            dealt = self._apply_damage_to_combatant(
                session,
                target,
                pending_damage,
                critical=outcome.roll_result.is_critical_hit or was_unconscious,
                source="monster",
            )
        if outcome.roll_result.hits:
            message = (
                f"{monster.name} uses {attack.name}, hitting {target.name} for {dealt} damage "
                f"(Attack {outcome.roll_result.total} vs AC {target_ac})."
            )
            if outcome.roll_result.is_critical_hit:
                message += " Critical hit!"
        else:
            message = (
                f"{monster.name} uses {attack.name}, missing {target.name} "
                f"(Attack {outcome.roll_result.total} vs AC {target_ac})."
            )
        if target.defeated:
            message += f" {target.name} is defeated!"
        return message

    def _execute_monster_auto_action(
        self,
        session: Optional[DungeonSession],
        monster: CombatantState,
        target: CombatantState,
        action: Optional[Mapping[str, object]],
        *,
        resistances: Iterable[str],
        vulnerabilities: Iterable[str],
        immunities: Iterable[str],
    ) -> str:
        action_data: Mapping[str, object] = action or {}
        name = str(action_data.get("name", f"{monster.name}'s assault"))
        damage_expr = str(action_data.get("damage", monster.metadata.get("damage", "1d6+1")))
        damage_type_raw = action_data.get("damage_type", monster.metadata.get("damage_type"))
        damage_type = str(damage_type_raw).lower() if damage_type_raw else None
        base_damage = self._roll_damage(damage_expr)
        packets = (DamagePacket(amount=base_damage, damage_type=damage_type),)
        damage = apply_damage(
            packets,
            resistances=resistances,
            vulnerabilities=vulnerabilities,
            immunities=immunities,
        )
        dealt = 0
        if damage > 0:
            dealt = self._apply_damage_to_combatant(
                session,
                target,
                damage,
                critical=target.current_hp <= 0,
                source="monster",
            )
        condition_text = self._apply_conditions_to_target(target, action_data.get("conditions"))
        if dealt:
            message = (
                f"{monster.name} unleashes {name}, dealing {dealt} damage to {target.name}."
            )
        else:
            message = f"{monster.name} unleashes {name}, but it has no effect on {target.name}."
        if condition_text:
            message += f" {target.name} gains {condition_text}."
        if target.defeated:
            message += f" {target.name} is defeated!"
        return message

    def _execute_monster_save_action(
        self,
        session: Optional[DungeonSession],
        monster: CombatantState,
        target: CombatantState,
        action: Optional[Mapping[str, object]],
        *,
        resistances: Iterable[str],
        vulnerabilities: Iterable[str],
        immunities: Iterable[str],
    ) -> str:
        action_data: Mapping[str, object] = action or {}
        name = str(action_data.get("name", f"{monster.name}'s spell"))
        dc = int(action_data.get("save_dc", monster.metadata.get("save_dc", 10)))
        ability = str(action_data.get("save_ability", monster.metadata.get("save_ability", "DEX"))).upper()
        saving_throws = target.metadata.get("saving_throws")
        if isinstance(saving_throws, Mapping):
            try:
                save_bonus = int(saving_throws.get(ability, 0))
            except (TypeError, ValueError):
                save_bonus = 0
        else:
            save_bonus = 0
        save_result = saving_throw(save_bonus, dc)
        damage_expr = str(action_data.get("damage", monster.metadata.get("damage", "1d6+1")))
        damage_type_raw = action_data.get("damage_type", monster.metadata.get("damage_type"))
        damage_type = str(damage_type_raw).lower() if damage_type_raw else None
        damage_amount = self._roll_damage(damage_expr)
        if save_result.success:
            if action_data.get("half_on_success"):
                damage_amount //= 2
            else:
                damage_amount = 0
        packets = (DamagePacket(amount=damage_amount, damage_type=damage_type),)
        damage = apply_damage(
            packets,
            resistances=resistances,
            vulnerabilities=vulnerabilities,
            immunities=immunities,
        )
        dealt = 0
        if damage > 0:
            dealt = self._apply_damage_to_combatant(
                session,
                target,
                damage,
                critical=target.current_hp <= 0,
                source="monster",
            )
        if save_result.success:
            message = (
                f"{monster.name} uses {name}; {target.name} succeeds on a DC {dc} {ability} save."
            )
            if dealt:
                message += f" {target.name} still takes {dealt} damage."
            condition_text = self._apply_conditions_to_target(
                target, action_data.get("success_conditions")
            )
        else:
            message = (
                f"{monster.name} uses {name}; {target.name} fails the DC {dc} {ability} save and takes {dealt} damage."
            )
            fail_conditions = (
                action_data.get("fail_conditions")
                or action_data.get("conditions_on_fail")
                or action_data.get("conditions")
            )
            condition_text = self._apply_conditions_to_target(target, fail_conditions)
        if condition_text:
            message += f" {target.name} gains {condition_text}."
        if target.defeated:
            message += f" {target.name} is defeated!"
        return message

    @staticmethod
    def _format_damage_expression(damage_die: str, ability_mod: int) -> str:
        if ability_mod > 0:
            return f"{damage_die}+{ability_mod}"
        if ability_mod < 0:
            return f"{damage_die}{ability_mod}"
        return damage_die

    def _calculate_armor_class(
        self, ability_scores: Dict[str, int], equipment_keys: Sequence[str]
    ) -> tuple[int, Optional[str], bool]:
        dex_mod = ability_modifier(int(ability_scores.get("DEX", 10)))
        armor_ac: Optional[int] = None
        armor_key: Optional[str] = None
        for key in equipment_keys:
            definition = ARMOR_DEFINITIONS.get(key)
            if definition is None:
                continue
            dex_bonus: int
            if definition.dex_cap is None:
                dex_bonus = dex_mod
            elif definition.dex_cap == 0:
                dex_bonus = 0
            elif dex_mod >= 0:
                dex_bonus = min(dex_mod, definition.dex_cap)
            else:
                dex_bonus = dex_mod
            total = definition.base_ac + dex_bonus
            if armor_ac is None or total > armor_ac:
                armor_ac = total
                armor_key = key
        if armor_ac is None:
            armor_ac = 10 + dex_mod
        shield_bonus = 0
        shield_equipped = False
        for key in equipment_keys:
            bonus = SHIELD_BONUSES.get(key, 0)
            if bonus:
                shield_equipped = True
                shield_bonus += bonus
        return armor_ac + shield_bonus, armor_key, shield_equipped

    def _weapon_attack_options(
        self,
        character: Character,
        equipment_keys: Sequence[str],
        ability_scores: Dict[str, int],
    ) -> tuple[List[Dict[str, object]], List[str]]:
        proficiencies = {value.lower() for value in character.proficiencies}
        proficiencies.update(value.lower() for value in character.character_class.weapon_proficiencies)
        strength_mod = ability_modifier(int(ability_scores.get("STR", 10)))
        dexterity_mod = ability_modifier(int(ability_scores.get("DEX", 10)))
        options: List[Dict[str, object]] = []
        warnings: List[str] = []
        seen: set[str] = set()
        for key in equipment_keys:
            definition = WEAPON_DEFINITIONS.get(key)
            if definition is None:
                continue
            if key in seen and definition.quantity == 1:
                continue
            seen.add(key)
            ability = "STR"
            ability_mod = strength_mod
            if definition.ranged:
                ability = "DEX"
                ability_mod = dexterity_mod
            elif definition.finesse:
                if dexterity_mod >= strength_mod:
                    ability = "DEX"
                    ability_mod = dexterity_mod
            proficient = any(tag.lower() in proficiencies for tag in definition.categories)
            attack_bonus = ability_mod + (PROFICIENCY_BONUS if proficient else 0)
            damage = self._format_damage_expression(definition.damage_die, ability_mod)
            item = EQUIPMENT.get(key)
            display_name = definition.name or (item.name if item else key.replace("_", " ").title())
            try:
                dice_count_str, dice_sides_str = definition.damage_die.lower().split("d", 1)
                dice_count = int(dice_count_str)
                dice_sides = int(dice_sides_str)
            except (AttributeError, ValueError):
                dice_count = 1
                dice_sides = 4
            average_damage = dice_count * (dice_sides + 1) / 2 + ability_mod
            option: Dict[str, object] = {
                "name": display_name,
                "weapon_key": key,
                "attack_bonus": attack_bonus,
                "damage": damage,
                "damage_die": definition.damage_die,
                "ability": ability,
                "ability_modifier": ability_mod,
                "proficient": proficient,
                "quantity": definition.quantity,
                "average_damage": average_damage,
            }
            options.append(option)
            if not proficient:
                warnings.append(f"Not proficient with {display_name}—attacks will suffer.")
        if not options:
            ability = "STR" if strength_mod >= dexterity_mod else "DEX"
            ability_mod = strength_mod if ability == "STR" else dexterity_mod
            attack_bonus = ability_mod + PROFICIENCY_BONUS
            damage_die = "1d4"
            options.append(
                {
                    "name": "Unarmed Strike",
                    "weapon_key": "unarmed",
                    "attack_bonus": attack_bonus,
                    "damage": self._format_damage_expression(damage_die, ability_mod),
                    "damage_die": damage_die,
                    "ability": ability,
                    "ability_modifier": ability_mod,
                    "proficient": True,
                    "quantity": 1,
                    "average_damage": (4 + 1) / 2 + ability_mod,
                }
            )
            warnings.append("No weapon found—defaulting to an unarmed strike.")
        options.sort(
            key=lambda option: (option["attack_bonus"], option.get("average_damage", 0.0)),
            reverse=True,
        )
        return options, warnings

    def _spellcasting_profile(
        self, character: Character, ability_scores: Dict[str, int]
    ) -> tuple[Optional[Dict[str, object]], List[str]]:
        ability_key = SPELLCASTING_ABILITIES.get(character.character_class.key)
        warnings: List[str] = []
        if ability_key is None:
            return None, warnings
        ability_score = ability_scores.get(ability_key)
        if ability_score is None:
            warnings.append(
                f"Spellcasting ability {ability_key} is missing—spell attacks will be unavailable."
            )
            return None, warnings
        ability_mod = ability_modifier(int(ability_score))
        profile = {
            "ability": ability_key,
            "attack_bonus": ability_mod + PROFICIENCY_BONUS,
            "save_dc": 8 + PROFICIENCY_BONUS + ability_mod,
        }
        return profile, warnings

    def _spell_options_for_character(
        self,
        character: Character,
        ability_scores: Mapping[str, int],
        spellcasting_profile: Optional[Mapping[str, object]],
    ) -> List[Dict[str, object]]:
        if not spellcasting_profile:
            return []
        ability = str(spellcasting_profile.get("ability", "")).upper()
        ability_mod = ability_modifier(int(ability_scores.get(ability, 10))) if ability else 0
        defaults = DEFAULT_SPELL_OPTIONS.get(character.character_class.key, [])
        options: List[Dict[str, object]] = []
        for entry in defaults:
            option = copy.deepcopy(entry)
            option["level"] = int(option.get("level", 0))
            option.setdefault("attack_bonus", spellcasting_profile.get("attack_bonus", ability_mod))
            option.setdefault("save_dc", spellcasting_profile.get("save_dc", 8 + ability_mod))
            option.setdefault("ability_modifier", ability_mod)
            option.setdefault("ability", ability)
            options.append(option)
        return options

    def _feature_action_options(
        self, character: Character, ability_scores: Mapping[str, int]
    ) -> List[Dict[str, object]]:
        options: List[Dict[str, object]] = []
        for feature in character.character_class.features:
            if feature.level > 1:
                continue
            entry: Dict[str, object] = {
                "name": feature.name,
                "description": feature.description,
                "key": feature.name.lower().replace(" ", "_") if feature.name else "feature",
            }
            if "Second Wind" in feature.name:
                entry["effects"] = {"type": "heal", "dice": "1d10+1"}
                entry["resource"] = {
                    "type": "pool",
                    "pool": "feature_uses",
                    "key": feature.name,
                    "amount": 1,
                    "max": 1,
                    "refresh": "short_rest",
                }
            options.append(entry)
        return options

    def _build_character_combat_profile(self, character: Character) -> tuple[Dict[str, object], List[str]]:
        warnings: List[str] = []
        ability_scores = {key: int(value) for key, value in character.ability_scores.values.items()}
        initiative_bonus = ability_modifier(ability_scores.get("DEX", 10))
        constitution_mod = ability_modifier(ability_scores.get("CON", 10))
        hit_die = int(character.character_class.hit_die)
        max_hp = max(1, hit_die + constitution_mod)
        equipment_keys = [entry.lower() for entry in character.equipment]
        armor_class, armor_key, has_shield = self._calculate_armor_class(ability_scores, equipment_keys)
        weapon_options, weapon_warnings = self._weapon_attack_options(
            character, equipment_keys, ability_scores
        )
        warnings.extend(weapon_warnings)
        spellcasting_profile, spell_warnings = self._spellcasting_profile(character, ability_scores)
        warnings.extend(spell_warnings)
        spell_options = self._spell_options_for_character(
            character, ability_scores, spellcasting_profile
        )
        feature_options = self._feature_action_options(character, ability_scores)
        equipment_summary = []
        for key in equipment_keys:
            item = EQUIPMENT.get(key)
            equipment_summary.append(item.name if item else key.replace("_", " ").title())
        resources: Dict[str, object] = {}
        if spellcasting_profile:
            slot_defaults = DEFAULT_SPELL_SLOTS.get(character.character_class.key, {1: 2})
            resources["spell_slots"] = {
                str(level): {"max": amount, "available": amount}
                for level, amount in slot_defaults.items()
            }
        if feature_options:
            feature_pool: Dict[str, Dict[str, int]] = {}
            for option in feature_options:
                resource = option.get("resource")
                if isinstance(resource, Mapping):
                    pool_name = str(resource.get("pool", "feature_uses"))
                    pool = feature_pool.setdefault(pool_name, {})
                    key = str(resource.get("key", option.get("name", "Feature")))
                    max_uses = int(resource.get("max", resource.get("amount", 1) or 1))
                    available = int(resource.get("available", resource.get("amount", max_uses)))
                    pool[key] = {"max": max_uses, "available": available}
            for pool_name, pool in feature_pool.items():
                resources[pool_name] = pool
        metadata: Dict[str, object] = {
            "armor_class": armor_class,
            "initiative_bonus": initiative_bonus,
            "attack_options": weapon_options,
            "default_attack_index": 0,
            "combat_options": {
                "weapons": weapon_options,
                "spells": spell_options,
                "features": feature_options,
            },
            "equipment": equipment_summary,
            "proficiency_bonus": PROFICIENCY_BONUS,
            "features": [option.get("name", "Feature") for option in feature_options],
            "character_name": character.name,
            "character_class": character.character_class.name,
            "race": character.race.name,
            "hit_die": hit_die,
            "max_hp": max_hp,
        }
        if weapon_options:
            metadata["attack_bonus"] = weapon_options[0]["attack_bonus"]
            metadata["damage"] = weapon_options[0]["damage"]
            metadata["weapon_name"] = weapon_options[0]["name"]
        else:
            metadata["attack_bonus"] = DEFAULT_PLAYER_ATTACK_BONUS
            metadata["damage"] = DEFAULT_PLAYER_DAMAGE
        if spellcasting_profile:
            metadata["spellcasting"] = spellcasting_profile
            metadata["spell_attack_bonus"] = spellcasting_profile["attack_bonus"]
            metadata["spell_save_dc"] = spellcasting_profile["save_dc"]
        metadata["resources"] = resources
        if armor_key:
            armor_item = EQUIPMENT.get(armor_key)
            metadata["armor"] = {
                "key": armor_key,
                "name": armor_item.name if armor_item else armor_key.replace("_", " ").title(),
            }
        metadata["shield"] = has_shield
        metadata["warnings"] = warnings
        profile = {
            "max_hp": max_hp,
            "initiative_bonus": initiative_bonus,
            "armor_class": armor_class,
            "metadata": metadata,
        }
        return profile, warnings

    async def _run_automatic_turns(
        self, session: DungeonSession, state: CombatState
    ) -> None:
        if not state.active:
            return
        current = self._ensure_current_combatant(state)
        if current is None:
            self._finish_combat(session, state, victory=False)
            await self._refresh_session_view(session)
            return
        while state.active:
            state.current_action = None
            current = state.current_combatant()
            if current is None:
                break
            if current.is_player and current.current_hp <= 0:
                handled = self._handle_player_zero_hp_turn(state, current, session=session)
                if handled:
                    newly_fallen = self._identify_newly_fallen(session, state)
                    for fallen in newly_fallen:
                        await self._announce_player_death(session, fallen)
                    self._evaluate_combat_state(session, state)
                    await self._refresh_session_view(session)
                    if not state.active:
                        break
                    next_combatant = state.advance_turn()
                    if next_combatant is None:
                        break
                    continue
                await self._refresh_session_view(session)
                break
            if current.defeated:
                next_combatant = self._ensure_current_combatant(state)
                if next_combatant is None:
                    break
                continue
            if current.is_player:
                state.waiting_for = current.user_id
                state.current_action = {
                    "actor": current.name,
                    "state": "awaiting action",
                    "summary": "Awaiting their next move...",
                    "emoji": "🎲",
                    "team": "player",
                }
                await self._refresh_session_view(session)
                break
            state.waiting_for = None
            state.current_action = {
                "actor": current.name,
                "state": "thinking",
                "summary": "Plotting their next move...",
                "emoji": "🤔",
                "team": "enemy",
            }
            thinking_entry = "Enemy is thinking..."
            state.log.append(thinking_entry)
            self._trim_combat_log(state)
            await self._refresh_session_view(session)
            await asyncio.sleep(random.uniform(*MONSTER_THINKING_DELAY_RANGE))
            if state.log and state.log[-1] == thinking_entry:
                state.log.pop()
            self._resolve_monster_action(state, current, session=session)
            newly_fallen = self._identify_newly_fallen(session, state)
            for fallen in newly_fallen:
                await self._announce_player_death(session, fallen)
            self._evaluate_combat_state(session, state)
            await self._refresh_session_view(session)
            await asyncio.sleep(random.uniform(*MONSTER_ACTION_PAUSE_RANGE))
            if not state.active:
                break
            next_combatant = state.advance_turn()
            if next_combatant is None:
                break
        if not state.active:
            state.waiting_for = None
            state.current_action = None
            await self._refresh_session_view(session)

    def _schedule_automatic_turns(
        self, session: DungeonSession, state: Optional[CombatState]
    ) -> None:
        if state is None or not state.active:
            return
        result = self._run_automatic_turns(session, state)
        if asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
            task.add_done_callback(self._handle_automatic_turn_completion)

    @staticmethod
    def _handle_automatic_turn_completion(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:  # pragma: no cover - background task logging
            log.exception("Automatic combat turn task failed")

    def _player_weapon_attack(
        self,
        session: DungeonSession,
        state: CombatState,
        player: CombatantState,
        selection: Optional[str],
    ) -> str:
        target = self._select_player_target(state, player)
        if target is None:
            return "There are no foes left to strike."
        attack_bonus = int(player.metadata.get("attack_bonus", DEFAULT_PLAYER_ATTACK_BONUS))
        damage_expr = str(player.metadata.get("damage", DEFAULT_PLAYER_DAMAGE))
        weapon_label = str(player.metadata.get("weapon_name", "weapon"))
        combat_options = player.metadata.get("combat_options", {})
        options = combat_options.get("weapons") if isinstance(combat_options, Mapping) else None
        option: Optional[Mapping[str, object]] = None
        if not options:
            options = player.metadata.get("attack_options")
        if isinstance(options, list) and options:
            default_index = self._resolve_selection_index(
                player.metadata.get("default_attack_index", 0), "weapon", 0
            )
            option_index = self._resolve_selection_index(selection, "weapon", default_index)
            if option_index < 0 or option_index >= len(options):
                option_index = 0
            option = options[option_index]
            player.metadata["default_attack_index"] = option_index
            attack_bonus = int(option.get("attack_bonus", attack_bonus))
            damage_expr = str(option.get("damage", damage_expr))
            weapon_label = str(option.get("name", weapon_label))
        target_ac = int(target.metadata.get("armor_class", 10))
        if weapon_label.lower() == "weapon":
            action_summary = f"Attacking {target.name}"
        else:
            action_summary = f"Striking {target.name} with {weapon_label}"
        action_payload = {
            "actor": player.name,
            "state": "weapon attack",
            "summary": action_summary,
            "emoji": "⚔️",
            "team": "player",
        }
        result = attack_roll(attack_bonus, target_ac)
        if result.hits:
            extra_dice = option.get("critical_extra_dice") if isinstance(option, Mapping) else None
            if isinstance(extra_dice, Sequence):
                extra_dice_values: Optional[Sequence[str]] = [str(value) for value in extra_dice]
            else:
                extra_dice_values = None
            damage = self._roll_damage(
                damage_expr,
                critical=result.is_critical_hit,
                extra_dice=extra_dice_values,
            )
            dealt = self._apply_damage_to_combatant(
                session, target, damage, critical=result.is_critical_hit
            )
            weapon_text = "" if weapon_label.lower() == "weapon" else f" with your {weapon_label}"
            summary = (
                f"You hit {target.name}{weapon_text} for {dealt} damage! "
                f"(Attack {result.total} vs AC {target_ac})"
            )
            if result.is_critical_hit:
                summary += " Critical hit!"
            log_entry = (
                f"{player.name} hits {target.name}{weapon_text} for {dealt} damage. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
            if result.is_critical_hit:
                log_entry += " Critical hit!"
            if target.defeated:
                log_entry += f" {target.name} is defeated!"
        else:
            weapon_text = "" if weapon_label.lower() == "weapon" else f" with your {weapon_label}"
            summary = (
                f"Your attack{weapon_text} misses {target.name}. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
            log_entry = (
                f"{player.name}'s attack{weapon_text} misses {target.name}. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
        action_payload["detail"] = summary
        state.current_action = action_payload
        state.log.append(log_entry)
        self._trim_combat_log(state)
        self._evaluate_combat_state(session, state)
        return summary

    def _player_cast_spell(
        self,
        session: DungeonSession,
        state: CombatState,
        player: CombatantState,
        selection: Optional[str],
    ) -> str:
        combat_options = player.metadata.get("combat_options", {})
        spells = combat_options.get("spells") if isinstance(combat_options, Mapping) else None
        if not isinstance(spells, list) or not spells:
            return "You have no spells prepared."
        spell_index = self._resolve_selection_index(selection, "spell", 0)
        if spell_index < 0 or spell_index >= len(spells):
            return "That spell isn't available right now."
        spell = spells[spell_index]
        spell_name = str(spell.get("name", "Spell"))
        effect_type = str(spell.get("type", "attack")).lower()
        target_required = effect_type in {"attack", "auto", "save", "damage"}
        if target_required:
            target = self._select_player_target(state, player)
            if target is None:
                return "There are no valid targets for that spell."
        else:
            target = self._select_player_target(state, player, update=False)
        requirement = spell.get("consumes")
        can_use, failure_reason = self._consume_resource(player, requirement)
        if not can_use:
            return failure_reason or f"You cannot cast {spell_name} right now."
        attack_bonus = int(
            spell.get(
                "attack_bonus",
                player.metadata.get("spell_attack_bonus", player.metadata.get("attack_bonus", 0)),
            )
        )
        damage_expr = str(spell.get("damage", DEFAULT_PLAYER_DAMAGE))
        damage_type = spell.get("damage_type")
        extra_dice = spell.get("critical_extra_dice") if isinstance(spell, Mapping) else None
        if isinstance(extra_dice, Sequence):
            critical_dice: Optional[Sequence[str]] = [str(value) for value in extra_dice]
        else:
            critical_dice = None
        log_entry: str
        summary: str
        target_label = target.name if target is not None else "the area"
        if target_required and target is not None:
            action_summary = f"Casting {spell_name} at {target_label}"
        else:
            action_summary = f"Channeling {spell_name}"
        action_payload = {
            "actor": player.name,
            "state": "spell",
            "summary": action_summary,
            "emoji": "✨",
            "team": "player",
        }
        if effect_type == "attack" and target is not None:
            target_ac = int(target.metadata.get("armor_class", DEFAULT_PLAYER_ARMOR_CLASS))
            result = attack_roll(attack_bonus, target_ac)
            if result.hits:
                damage = self._roll_damage(
                    damage_expr,
                    critical=result.is_critical_hit,
                    extra_dice=critical_dice,
                )
                dealt = self._apply_damage_to_combatant(
                    session,
                    target,
                    damage,
                    critical=result.is_critical_hit,
                )
                type_text = f" {str(damage_type).title()}" if damage_type else ""
                summary = (
                    f"You cast {spell_name}, striking {target.name} for {dealt}{type_text} damage. "
                    f"(Attack {result.total} vs AC {target_ac})"
                )
                log_entry = (
                    f"{player.name} casts {spell_name}, hitting {target.name} for {dealt}{type_text} damage. "
                    f"(Attack {result.total} vs AC {target_ac})"
                )
                if result.is_critical_hit:
                    summary += " Critical hit!"
                    log_entry += " Critical hit!"
                if target.defeated:
                    log_entry += f" {target.name} is defeated!"
            else:
                summary = (
                    f"Your {spell_name} misses {target.name}. "
                    f"(Attack {result.total} vs AC {target_ac})"
                )
                log_entry = (
                    f"{player.name}'s {spell_name} misses {target.name}. "
                    f"(Attack {result.total} vs AC {target_ac})"
                )
        elif effect_type == "auto" and target is not None:
            damage = self._roll_damage(damage_expr, extra_dice=critical_dice)
            dealt = self._apply_damage_to_combatant(session, target, damage)
            type_text = f" {str(damage_type).title()}" if damage_type else ""
            summary = f"You unleash {spell_name}, automatically dealing {dealt}{type_text} damage to {target.name}."
            log_entry = (
                f"{player.name} casts {spell_name}, dealing {dealt}{type_text} damage to {target.name}."
            )
            if target.defeated:
                log_entry += f" {target.name} is defeated!"
        elif effect_type == "save" and target is not None:
            dc = int(spell.get("save_dc", player.metadata.get("spell_save_dc", 10)))
            ability = str(spell.get("save_ability", "DEX")).upper()
            saving_throws = target.metadata.get("saving_throws")
            if isinstance(saving_throws, Mapping):
                try:
                    save_bonus = int(saving_throws.get(ability, 0))
                except (TypeError, ValueError):
                    save_bonus = 0
            else:
                save_bonus = 0
            save_result = saving_throw(save_bonus, dc)
            damage = self._roll_damage(damage_expr, extra_dice=critical_dice)
            if save_result.success:
                if spell.get("half_on_success"):
                    damage //= 2
                    dealt = self._apply_damage_to_combatant(session, target, damage)
                    summary = (
                        f"{target.name} resists some of your {spell_name}, taking {dealt} damage after succeeding "
                        f"the save (DC {dc})."
                    )
                    log_entry = (
                        f"{player.name} casts {spell_name}; {target.name} succeeds on the save (DC {dc}) and takes {dealt} damage."
                    )
                else:
                    summary = (
                        f"{target.name} shrugs off your {spell_name}, succeeding on the saving throw (DC {dc})."
                    )
                    log_entry = (
                        f"{player.name} casts {spell_name}, but {target.name} succeeds on the save (DC {dc})."
                    )
                    damage = 0
            else:
                dealt = self._apply_damage_to_combatant(session, target, damage)
                summary = (
                    f"{target.name} fails the save (DC {dc}) against your {spell_name}, taking {dealt} damage."
                )
                log_entry = (
                    f"{player.name}'s {spell_name} forces {target.name} to fail the save (DC {dc}), taking {dealt} damage."
                )
                if target.defeated:
                    log_entry += f" {target.name} is defeated!"
        else:
            summary = f"You focus your energies with {spell_name}, but nothing notable happens."
            log_entry = f"{player.name} casts {spell_name}, but it has no immediate effect."
        if spell.get("concentration"):
            player.concentration = spell_name
            self._sync_combatant_state(player, session)
            summary += " You begin concentrating on the spell."
            log_entry += f" {player.name} begins concentrating on {spell_name}."
        status_text = self._resource_status_text(player, requirement)
        if status_text:
            summary += f" {status_text}"
        self._sync_combatant_state(player, session)
        action_payload["detail"] = summary
        state.current_action = action_payload
        state.log.append(log_entry)
        self._trim_combat_log(state)
        self._evaluate_combat_state(session, state)
        return summary

    def _player_use_feature(
        self,
        session: DungeonSession,
        state: CombatState,
        player: CombatantState,
        selection: Optional[str],
    ) -> str:
        combat_options = player.metadata.get("combat_options", {})
        features = combat_options.get("features") if isinstance(combat_options, Mapping) else None
        if not isinstance(features, list) or not features:
            return "You have no combat features to use."
        feature_index = self._resolve_selection_index(selection, "feature", 0)
        if feature_index < 0 or feature_index >= len(features):
            return "That feature is not available right now."
        feature = features[feature_index]
        feature_name = str(feature.get("name", "Feature"))
        requirement = feature.get("resource") if isinstance(feature, Mapping) else None
        can_use, failure_reason = self._consume_resource(player, requirement if isinstance(requirement, Mapping) else None)
        if not can_use:
            return failure_reason or f"You cannot use {feature_name} right now."
        effect = feature.get("effects") if isinstance(feature, Mapping) else None
        log_entry = f"{player.name} uses {feature_name}."
        summary = f"You activate {feature_name}."
        action_payload = {
            "actor": player.name,
            "state": "feature",
            "summary": f"Activating {feature_name}",
            "emoji": "🎖️",
            "team": "player",
        }
        if isinstance(effect, Mapping):
            effect_type = str(effect.get("type", "")).lower()
            if effect_type == "heal":
                heal_expr = str(effect.get("dice", "1d6"))
                healed = self._roll_damage(heal_expr)
                restored = self._apply_healing_to_combatant(session, player, healed)
                summary = f"You use {feature_name}, regaining {restored} HP."
                log_entry = f"{player.name} uses {feature_name}, regaining {restored} HP."
            elif effect_type == "condition":
                condition = str(effect.get("condition", "")).strip()
                target_scope = str(effect.get("target", "self")).lower()
                if condition:
                    if target_scope == "self":
                        player.conditions.add(condition)
                        self._sync_combatant_state(player, session)
                        summary = f"{feature_name} grants you {condition}."
                        log_entry = f"{player.name} uses {feature_name}, gaining {condition}."
        status_text = self._resource_status_text(player, requirement if isinstance(requirement, Mapping) else None)
        if status_text:
            summary += f" {status_text}"
        self._sync_combatant_state(player, session)
        action_payload["detail"] = summary
        state.current_action = action_payload
        state.log.append(log_entry)
        self._trim_combat_log(state)
        return summary

    def _player_defend(self, state: CombatState, player: CombatantState) -> str:
        message = f"{player.name} takes a defensive stance, ready for the next assault."
        state.current_action = {
            "actor": player.name,
            "state": "defend",
            "summary": "Taking a defensive stance",
            "detail": "You brace yourself, gaining no additional effects but readying for the next turn.",
            "emoji": "🛡️",
            "team": "player",
        }
        state.log.append(message)
        self._trim_combat_log(state)
        return "You brace yourself, gaining no additional effects but readying for the next turn."

    def _player_roll_death_save(
        self,
        session: Optional[DungeonSession],
        state: CombatState,
        player: CombatantState,
    ) -> str:
        if player.current_hp > 0:
            return "You are still conscious—you don't need a death save."
        if player.is_dead:
            return f"{player.name} has already succumbed to their wounds."
        if player.stable:
            return f"{player.name} is stable and does not need to roll."
        message = self._resolve_player_death_save(session, player)
        state.current_action = {
            "actor": player.name,
            "state": "death save",
            "summary": "Rolling a death save",
            "detail": message,
            "emoji": "🩸",
            "team": "player",
        }
        state.log.append(message)
        self._trim_combat_log(state)
        return message

    async def _build_combat_state(
        self,
        interaction: discord.Interaction,
        session: DungeonSession,
        party_order: Optional[Sequence[int]] = None,
    ) -> CombatState:
        session.fallen_players.clear()
        combatants: List[CombatantState] = []
        warnings_log: List[str] = []
        guild_id = interaction.guild_id
        party_ids = list(party_order) if party_order is not None else sorted(session.party_ids)
        for user_id in party_ids:
            roll = random.randint(1, 20)
            name = self._display_name_for_user(user_id, interaction=interaction)
            metadata: Dict[str, object]
            initiative_bonus = 0
            max_hp = DEFAULT_PLAYER_HP
            armor_class = DEFAULT_PLAYER_ARMOR_CLASS
            warnings: List[str] = []
            profile: Optional[Dict[str, object]] = None
            character: Optional[Character] = None
            if guild_id is not None:
                try:
                    character = await self.characters.get(guild_id, user_id)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("Failed to load character for user %s", user_id, exc_info=exc)
                    warnings.append("Character data could not be loaded—using default combat profile.")
            else:
                warnings.append("Characters are unavailable outside of guilds—using default combat profile.")
            if character is not None:
                try:
                    profile, profile_warnings = self._build_character_combat_profile(character)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("Failed to derive combat stats for %s", character, exc_info=exc)
                    profile = None
                    profile_warnings = ["Character data invalid—using default combat profile."]
                warnings.extend(profile_warnings)
            if profile:
                initiative_bonus = int(profile.get("initiative_bonus", 0))
                max_hp = int(profile.get("max_hp", DEFAULT_PLAYER_HP))
                armor_class = int(profile.get("armor_class", DEFAULT_PLAYER_ARMOR_CLASS))
                metadata = dict(profile.get("metadata", {}))
            else:
                metadata = {
                    "armor_class": DEFAULT_PLAYER_ARMOR_CLASS,
                    "initiative_bonus": 0,
                    "attack_options": [
                        {
                            "name": "Fallback Strike",
                            "weapon_key": "fallback",
                            "attack_bonus": DEFAULT_PLAYER_ATTACK_BONUS,
                            "damage": DEFAULT_PLAYER_DAMAGE,
                            "damage_die": "1d8",
                            "ability": "STR",
                            "ability_modifier": ability_modifier(16),
                            "proficient": True,
                            "quantity": 1,
                            "average_damage": ((8 + 1) / 2) + ability_modifier(16),
                        }
                    ],
                    "default_attack_index": 0,
                    "combat_options": {
                        "weapons": [],
                        "spells": [],
                        "features": [],
                    },
                    "proficiency_bonus": PROFICIENCY_BONUS,
                    "features": [],
                    "weapon_name": "Fallback Strike",
                    "attack_bonus": DEFAULT_PLAYER_ATTACK_BONUS,
                    "damage": DEFAULT_PLAYER_DAMAGE,
                    "character_name": name,
                    "max_hp": DEFAULT_PLAYER_HP,
                    "resources": {},
                }
                metadata["combat_options"]["weapons"] = metadata["attack_options"]
                warnings.append("Using default combat profile.")
            metadata.setdefault("armor_class", armor_class)
            metadata.setdefault("initiative_bonus", initiative_bonus)
            metadata.setdefault("attack_options", [])
            metadata.setdefault("default_attack_index", 0)
            metadata.setdefault("weapon_name", metadata.get("weapon_name", "weapon"))
            metadata.setdefault("attack_bonus", DEFAULT_PLAYER_ATTACK_BONUS)
            metadata.setdefault("damage", DEFAULT_PLAYER_DAMAGE)
            metadata.setdefault("combat_options", {
                "weapons": metadata.get("attack_options", []),
                "spells": metadata.get("combat_options", {}).get("spells", []),
                "features": metadata.get("features", []),
            })
            metadata.setdefault("resources", {})
            existing_warnings = list(metadata.get("warnings", []))
            existing_warnings.extend(warnings)
            metadata["warnings"] = list(dict.fromkeys(existing_warnings))
            metadata["armor_class"] = int(metadata.get("armor_class", armor_class))
            armor_class = int(metadata["armor_class"])
            metadata["initiative_bonus"] = int(metadata.get("initiative_bonus", initiative_bonus))
            initiative_bonus = int(metadata["initiative_bonus"])
            metadata["max_hp"] = max_hp
            metadata["character_loaded"] = character is not None and profile is not None
            metadata["user_id"] = user_id
            if character is not None:
                metadata["character_id"] = character.user_id
            if metadata["warnings"]:
                for warning in metadata["warnings"]:
                    warnings_log.append(f"{name}: {warning}")
            initiative_total = roll + int(metadata.get("initiative_bonus", 0))
            conditions_raw = metadata.get("conditions") or []
            if isinstance(conditions_raw, (list, tuple, set)):
                conditions = {str(value) for value in conditions_raw if str(value).strip()}
            else:
                conditions = set()
            concentration_raw = metadata.get("concentration")
            concentration = str(concentration_raw) if concentration_raw else None
            resources_payload = metadata.get("resources")
            if isinstance(resources_payload, MutableMapping):
                shared_resources = copy.deepcopy(resources_payload)
            else:
                shared_resources = {}
            metadata["resources"] = shared_resources
            stored_health = session.party_health.get(user_id)
            if isinstance(stored_health, Mapping):
                try:
                    stored_max_hp = int(stored_health.get("max", max_hp))
                except (TypeError, ValueError):
                    stored_max_hp = max_hp
                try:
                    stored_current_hp = int(stored_health.get("current", max_hp))
                except (TypeError, ValueError):
                    stored_current_hp = max_hp
            else:
                stored_max_hp = max_hp
                stored_current_hp = max_hp
            max_hp_value = max_hp if max_hp > 0 else DEFAULT_PLAYER_HP
            if stored_max_hp > 0:
                max_hp_value = max(max_hp_value, stored_max_hp)
            current_hp_value = max(0, min(max_hp_value, stored_current_hp))
            metadata["max_hp"] = max_hp_value
            combatant = CombatantState(
                identifier=f"player:{user_id}",
                name=name,
                initiative_roll=roll,
                initiative_total=initiative_total,
                max_hp=max_hp_value,
                current_hp=current_hp_value,
                is_player=True,
                user_id=user_id,
                metadata=metadata,
                conditions=conditions,
                concentration=concentration,
                resources=shared_resources,
            )
            self._sync_combatant_state(combatant, session)
            combatants.append(combatant)
        monster_labels = self._unique_monster_labels(session.room.encounter.monsters)
        for index, (monster, display_name) in enumerate(
            zip(session.room.encounter.monsters, monster_labels)
        ):
            roll = random.randint(1, 20)
            initiative_total = roll
            dex_score = monster.ability_scores.get("DEX") if monster.ability_scores else None
            if dex_score is not None:
                initiative_total += (int(dex_score) - 10) // 2
            monster_resources: Dict[str, object] = {}
            monster_combatant = CombatantState(
                identifier=f"monster:{index}",
                name=display_name,
                initiative_roll=roll,
                initiative_total=initiative_total,
                max_hp=monster.hit_points,
                current_hp=monster.hit_points,
                is_player=False,
                metadata={
                    "armor_class": monster.armor_class,
                    "attack_bonus": monster.attack_bonus,
                    "damage": monster.damage,
                },
                resources=monster_resources,
            )
            self._sync_combatant_state(monster_combatant)
            combatants.append(monster_combatant)
        combatants.sort(key=lambda combatant: (combatant.initiative_total, combatant.initiative_roll), reverse=True)
        state = CombatState(order=combatants)
        if warnings_log:
            state.log.extend(warnings_log)
        if combatants:
            order_summary = ", ".join(
                f"{combatant.name} ({combatant.initiative_total})" for combatant in combatants
            )
            state.log.append(f"Initiative order: {order_summary}")
            self._trim_combat_log(state)
        self._ensure_current_combatant(state)
        return state

    def _build_room_embed(
        self, interaction: Optional[discord.Interaction], session: DungeonSession
    ) -> discord.Embed:
        room = session.room
        dungeon = session.dungeon
        self._ensure_room_trap_state(session, room)
        self._ensure_room_discovery_state(session, room)
        trap_catalog = session.trap_catalog.get(room.id, {})
        trap_states = session.trap_states.get(room.id, {})
        discovered_traps = session.discovered_traps.get(room.id, set())
        trap_detected = any(
            trap_states.get(trap.key, "hidden") != "hidden" or trap.key in discovered_traps
            for trap in room.encounter.traps
        )

        embed = discord.Embed(
            title=f"{dungeon.name} — Room {room.id + 1}: {room.name}",
            description=room.description,
            color=discord.Color.dark_purple(),
        )
        encounter_summary = room.encounter.summary or "Quiet for now."
        if room.encounter.traps and not trap_detected:
            encounter_summary = "Quiet for now."
        embed.add_field(name="Encounter", value=encounter_summary, inline=False)

        if room.encounter.monsters:
            monster_labels = self._unique_monster_labels(room.encounter.monsters)
            monsters = "\n".join(
                f"• {label} (AC {monster.armor_class}, HP {monster.hit_points})"
                for monster, label in zip(room.encounter.monsters, monster_labels)
            )
            embed.add_field(name="Monsters", value=monsters, inline=False)

        if trap_catalog:
            trap_lines: list[str] = []
            ordered_keys = [trap.key for trap in room.encounter.traps]
            for key in trap_catalog.keys():
                if key not in ordered_keys:
                    ordered_keys.append(key)
            for key in ordered_keys:
                trap = trap_catalog.get(key)
                if trap is None:
                    continue
                status = trap_states.get(key, "hidden")
                if status == "hidden" and key not in discovered_traps:
                    continue
                saving_throw_data = trap.saving_throw or {}
                dc = saving_throw_data.get("dc")
                ability = saving_throw_data.get("ability")
                if dc and ability:
                    detail = f"DC {dc} {ability} save"
                else:
                    detail = "Hidden hazard"
                suffix = ""
                if status == "discovered":
                    suffix = " — detected"
                elif status == "disarmed":
                    suffix = " — disarmed"
                elif status == "sprung":
                    suffix = " — sprung"
                trap_lines.append(f"• {trap.name} ({detail}){suffix}")
            if trap_lines:
                embed.add_field(name="Traps", value="\n".join(trap_lines), inline=False)

        discovered_loot = session.discovered_loot.get(room.id, set())
        if room.encounter.loot:
            loot_lines = [
                f"• {item.name} ({item.rarity})"
                for item in room.encounter.loot
                if item.key in discovered_loot
            ]
            if loot_lines:
                embed.add_field(name="Loot", value="\n".join(loot_lines), inline=False)

        approach_lines: list[str] = []
        if session.last_travel_note:
            approach_lines.append(session.last_travel_note)
        travel = session.travel_description()
        if travel:
            approach_lines.append(travel)
        if approach_lines:
            embed.add_field(name="Approach", value="\n".join(approach_lines), inline=False)

        embed.add_field(
            name="Party", value=self._party_display(interaction, session), inline=False
        )

        if room.encounter.monsters or session.stealthed:
            combat = session.combat_state
            if combat and combat.active:
                status_text = "Spotted — combat is underway."
            elif session.stealthed:
                status_text = "Hidden — the party has not been noticed."
            else:
                status_text = "Spotted — nearby creatures are aware of the party."
            embed.add_field(name="Stealth Status", value=status_text, inline=False)

        exit_lines: list[str] = []
        discovered_exits = session.discovered_exits.get(room.id, set())
        visited_rooms = set(session.breadcrumbs)
        previous_room = session.breadcrumbs[-2] if len(session.breadcrumbs) >= 2 else None
        for exit_option in room.exits:
            if exit_option.key not in discovered_exits:
                continue
            if getattr(exit_option, "completes_delve", False):
                status = "Leave the dungeon and return to the tavern"
                exit_lines.append(f"• {exit_option.label} — {status}")
                continue
            destination_id = exit_option.destination
            if destination_id is None:
                exit_lines.append(
                    f"• {exit_option.label} — Destination unknown"
                )
                continue
            try:
                destination_room = dungeon.get_room(destination_id)
            except KeyError:
                continue
            status: str
            if destination_id == previous_room:
                status = f"Backtrack to Room {destination_room.id + 1}: {destination_room.name}"
            elif destination_id in visited_rooms:
                status = f"Visited Room {destination_room.id + 1}: {destination_room.name}"
            else:
                status = "Unexplored passage"
            exit_lines.append(f"• {exit_option.label} — {status}")
        if exit_lines:
            embed.add_field(name="Exits", value="\n".join(exit_lines), inline=False)
        else:
            embed.add_field(name="Exits", value="No obvious exits are visible.", inline=False)

        if session.breadcrumbs:
            path_lines: list[str] = []
            for index, room_id in enumerate(session.breadcrumbs):
                try:
                    breadcrumb_room = dungeon.get_room(room_id)
                except KeyError:
                    continue
                label = f"Room {breadcrumb_room.id + 1}: {breadcrumb_room.name}"
                if index == 0:
                    path_lines.append(label)
                else:
                    direction = (
                        session.exit_history[index - 1]
                        if index - 1 < len(session.exit_history)
                        else "Unknown path"
                    )
                    path_lines.append(f"↳ {direction} → {label}")
            if path_lines:
                embed.add_field(name="Path Taken", value="\n".join(path_lines), inline=False)

        combat = session.combat_state
        actions: list[str]
        if combat and combat.active:
            actions = ["Stand your ground and resolve the battle using the combat controls."]
        else:
            actions = []
            if room.exits:
                actions.append("Choose an exit to continue the expedition or retrace your steps.")
            else:
                actions.append("Search the chamber for hidden exits or wait for rescue.")
            if room.encounter.monsters:
                actions.append("Engage the monsters in combat.")
            if self._room_has_discovered_traps(session, room):
                actions.append("Attempt to disarm the exposed traps.")
            else:
                actions.append("Survey the chamber for hidden dangers.")
            if room.encounter.loot:
                actions.append("Search the chamber for hidden loot.")
            if not actions:
                actions.append("Take a moment to catch your breath.")
        embed.add_field(name="Available Actions", value="\n".join(f"• {line}" for line in actions), inline=False)

        footer_parts = [f"Theme: {dungeon.theme.name}"]
        footer_parts.append(
            f"Difficulty: {dungeon.difficulty.title() if getattr(dungeon, 'difficulty', None) else 'Standard'}"
        )
        if dungeon.seed is not None:
            footer_parts.append(f"Seed: {dungeon.seed}")
        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    def _resolve_room_positions(self, dungeon: Dungeon) -> Dict[int, tuple[int, int]]:
        raw_positions = getattr(dungeon, "room_positions", None) or {}
        room_position_values = [getattr(room, "position", None) for room in dungeon.rooms]
        use_room_attributes = any(
            value not in (None, (0, 0)) for value in room_position_values
        )
        positions: Dict[int, tuple[int, int]] = {}
        occupied: Set[tuple[int, int]] = set()

        def normalise(value: object) -> Optional[tuple[int, int]]:
            if isinstance(value, tuple) and len(value) == 2:
                candidate = value
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                candidate = tuple(value[:2])  # type: ignore[index]
                if len(candidate) != 2:
                    return None
            else:
                return None
            try:
                x = int(candidate[0])
                y = int(candidate[1])
            except (TypeError, ValueError):
                return None
            return (x, y)

        for room in dungeon.rooms:
            raw = raw_positions.get(room.id)
            if raw is None and use_room_attributes:
                raw = getattr(room, "position", None)
            normalised = normalise(raw)
            if normalised is not None:
                positions[room.id] = normalised
                occupied.add(normalised)

        adjacency: Dict[int, Set[int]] = {room.id: set() for room in dungeon.rooms}
        for corridor in getattr(dungeon, "corridors", ()):
            adjacency.setdefault(corridor.from_room, set()).add(corridor.to_room)
            adjacency.setdefault(corridor.to_room, set()).add(corridor.from_room)

        unplaced: Set[int] = {
            room.id for room in dungeon.rooms if room.id not in positions
        }

        directions: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))

        def search_ring(origin: tuple[int, int]) -> tuple[int, int]:
            radius = 0
            while True:
                for dy in range(-radius, radius + 1):
                    for dx in range(-radius, radius + 1):
                        if abs(dx) + abs(dy) != radius:
                            continue
                        candidate = (origin[0] + dx, origin[1] + dy)
                        if candidate not in occupied:
                            return candidate
                radius += 1

        def allocate_neighbor(anchor_id: int, target_id: int) -> tuple[int, int]:
            anchor_position = positions[anchor_id]
            for dx, dy in directions:
                candidate = (anchor_position[0] + dx, anchor_position[1] + dy)
                if candidate not in occupied:
                    return candidate

            assigned_neighbors = [
                positions[neighbor]
                for neighbor in sorted(adjacency.get(target_id, ()))
                if neighbor in positions and neighbor != anchor_id
            ]
            for neighbor_pos in assigned_neighbors:
                for dx, dy in directions:
                    candidate = (neighbor_pos[0] + dx, neighbor_pos[1] + dy)
                    if candidate not in occupied:
                        return candidate

            return search_ring(anchor_position)

        queue: deque[int] = deque(sorted(positions.keys()))
        if not queue and dungeon.rooms:
            start_id = dungeon.rooms[0].id
            start_pos = (0, 0)
            positions[start_id] = start_pos
            occupied.add(start_pos)
            queue.append(start_id)
            unplaced.discard(start_id)

        processed: Set[int] = set()
        while queue or unplaced:
            if not queue:
                orphan_id = min(unplaced)
                origin = (0, 0)
                start = search_ring(origin)
                positions[orphan_id] = start
                occupied.add(start)
                queue.append(orphan_id)
                unplaced.remove(orphan_id)
                continue

            current = queue.popleft()
            if current in processed:
                continue
            processed.add(current)

            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in positions:
                    if neighbor not in processed:
                        queue.append(neighbor)
                    continue
                if current not in positions:
                    continue
                assigned = allocate_neighbor(current, neighbor)
                positions[neighbor] = assigned
                occupied.add(assigned)
                queue.append(neighbor)
                unplaced.discard(neighbor)

        return positions

    def _build_map_string(self, session: DungeonSession) -> str:
        dungeon = session.dungeon
        positions = self._resolve_room_positions(dungeon)

        if not positions:
            return "(no rooms)"

        xs = [coord[0] for coord in positions.values()]
        ys = [coord[1] for coord in positions.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        width = max_x - min_x + 1
        height = max_y - min_y + 1

        interior_width = 4
        box_height = 3
        tile_width = interior_width + 4  # corridor padding on both sides
        tile_height = box_height + 2  # corridor padding above and below

        map_width = width * tile_width
        map_height = height * tile_height

        grid: list[list[str]] = [[" " for _ in range(map_width)] for _ in range(map_height)]
        blocked: Set[tuple[int, int]] = set()

        def write_room_char(x: int, y: int, char: str) -> None:
            if not (0 <= x < map_width and 0 <= y < map_height):
                return
            grid[y][x] = char
            blocked.add((x, y))

        def place_char(x: int, y: int, char: str) -> None:
            if not (0 <= x < map_width and 0 <= y < map_height):
                return
            if char == " ":
                return
            existing = grid[y][x]
            if existing == char:
                return
            if (x, y) in blocked:
                if char == "+":
                    grid[y][x] = "+"
                elif {existing, char} <= {"-", "|"}:
                    grid[y][x] = "+"
                return
            if existing == " ":
                grid[y][x] = char
                return
            if char == "+" or existing == "+":
                grid[y][x] = "+"
                return
            if {existing, char} <= {"-", "|"} or (existing in "-|" and char in "-|"):
                grid[y][x] = "+"
                return
            # Avoid overwriting room labels and borders with corridor characters.

        room_draw_info: Dict[int, Dict[str, int]] = {}
        for room_id, (room_x, room_y) in positions.items():
            x_offset = (room_x - min_x) * tile_width
            y_offset = (max_y - room_y) * tile_height

            left_border = x_offset + 1
            right_border = x_offset + tile_width - 2
            top_border = y_offset + 1
            bottom_border = y_offset + tile_height - 2
            label_row = top_border + 1

            room_draw_info[room_id] = {
                "x_offset": x_offset,
                "y_offset": y_offset,
                "left_border": left_border,
                "right_border": right_border,
                "top_border": top_border,
                "bottom_border": bottom_border,
                "label_row": label_row,
                "center_x": left_border + (right_border - left_border) // 2,
                "center_y": label_row,
                "left_corridor": x_offset,
                "right_corridor": x_offset + tile_width - 1,
                "top_corridor": y_offset,
                "bottom_corridor": y_offset + tile_height - 1,
            }

            # Top border
            write_room_char(left_border, top_border, "+")
            for x in range(left_border + 1, right_border):
                write_room_char(x, top_border, "-")
            write_room_char(right_border, top_border, "+")

            # Bottom border
            write_room_char(left_border, bottom_border, "+")
            for x in range(left_border + 1, right_border):
                write_room_char(x, bottom_border, "-")
            write_room_char(right_border, bottom_border, "+")

            # Label row with borders
            write_room_char(left_border, label_row, "|")
            write_room_char(right_border, label_row, "|")

            label = f"{room_id + 1:02d}"
            label_text = f"[{label}]" if room_id == session.current_room else label
            padded = label_text.center(interior_width)
            for index, char in enumerate(padded):
                write_room_char(left_border + 1 + index, label_row, char)

        corridors = getattr(dungeon, "corridors", ())

        def connect_room_border(room_id: int, direction: str) -> None:
            info = room_draw_info[room_id]
            if direction == "left":
                place_char(info["left_border"], info["label_row"], "+")
                blocked.add((info["left_border"], info["label_row"]))
            elif direction == "right":
                place_char(info["right_border"], info["label_row"], "+")
                blocked.add((info["right_border"], info["label_row"]))
            elif direction == "up":
                place_char(info["center_x"], info["top_border"], "+")
                blocked.add((info["center_x"], info["top_border"]))
            elif direction == "down":
                place_char(info["center_x"], info["bottom_border"], "+")
                blocked.add((info["center_x"], info["bottom_border"]))

        def find_path(start: tuple[int, int], end: tuple[int, int]) -> Optional[list[tuple[int, int]]]:
            if start == end:
                return [start]
            queue: deque[tuple[int, int]] = deque([start])
            came_from: Dict[tuple[int, int], Optional[tuple[int, int]]] = {start: None}
            while queue:
                current = queue.popleft()
                if current == end:
                    break
                cx, cy = current
                for dx_step, dy_step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx_step, cy + dy_step
                    if not (0 <= nx < map_width and 0 <= ny < map_height):
                        continue
                    neighbor = (nx, ny)
                    if neighbor in came_from or neighbor in blocked:
                        continue
                    queue.append(neighbor)
                    came_from[neighbor] = current
            else:
                return None

            path: list[tuple[int, int]] = []
            node: Optional[tuple[int, int]] = end
            while node is not None:
                path.append(node)
                node = came_from.get(node)
            path.reverse()
            return path

        for corridor in corridors:
            first = corridor.from_room
            second = corridor.to_room
            if first not in room_draw_info or second not in room_draw_info:
                continue

            first_info = room_draw_info[first]
            second_info = room_draw_info[second]
            dx = positions[second][0] - positions[first][0]
            dy = positions[second][1] - positions[first][1]

            if dx == 0 and dy == 0:
                continue

            if abs(dx) >= abs(dy):
                if dx >= 0:
                    start = (first_info["right_corridor"], first_info["center_y"])
                    end = (second_info["left_corridor"], second_info["center_y"])
                    connect_room_border(first, "right")
                    connect_room_border(second, "left")
                else:
                    start = (first_info["left_corridor"], first_info["center_y"])
                    end = (second_info["right_corridor"], second_info["center_y"])
                    connect_room_border(first, "left")
                    connect_room_border(second, "right")
            else:
                if dy >= 0:
                    start = (first_info["center_x"], first_info["top_corridor"])
                    end = (second_info["center_x"], second_info["bottom_corridor"])
                    connect_room_border(first, "up")
                    connect_room_border(second, "down")
                else:
                    start = (first_info["center_x"], first_info["bottom_corridor"])
                    end = (second_info["center_x"], second_info["top_corridor"])
                    connect_room_border(first, "down")
                    connect_room_border(second, "up")

            path = find_path(start, end)
            if not path:
                continue

            for index, (x, y) in enumerate(path):
                prev_point = path[index - 1] if index > 0 else None
                next_point = path[index + 1] if index + 1 < len(path) else None
                horizontal = False
                vertical = False
                if prev_point:
                    if prev_point[0] != x:
                        horizontal = True
                    if prev_point[1] != y:
                        vertical = True
                if next_point:
                    if next_point[0] != x:
                        horizontal = True
                    if next_point[1] != y:
                        vertical = True
                if horizontal and vertical:
                    char = "+"
                elif horizontal:
                    char = "-"
                elif vertical:
                    char = "|"
                else:
                    char = "-"
                place_char(x, y, char)

        lines = ["".join(row).rstrip() for row in grid]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        return "\n".join(lines) if lines else "(no rooms)"

    def _build_map_image(self, session: DungeonSession) -> BytesIO:
        dungeon = session.dungeon
        positions = self._resolve_room_positions(dungeon)
        if not positions:
            raise ValueError("No rooms available to render the dungeon map")

        image = render_dungeon_map(
            rooms=dungeon.rooms,
            corridors=getattr(dungeon, "corridors", ()),
            positions=positions,
            current_room=session.current_room,
        )
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    def _build_session_embeds(
        self, session: DungeonSession, *, interaction: Optional[discord.Interaction] = None
    ) -> SessionEmbedPayload:
        map_string = self._build_map_string(session)
        map_embed = discord.Embed(title="Dungeon Map")

        files: List[discord.File] = []
        try:
            image_buffer = self._build_map_image(session)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "Pillow" in str(exc):
                log.warning("Dungeon map image unavailable: %s", exc)
            else:
                log.exception("Failed to render dungeon map image", exc_info=True)
        else:
            files.append(discord.File(image_buffer, filename="dungeon_map.png"))
            map_embed.set_image(url="attachment://dungeon_map.png")

        if not files:
            map_embed.description = f"```\n{map_string}\n```"

        embeds: List[discord.Embed] = [map_embed]
        room_embed = self._build_room_embed(interaction, session)
        embeds.append(room_embed)

        combat_embed = self._build_combat_embed(session)
        if combat_embed is not None:
            embeds.append(combat_embed)
        return SessionEmbedPayload(embeds=embeds, files=files)

    def _build_combat_embed(self, session: DungeonSession) -> Optional[discord.Embed]:
        combat = session.combat_state
        if combat is None or not combat.active:
            return None

        room = session.room
        description = room.encounter.summary or "A pitched battle erupts!"
        embed = discord.Embed(
            title=f"Combat — Room {room.id + 1}",
            description=description,
            color=discord.Color.dark_red(),
        )

        initiative_lines: list[str] = []
        for index, combatant in enumerate(combat.order):
            if combatant.defeated:
                status = "Defeated"
            else:
                status_parts: List[str] = [f"{combatant.current_hp}/{combatant.max_hp} HP"]
                if combatant.conditions:
                    status_parts.append(
                        "Conditions: " + ", ".join(sorted(combatant.conditions))
                    )
                if combatant.concentration:
                    status_parts.append(f"Concentration: {combatant.concentration}")
                if combatant.death_save_successes or combatant.death_save_failures:
                    status_parts.append(
                        "Death Saves "
                        f"S{combatant.death_save_successes}/F{combatant.death_save_failures}"
                    )
                resource_text = self._summarise_combatant_resources(combatant)
                if resource_text:
                    status_parts.append(resource_text)
                status = " | ".join(status_parts)
            turn_marker = "➡️ " if index == combat.turn_index and not combatant.defeated else ""
            initiative_lines.append(
                f"{turn_marker}{combatant.name} — Init {combatant.initiative_total} "
                f"(Roll {combatant.initiative_roll}) — {status}"
            )
        if initiative_lines:
            embed.add_field(name="Initiative Order", value="\n".join(initiative_lines), inline=False)

        current = combat.current_combatant()
        if current is not None and not current.defeated:
            turn_text = f"Round {combat.round_number}: {current.name} is acting."
        else:
            turn_text = f"Round {combat.round_number}: resolving initiative..."
        embed.add_field(name="Current Turn", value=turn_text, inline=False)

        current_action = combat.current_action or {}
        if current_action:
            team = current_action.get("team")
            if team == "enemy":
                section_label = "Enemy Turn"
            elif team == "player":
                section_label = "Player Action"
            else:
                section_label = "Current Action"
            emoji = current_action.get("emoji") or ("👹" if team == "enemy" else "🎲")
            field_name = f"{emoji} {section_label}".strip()
            actor = current_action.get("actor") or "Unknown"
            summary_line = current_action.get("summary") or "Taking action..."
            detail = current_action.get("detail")
            state_label = current_action.get("state")
            value_lines: List[str] = [f"**{actor}** — {summary_line}"]
            if state_label:
                formatted_state = state_label.replace("_", " ").title()
                value_lines.append(f"*{formatted_state}*")
            if detail and detail != summary_line:
                value_lines.append(str(detail))
            action_value = "\n".join(value_lines)
            if len(action_value) > 1024:
                action_value = action_value[:1021] + "..."
            embed.add_field(name=field_name, value=action_value or "Taking action...", inline=False)

        if combat.waiting_for is not None:
            waiting = next(
                (c for c in combat.order if c.user_id == combat.waiting_for),
                None,
            )
            if waiting is not None:
                waiting_text = f"Awaiting action from {waiting.name}."
            else:
                waiting_text = "Awaiting action from a party member."
            embed.add_field(name="Awaiting", value=waiting_text, inline=False)

        if combat.log:
            log_entries = combat.log[-MAX_COMBAT_LOG_ENTRIES:]
            while log_entries and len("\n".join(log_entries)) > 1024:
                log_entries = log_entries[1:]
            log_text = "\n".join(log_entries) if log_entries else "(log truncated)"
            embed.add_field(name="Combat Log", value=log_text or "No events yet.", inline=False)

        return embed

    def _build_navigation_view(self, session: DungeonSession) -> discord.ui.View:
        combat = session.combat_state
        if combat and combat.active:
            return CombatActionView(self, session)
        room = session.room
        return DungeonNavigationView(
            self,
            session,
            disable_search=not bool(room.encounter.loot),
            disable_disarm=False,
            disable_engage=not bool(room.encounter.monsters),
        )

    async def _refresh_session_view(self, session: DungeonSession) -> None:
        if session.message_id is None:
            return
        channel = self.bot.get_channel(session.channel_id)
        if channel is None:
            return
        partial_getter = getattr(channel, "get_partial_message", None)
        if callable(partial_getter):
            message = partial_getter(session.message_id)
        else:
            try:
                message = await channel.fetch_message(session.message_id)
            except (discord.HTTPException, AttributeError):
                return
        payload = self._build_session_embeds(session)
        view = self._build_navigation_view(session)
        try:
            await message.edit(
                embeds=payload.embeds,
                view=view,
                attachments=payload.files or [],
            )
        except discord.HTTPException:
            return
        self.bot.add_view(view, message_id=session.message_id)

    async def _refresh_session_message(self, interaction: discord.Interaction, session: DungeonSession) -> None:
        if session.message_id is None:
            return
        payload = self._build_session_embeds(session, interaction=interaction)
        view = self._build_navigation_view(session)
        try:
            await interaction.followup.edit_message(
                message_id=session.message_id,
                embeds=payload.embeds,
                view=view,
                attachments=payload.files or [],
            )
            self.bot.add_view(view, message_id=session.message_id)
        except discord.HTTPException:
            pass

    async def _find_tavern_channel(
        self, guild_id: Optional[int]
    ) -> Optional[discord.TextChannel]:
        if guild_id is None:
            return None
        tavern_cog = self._get_tavern_cog()
        if tavern_cog is None:
            return None
        try:
            config = await tavern_cog.config_store.get_config(guild_id)
        except Exception:
            return None
        if config is None:
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel_id = config.tavern_channel_id or config.channel_id
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _find_manage_channel(
        self, guild_id: Optional[int]
    ) -> Optional[discord.TextChannel]:
        if guild_id is None:
            return None
        tavern_cog = self._get_tavern_cog()
        if tavern_cog is None:
            return None
        try:
            config = await tavern_cog.config_store.get_config(guild_id)
        except Exception:
            return None
        if config is None:
            return None
        if config.manage_channel_id is None:
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(config.manage_channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _format_duration(started_at: datetime) -> str:
        now = datetime.now(timezone.utc)
        elapsed = now - started_at
        total_seconds = int(max(0, elapsed.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours}h")
        if minutes or hours:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    def _build_completion_embed(
        self, session: DungeonSession, *, exit_label: Optional[str]
    ) -> discord.Embed:
        title = f"Delve Complete — {session.dungeon.name}"
        if exit_label:
            description = (
                f"The party emerges through the {exit_label.lower()} and returns to the tavern."
            )
        else:
            description = "The party returns to the tavern victorious."
        embed = discord.Embed(colour=discord.Colour.green(), title=title, description=description)

        party_ids = tuple(sorted(session.party_ids))
        if party_ids:
            adventurers = "\n".join(f"• <@{user_id}>" for user_id in party_ids)
        else:
            adventurers = "• Unknown adventurers"
        embed.add_field(name="Adventurers", value=adventurers, inline=False)

        rooms_explored = len(set(session.breadcrumbs)) or 1
        summary_lines = [
            f"Rooms explored: {rooms_explored}",
            f"Monsters defeated: {session.monsters_defeated}",
            f"Traps disarmed: {session.traps_disarmed}",
        ]
        if session.traps_triggered:
            summary_lines.append(f"Traps sprung: {session.traps_triggered}")
        embed.add_field(name="Summary", value="\n".join(summary_lines), inline=False)

        if session.treasure_items_claimed or session.treasure_gold_claimed:
            treasure_lines: list[str] = []
            if session.treasure_items_claimed:
                treasure_lines.append(
                    f"Items recovered: {session.treasure_items_claimed}"
                )
            if session.treasure_gold_claimed:
                treasure_lines.append(
                    f"Gold secured: {session.treasure_gold_claimed}"
                )
            treasure_value = "\n".join(treasure_lines)
        else:
            treasure_value = "No treasure recovered."
        embed.add_field(name="Treasure", value=treasure_value, inline=False)

        duration_text = self._format_duration(session.started_at)
        embed.add_field(name="Duration", value=duration_text, inline=False)
        return embed

    def _build_party_failure_embed(self, session: DungeonSession) -> discord.Embed:
        embed = discord.Embed(
            colour=discord.Colour.dark_red(),
            title=f"Party Fallen — {session.dungeon.name}",
            description=(
                f"The heroes were slain within {session.dungeon.name}. "
                "No tales of victory escaped the depths."
            ),
        )

        room = session.room
        embed.add_field(
            name="Final Stand",
            value=f"Room {room.id + 1}: {room.name}",
            inline=False,
        )

        party_ids = tuple(sorted(session.party_ids))
        if party_ids:
            adventurers = "\n".join(f"• <@{user_id}>" for user_id in party_ids)
        else:
            adventurers = "• Unknown adventurers"
        embed.add_field(name="Adventurers", value=adventurers, inline=False)

        rooms_explored = len(set(session.breadcrumbs)) or 1
        summary_lines = [
            f"Rooms explored: {rooms_explored}",
            f"Monsters defeated: {session.monsters_defeated}",
            f"Traps disarmed: {session.traps_disarmed}",
        ]
        if session.traps_triggered:
            summary_lines.append(f"Traps sprung: {session.traps_triggered}")
        embed.add_field(name="Summary", value="\n".join(summary_lines), inline=False)

        if session.treasure_items_claimed or session.treasure_gold_claimed:
            treasure_lines: list[str] = []
            if session.treasure_items_claimed:
                treasure_lines.append(
                    f"Items recovered: {session.treasure_items_claimed}"
                )
            if session.treasure_gold_claimed:
                treasure_lines.append(
                    f"Gold secured: {session.treasure_gold_claimed}"
                )
            treasure_value = "\n".join(treasure_lines)
        else:
            treasure_value = "No treasure recovered."
        embed.add_field(name="Treasure", value=treasure_value, inline=False)

        duration_text = self._format_duration(session.started_at)
        embed.add_field(name="Duration", value=duration_text, inline=False)
        embed.set_footer(text="Raise a glass in the tavern for the fallen party.")
        return embed

    async def _handle_delve_completion(
        self,
        interaction: discord.Interaction,
        session: DungeonSession,
        *,
        exit_label: Optional[str],
        party_snapshot: tuple[int, ...],
    ) -> None:
        key = self._session_key(session.guild_id, session.channel_id)
        removed = await self.sessions.pop(key)
        if removed is None:
            removed = session
        if party_snapshot:
            removed.party_ids.update(party_snapshot)

        guild = interaction.guild or (
            self.bot.get_guild(removed.guild_id) if removed.guild_id is not None else None
        )
        party_channel: Optional[discord.TextChannel] = None
        if isinstance(interaction.channel, discord.TextChannel):
            party_channel = interaction.channel
        elif guild is not None:
            channel = guild.get_channel(removed.channel_id)
            if isinstance(channel, discord.TextChannel):
                party_channel = channel

        announcement_channel = await self._find_tavern_channel(removed.guild_id)
        if announcement_channel is not None:
            tavern_reference = announcement_channel.mention
        else:
            tavern_reference = "the tavern"

        exit_phrase = exit_label.lower() if exit_label else "exit"
        await interaction.followup.send(
            (
                f"You step through the {exit_phrase} and return to {tavern_reference}. "
                "Your success is heralded for all to hear!"
            ),
            ephemeral=True,
        )

        if removed.message_id is not None:
            try:
                await interaction.followup.edit_message(
                    message_id=removed.message_id,
                    view=None,
                )
            except discord.HTTPException:
                pass

        embed = self._build_completion_embed(removed, exit_label=exit_label)
        targets: list[discord.TextChannel] = []
        if announcement_channel is not None:
            targets.append(announcement_channel)
        elif party_channel is not None:
            targets.append(party_channel)
        delivered_channels: set[int] = set()
        triumph_text = f"The party returns triumphant from {removed.dungeon.name}!"
        for target in targets:
            if target.id in delivered_channels:
                continue
            try:
                await target.send(content=triumph_text, embed=embed)
            except discord.HTTPException:
                continue
            delivered_channels.add(target.id)

        asyncio.create_task(self._run_delayed_party_cleanup(removed))
        await self._update_tavern_access(removed.guild_id)
    async def _send_ephemeral_message(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _start_prepared_dungeon(
        self,
        interaction: discord.Interaction,
        stored: StoredDungeon,
        *,
        party_members: Optional[Iterable[int]] = None,
    ) -> bool:
        if interaction.guild_id is None:
            await self._send_ephemeral_message(
                interaction,
                "Prepared dungeons can only be started from within a guild channel.",
            )
            return False

        guild = interaction.guild
        if guild is None:
            await self._send_ephemeral_message(
                interaction,
                "Dungeon parties can only be started within a guild channel.",
            )
            return False

        if not await self._ensure_character_available(interaction):
            return False

        try:
            party_channel = await self._ensure_party_channel(guild, dungeon_name=stored.name)
        except discord.Forbidden:
            await self._send_ephemeral_message(
                interaction,
                "I do not have permission to create or manage party channels.",
            )
            return False
        except RuntimeError:
            await self._send_ephemeral_message(
                interaction,
                "Unable to find an available party channel for this delve.",
            )
            return False

        key = self._session_key(interaction.guild_id, party_channel.id)
        existing = await self.sessions.get(key)
        if existing is not None:
            await self._send_ephemeral_message(
                interaction,
                f"A party is already delving in {party_channel.mention}.",
            )
            return False

        me = guild.me
        if me is not None:
            permissions = party_channel.permissions_for(me)
            if not permissions.send_messages or not permissions.view_channel:
                await self._send_ephemeral_message(
                    interaction,
                    f"I cannot send messages in {party_channel.mention}. Please adjust permissions and try again.",
                )
                return False

        try:
            theme = self.theme_registry.get(stored.theme)
        except KeyError:
            await self._send_ephemeral_message(
                interaction,
                "The theme for this dungeon is no longer available. Ask an administrator to prepare it again.",
            )
            return False

        room_count = stored.room_count if stored.room_count and stored.room_count > 0 else 5
        generator = DungeonGenerator(
            theme,
            seed=stored.seed,
            difficulty=stored.difficulty or "standard",
        )
        dungeon = generator.generate(
            room_count=room_count,
            name=stored.name,
            difficulty=stored.difficulty or "standard",
        )
        initial_party_ids = set(party_members or ())
        initial_party_ids.add(interaction.user.id)

        session = DungeonSession(
            dungeon=dungeon,
            guild_id=interaction.guild_id,
            channel_id=party_channel.id,
            seed=stored.seed,
        )
        session.party_ids.update(initial_party_ids)
        await self.sessions.set(key, session)

        await self._sync_party_channel_access(session)

        payload = self._build_session_embeds(session, interaction=interaction)
        view = self._build_navigation_view(session)
        try:
            message = await party_channel.send(
                embeds=payload.embeds,
                files=payload.files or None,
                view=view,
            )
        except discord.HTTPException as exc:
            removed = await self.sessions.pop(key)
            if removed is not None:
                await self._clear_party_channel_access(removed)
            await self._send_ephemeral_message(
                interaction,
                f"I couldn't start the expedition in {party_channel.mention}: {exc}.",
            )
            return False

        await self.sessions.update(key, lambda run: setattr(run, "message_id", message.id))
        self.bot.add_view(view, message_id=message.id)

        await self._update_tavern_access(interaction.guild_id)

        await self.metadata_store.record_session(
            interaction.guild_id,
            theme=theme.key,
            seed=stored.seed,
            difficulty=dungeon.difficulty,
            name=dungeon.name,
            room_count=room_count,
        )
        await self._send_ephemeral_message(
            interaction,
            f"The party gathers in {party_channel.mention}!",
        )
        return True

    # ---- Slash commands --------------------------------------------------
    @dungeon_group.command(name="prepare", description="Generate and store a dungeon expedition for later exploration.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        theme="Name of the dungeon theme to use",
        size="How large the dungeon should be (in rooms)",
        difficulty="Challenge level for encounters",
        name="Custom name for the dungeon",
        seed="Optional RNG seed",
    )
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def prepare(
        self,
        interaction: discord.Interaction,
        theme: Optional[str] = None,
        size: app_commands.Range[int, 1, 20] = 5,
        difficulty: str = DEFAULT_DIFFICULTY,
        name: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Prepared dungeons can only be created inside a guild.",
                ephemeral=True,
            )
            return

        if not self.theme_registry.values():
            message = "No dungeon themes are available. Please add files under data/<category>/ and reload."
            if self._content_error is not None:
                message += f" Last load error: {self._content_error}."
            await interaction.response.send_message(message, ephemeral=True)
            return

        try:
            theme_obj = await self._resolve_theme(theme, interaction.guild_id)
        except KeyError:
            available = ", ".join(sorted(t.name for t in self.theme_registry.values()))
            await interaction.response.send_message(
                f"Unknown theme '{theme}'. Available themes: {available}.",
                ephemeral=True,
            )
            return
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if seed is None:
            seed = random.randint(0, 999999)
        difficulty_key = (difficulty or DEFAULT_DIFFICULTY).lower()
        if difficulty_key not in DIFFICULTY_PROFILES:
            difficulty_key = DEFAULT_DIFFICULTY

        generator = DungeonGenerator(theme_obj, seed=seed, difficulty=difficulty_key)
        dungeon = generator.generate(
            room_count=int(size), name=name, difficulty=difficulty_key
        )
        await self.metadata_store.record_session(
            interaction.guild_id,
            theme=theme_obj.key,
            seed=seed,
            difficulty=difficulty_key,
            name=dungeon.name,
            room_count=int(size),
        )

        details = [f"Theme: {theme_obj.name}"]
        details.append(f"Rooms: {int(size)}")
        if difficulty_key:
            details.append(
                f"Difficulty: {_format_difficulty_label(difficulty_key)}"
            )
        details.append(f"Seed: {seed}")
        await interaction.response.send_message(
            (
                f"Prepared the dungeon **{dungeon.name}** for adventurers.\n"
                + "\n".join(details)
                + "\nParties can begin this expedition with /dungeon start."
            ),
            ephemeral=True,
        )

    @prepare.autocomplete("theme")
    async def theme_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> Iterable[app_commands.Choice[str]]:
        names = [theme.name for theme in self.theme_registry.values()]
        filtered = [name for name in names if current.lower() in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in filtered]

    @dungeon_group.command(name="start", description="Begin a prepared dungeon expedition in this channel.")
    @app_commands.describe(name="Name of the stored dungeon to explore")
    async def start(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Stored dungeons can only be started within a guild channel.",
                ephemeral=True,
            )
            return

        stored = await self.metadata_store.get_dungeon(interaction.guild_id, name)
        if stored is None:
            names = await self.metadata_store.list_dungeon_names(interaction.guild_id)
            if names:
                available = ", ".join(names)
                message = (
                    f"No stored dungeon named '{name}'. Available expeditions: {available}."
                )
            else:
                message = "No prepared dungeons are available. Ask an administrator to use /dungeon prepare."
            await interaction.response.send_message(message, ephemeral=True)
            return

        await self._start_prepared_dungeon(interaction, stored)

    @start.autocomplete("name")
    async def start_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> Iterable[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        names = await self.metadata_store.list_dungeon_names(interaction.guild_id)
        filtered = [candidate for candidate in names if current.lower() in candidate.lower()][:25]
        return [app_commands.Choice(name=candidate, value=candidate) for candidate in filtered]

    @dungeon_group.command(name="reset", description="Reset the active dungeon session in this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.sessions.pop(key)
        if session is None:
            await interaction.response.send_message("There is no active dungeon in this channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._clear_party_channel_access(session)
        if session.message_id is not None:
            try:
                await interaction.followup.edit_message(message_id=session.message_id, view=None)
            except discord.HTTPException:
                pass
        await interaction.followup.send("The dungeon session has been reset.", ephemeral=True)
        if interaction.guild_id is not None:
            await self._update_tavern_access(interaction.guild_id)

    @dungeon_group.command(name="delete", description="Delete a stored dungeon by name.")
    @app_commands.describe(name="Name of the stored dungeon to delete")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delete_dungeon(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Stored dungeons can only be managed within a guild.",
                ephemeral=True,
            )
            return

        stored = await self.metadata_store.get_dungeon(interaction.guild_id, name)
        if stored is None:
            names = await self.metadata_store.list_dungeon_names(interaction.guild_id)
            if not names:
                message = "There are no stored dungeons for this guild."
            else:
                available = ", ".join(names)
                message = (
                    f"No stored dungeon named '{name}'. Available dungeons: {available}."
                )
            await interaction.response.send_message(message, ephemeral=True)
            return

        summary_lines = [
            f"Are you sure you want to delete the stored dungeon **{stored.name}**?",
            f"Theme: {stored.theme}",
        ]
        if stored.difficulty:
            summary_lines.append(f"Difficulty: {stored.difficulty.title()}")
        if stored.seed is not None:
            summary_lines.append(f"Seed: {stored.seed}")
        if stored.room_count:
            summary_lines.append(f"Rooms: {stored.room_count}")

        view = DungeonDeleteConfirmation(
            self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild_id,
            dungeon=stored,
        )
        await interaction.response.send_message(
            "\n".join(summary_lines),
            view=view,
            ephemeral=True,
        )

    @delete_dungeon.autocomplete("name")
    async def delete_dungeon_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> Iterable[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        names = await self.metadata_store.list_dungeon_names(interaction.guild_id)
        filtered = [candidate for candidate in names if current.lower() in candidate.lower()][:25]
        return [app_commands.Choice(name=candidate, value=candidate) for candidate in filtered]

    @dungeon_group.command(name="reload", description="Reload dungeon content from disk.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reload_content(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            self._load_content(silent=False)
        except ContentLoadError as exc:
            await interaction.followup.send(f"Failed to reload dungeon content: {exc}", ephemeral=True)
            return
        self._guild_theme_cache.clear()
        await interaction.followup.send("Dungeon content reloaded.", ephemeral=True)

    @dungeon_group.command(name="theme", description="Configure the default dungeon theme for this guild.")
    @app_commands.describe(name="Theme name to use as default", clear="Clear the configured default theme")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def configure_theme(
        self,
        interaction: discord.Interaction,
        name: Optional[str] = None,
        clear: bool = False,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Default themes can only be configured inside a guild.",
                ephemeral=True,
            )
            return

        if clear:
            await self.metadata_store.set_default_theme(interaction.guild_id, None)
            self._guild_theme_cache.pop(interaction.guild_id, None)
            await interaction.response.send_message("Cleared the default dungeon theme.", ephemeral=True)
            return

        if name is None:
            await interaction.response.send_message(
                "Please provide a theme name or enable the clear option.",
                ephemeral=True,
            )
            return

        try:
            theme = self.theme_registry.get(name)
        except KeyError:
            available = ", ".join(sorted(t.name for t in self.theme_registry.values())) or "None"
            await interaction.response.send_message(
                f"Unknown theme '{name}'. Available themes: {available}.",
                ephemeral=True,
            )
            return

        await self.metadata_store.set_default_theme(interaction.guild_id, theme.key)
        self._guild_theme_cache[interaction.guild_id] = theme.key
        await interaction.response.send_message(
            f"Default dungeon theme set to {theme.name}.",
            ephemeral=True,
        )

    # ---- Interaction handlers -------------------------------------------
    async def handle_exit(self, interaction: discord.Interaction, exit_key: str) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.sessions.get(key)
        if session is None:
            await interaction.response.send_message("No active dungeon for this party.", ephemeral=True)
            return

        if session.combat_state and session.combat_state.active:
            await interaction.response.send_message(
                "Combat is raging—you can't leave the room until it ends!",
                ephemeral=True,
            )
            return

        if interaction.user.id not in session.party_ids:
            if not await self._ensure_character_available(interaction):
                return

        await interaction.response.defer()
        moved = False
        backtracked = False
        added_member = False
        exit_label: Optional[str] = None
        destination_room: Optional[Room] = None
        party_snapshot: tuple[int, ...] = ()
        triggered_events: list[tuple[int, Trap, str]] = []
        avoidance_messages: list[str] = []
        completed_delve = False

        def mutate(run: DungeonSession) -> None:
            nonlocal moved, backtracked, added_member, exit_label, destination_room
            nonlocal party_snapshot, triggered_events, avoidance_messages, completed_delve
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True

            current_room = run.room
            self._ensure_room_damage_entry(run, current_room.id)
            self._ensure_room_trap_state(run, current_room)
            self._ensure_room_discovery_state(run, current_room)
            catalog = run.trap_catalog.setdefault(current_room.id, {})
            states = run.trap_states.setdefault(current_room.id, {})
            traps = list(current_room.encounter.traps)
            traps_changed = False
            for trap in list(traps):
                status = states.get(trap.key, "hidden")
                if status in ("disarmed", "sprung"):
                    continue
                saving_throw_data = trap.saving_throw or {}
                ability_value = saving_throw_data.get("ability", "DEX")
                ability = str(ability_value).upper()
                dc_raw = saving_throw_data.get("dc", 15)
                try:
                    dc_value = int(dc_raw)
                except (TypeError, ValueError):
                    dc_value = 15
                result = saving_throw(save_bonus=5, dc=dc_value)
                catalog.setdefault(trap.key, trap)
                if result.success:
                    ability_name = ABILITY_NAME_OVERRIDES.get(ability, ability.title())
                    if status == "hidden":
                        avoidance_messages.append(
                            (
                                "A hidden hazard nearly springs, but the party slips past it "
                                f"({ability_name} save {result.total} vs DC {dc_value})."
                            )
                        )
                    else:
                        avoidance_messages.append(
                            (
                                f"The party skirts the {trap.name} safely "
                                f"({ability_name} save {result.total} vs DC {dc_value})."
                            )
                        )
                    continue
                triggered_events.append((current_room.id, trap, "ignored"))
                self._set_trap_status(run, current_room.id, trap.key, "sprung")
                traps.remove(trap)
                traps_changed = True
            if traps_changed:
                run.room.encounter = replace(
                    current_room.encounter, traps=tuple(traps)
                )
            selected_exit = next((option for option in current_room.exits if option.key == exit_key), None)
            if selected_exit is None:
                return

            origin_room_id = run.current_room
            if getattr(selected_exit, "completes_delve", False):
                run.discovered_exits.setdefault(origin_room_id, set()).add(selected_exit.key)
                run.exit_history.append(selected_exit.label)
                run.last_exit_taken = selected_exit.key
                run.last_travel_description = selected_exit.description
                lower_label = selected_exit.label.lower()
                run.last_travel_note = f"The party leaves via the {lower_label}."
                run.stealthed = False
                party_snapshot = tuple(sorted(run.party_ids))
                exit_label = selected_exit.label
                moved = True
                completed_delve = True
                return

            destination_id = selected_exit.destination
            if destination_id is None or destination_id == origin_room_id:
                return

            run.discovered_exits.setdefault(origin_room_id, set()).add(selected_exit.key)

            try:
                destination_room_local = run.dungeon.get_room(destination_id)
            except KeyError:
                return

            corridor = next(
                (
                    link
                    for link in run.dungeon.corridors
                    if {link.from_room, link.to_room} == {origin_room_id, destination_id}
                ),
                None,
            )

            previous_room = run.breadcrumbs[-2] if len(run.breadcrumbs) >= 2 else None
            if previous_room == destination_id:
                if run.breadcrumbs:
                    run.breadcrumbs.pop()
                if run.exit_history:
                    run.exit_history.pop()
                backtracked = True
            else:
                run.breadcrumbs.append(destination_id)
                run.exit_history.append(selected_exit.label)
                backtracked = False

            run.current_room = destination_id
            self._ensure_room_damage_entry(run, destination_id)
            run.last_exit_taken = selected_exit.key
            run.last_travel_description = corridor.description if corridor else None
            exit_label = selected_exit.label
            lower_label = selected_exit.label.lower()
            if backtracked:
                run.last_travel_note = f"The party backtracks through the {lower_label}."
            else:
                run.last_travel_note = f"The party takes the {lower_label}."
            run.stealthed = False
            party_snapshot = tuple(sorted(run.party_ids))
            destination_room = destination_room_local
            self._ensure_room_trap_state(run, destination_room_local)
            self._ensure_room_discovery_state(run, destination_room_local)
            dest_known = run.discovered_exits.setdefault(destination_id, set())
            for option in destination_room_local.exits:
                if option.destination == origin_room_id:
                    dest_known.add(option.key)
            moved = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.followup.send("No active dungeon for this party.", ephemeral=True)
            return

        if completed_delve:
            await self._handle_delve_completion(
                interaction,
                session,
                exit_label=exit_label,
                party_snapshot=party_snapshot,
            )
            return

        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)

        if not moved:
            await interaction.followup.send(
                "That passage isn't accessible right now. Try another direction.",
                ephemeral=True,
            )
            return

        stealth_summary: Optional[str] = None
        started_combat = False
        trigger_monster_turns = False
        monsters_present = bool(destination_room and destination_room.encounter.monsters)
        combat_active = bool(session.combat_state and session.combat_state.active)
        if (
            monsters_present
            and not combat_active
            and party_snapshot
        ):
            success, summary = await self._attempt_room_stealth(
                interaction, session, party_snapshot
            )
            stealth_summary = summary
            if success:

                def apply_stealth(run: DungeonSession) -> None:
                    if run.current_room == session.current_room:
                        run.stealthed = True

                session = await self.sessions.update(key, apply_stealth)
                if session is None:
                    await interaction.followup.send(
                        "No active dungeon for this party.", ephemeral=True
                    )
                    return
            else:
                combat = await self._build_combat_state(interaction, session, party_snapshot)

                def engage_combat(run: DungeonSession) -> None:
                    if run.current_room != session.current_room:
                        return
                    run.stealthed = False
                    run.combat_state = combat
                    if combat is not None:
                        nonlocal trigger_monster_turns
                        trigger_monster_turns = True

                session = await self.sessions.update(key, engage_combat)
                if session is None:
                    await interaction.followup.send(
                        "No active dungeon for this party.", ephemeral=True
                    )
                    return
                started_combat = combat is not None
                if trigger_monster_turns:
                    self._schedule_automatic_turns(session, session.combat_state)

        triggered_messages: list[str] = []
        if triggered_events:
            for _room_id, trap, reason in triggered_events:
                trap_lines = await self._resolve_trap_trigger(
                    interaction,
                    session,
                    trap=trap,
                    party_snapshot=party_snapshot,
                )
                if not trap_lines:
                    continue
                if reason == "ignored":
                    trap_lines = ["Ignoring the hazard proves costly!"] + trap_lines
                triggered_messages.append("\n".join(trap_lines))
        if avoidance_messages:
            triggered_messages.extend(avoidance_messages)

        if destination_room is not None and exit_label is not None:
            target_text = f"Room {destination_room.id + 1}: {destination_room.name}"
            lower_label = exit_label.lower()
            if backtracked:
                message = f"You backtrack through the {lower_label} to {target_text}."
            else:
                message = f"You take the {lower_label} toward {target_text}."
        else:
            message = "You make your way through the chosen passage."

        if stealth_summary:
            message = f"{message}\n\n{stealth_summary}"
            if started_combat:
                message = f"{message} Initiative is rolled as combat erupts!"

        if triggered_messages:
            message = f"{message}\n\n" + "\n\n".join(triggered_messages)

        await interaction.followup.send(message, ephemeral=True)

        await self._refresh_session_message(interaction, session)

    async def handle_search(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        current_session = await self.sessions.get(key)
        if current_session is None:
            await interaction.response.send_message("No active dungeon to search.", ephemeral=True)
            return
        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return
        added_member = False
        collected_loot: tuple[Item, ...] = ()
        party_snapshot: tuple[int, ...] = ()
        loot_cursor = 0
        room_id: Optional[int] = None

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, collected_loot, party_snapshot, loot_cursor, room_id
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            party_snapshot = tuple(sorted(run.party_ids))
            room = run.room
            self._ensure_room_discovery_state(run, room)
            room_id_local = room.id
            room_id = room_id_local
            if not room.encounter.loot:
                return
            discovered_loot = run.discovered_loot.get(room_id_local, set())
            if not discovered_loot:
                return
            available = [
                item for item in run.room.encounter.loot if item.key in discovered_loot
            ]
            if not available:
                return
            collected_loot = tuple(available)
            loot_cursor = run.loot_cursor if party_snapshot else 0
            remaining = [
                item for item in run.room.encounter.loot if item.key not in discovered_loot
            ]
            run.room.encounter = replace(run.room.encounter, loot=tuple(remaining))

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No active dungeon to search.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)
        if not collected_loot:
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "You find nothing of value after a thorough search.",
                ephemeral=True,
            )
            return

        def restore_loot(run: DungeonSession) -> None:
            if collected_loot:
                run.room.encounter = replace(
                    run.room.encounter, loot=tuple(collected_loot)
                )
                if room_id is not None:
                    run.discovered_loot.setdefault(room_id, set()).update(
                        item.key for item in collected_loot
                    )

        if session.guild_id is None:
            await self.sessions.update(key, restore_loot)
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "Without a guild roster I can't assign the treasure to anyone.",
                ephemeral=True,
            )
            return

        characters = await self._load_party_characters(session.guild_id, party_snapshot)
        if not characters:
            await self.sessions.update(key, restore_loot)
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "No one in the party has a ready character to claim the spoils just yet.",
                ephemeral=True,
            )
            return

        shares = allocate_loot(collected_loot, party_snapshot, loot_cursor, characters.keys())
        if not shares:
            await self.sessions.update(key, restore_loot)
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "The treasure slips through your fingers—try again once everyone is ready.",
                ephemeral=True,
            )
            return

        item_lines, gold_lines, delivered_totals = await self._apply_reward_shares(
            session.guild_id, characters, shares
        )
        delivered_items, delivered_gold = delivered_totals
        if delivered_items or delivered_gold:

            def record_rewards(run: DungeonSession) -> None:
                run.treasure_items_claimed += delivered_items
                run.treasure_gold_claimed += delivered_gold

            updated = await self.sessions.update(key, record_rewards)
            if updated is not None:
                session = updated

        next_cursor = loot_cursor
        if party_snapshot:
            next_cursor = (loot_cursor + len(collected_loot)) % len(party_snapshot)
            await self.sessions.update(
                key, lambda run: setattr(run, "loot_cursor", next_cursor)
            )
            session.loot_cursor = next_cursor

        await self._refresh_session_message(interaction, session)

        message_lines: list[str] = ["You uncover hidden treasure!"]
        if item_lines:
            message_lines.append("Loot distributed:")
            message_lines.extend(item_lines)
        else:
            message_lines.append("The finds are safely packed for the expedition.")
        if gold_lines:
            message_lines.append("")
            message_lines.append("Coin shares:")
            message_lines.extend(gold_lines)

        await interaction.followup.send("\n".join(message_lines), ephemeral=True)


    async def handle_perception(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        current_session = await self.sessions.get(key)
        if current_session is None:
            await interaction.response.send_message(
                "No active dungeon to search for hidden threats.", ephemeral=True
            )
            return
        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return
        added_member = False
        nothing_hidden = False
        room_id: Optional[int] = None
        results: list[tuple[str, str, SavingThrowResult, int, str]] = []

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, nothing_hidden, room_id
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            room = run.room
            self._ensure_room_trap_state(run, room)
            self._ensure_room_discovery_state(run, room)
            room_id_local = room.id
            room_id = room_id_local
            trap_states = run.trap_states.setdefault(room_id_local, {})
            discovered_traps = run.discovered_traps.setdefault(room_id_local, set())
            discovered_loot = run.discovered_loot.setdefault(room_id_local, set())
            discovered_exits = run.discovered_exits.setdefault(room_id_local, set())
            attempts_map = run.perception_attempts.setdefault(room_id_local, {})
            trap_catalog = run.trap_catalog.setdefault(room_id_local, {})
            difficulty_map = run.perception_difficulties.setdefault(room_id_local, {})

            hidden_traps: list[Trap] = []
            for trap in room.encounter.traps:
                trap_catalog.setdefault(trap.key, trap)
                status = trap_states.get(trap.key, "hidden")
                if status in {"disarmed", "sprung"}:
                    continue
                if trap.key in discovered_traps:
                    continue
                hidden_traps.append(trap)

            hidden_loot = [item for item in room.encounter.loot if item.key not in discovered_loot]
            hidden_exits = [
                exit_option for exit_option in room.exits if exit_option.key not in discovered_exits
            ]

            if not hidden_traps and not hidden_loot and not hidden_exits:
                nothing_hidden = True
                return

            attempts_used = attempts_map.get(interaction.user.id, 0)
            attempts_map[interaction.user.id] = attempts_used + 1

            for trap in hidden_traps:
                saving_throw_data = trap.saving_throw or {}
                ability_value = saving_throw_data.get("ability", "WIS")
                ability = str(ability_value).upper()
                dc_raw = saving_throw_data.get("dc", DEFAULT_PERCEPTION_DC)
                try:
                    dc_value = int(dc_raw)
                except (TypeError, ValueError):
                    dc_value = DEFAULT_PERCEPTION_DC
                difficulty_key = ("trap", trap.key)
                dc_shift = difficulty_map.get(difficulty_key, 0)
                adjusted_dc = dc_value + dc_shift
                result = saving_throw(save_bonus=5, dc=adjusted_dc)
                results.append(("trap", trap.name, result, adjusted_dc, ability))
                if result.success:
                    self._set_trap_status(run, room_id_local, trap.key, "discovered")
                    if difficulty_key in difficulty_map:
                        difficulty_map.pop(difficulty_key, None)
                elif random.random() < PERCEPTION_DC_INCREASE_CHANCE:
                    difficulty_map[difficulty_key] = dc_shift + 1

            for item in hidden_loot:
                difficulty_key = ("loot", item.key)
                dc_shift = difficulty_map.get(difficulty_key, 0)
                adjusted_dc = DEFAULT_PERCEPTION_DC + dc_shift
                result = saving_throw(save_bonus=5, dc=adjusted_dc)
                results.append(("loot", item.name, result, adjusted_dc, "WIS"))
                if result.success:
                    discovered_loot.add(item.key)
                    if difficulty_key in difficulty_map:
                        difficulty_map.pop(difficulty_key, None)
                elif random.random() < PERCEPTION_DC_INCREASE_CHANCE:
                    difficulty_map[difficulty_key] = dc_shift + 1

            for exit_option in hidden_exits:
                difficulty_key = ("exit", exit_option.key)
                dc_shift = difficulty_map.get(difficulty_key, 0)
                adjusted_dc = DEFAULT_PERCEPTION_DC + dc_shift
                result = saving_throw(save_bonus=5, dc=adjusted_dc)
                results.append(("exit", exit_option.label, result, adjusted_dc, "WIS"))
                if result.success:
                    discovered_exits.add(exit_option.key)
                    if difficulty_key in difficulty_map:
                        difficulty_map.pop(difficulty_key, None)
                elif random.random() < PERCEPTION_DC_INCREASE_CHANCE:
                    difficulty_map[difficulty_key] = dc_shift + 1

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message(
                "No active dungeon to search for hidden threats.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)

        if room_id is None:
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "There is nothing here that a keen eye might uncover.",
                ephemeral=True,
            )
            return

        if nothing_hidden and not results:
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "You find no clues to any hidden dangers.", ephemeral=True
            )
            return
        await self._refresh_session_message(interaction, session)

        if not results:
            await interaction.followup.send(
                "You find no clues to any hidden dangers.", ephemeral=True
            )
            return

        successes = [entry for entry in results if entry[2].success]
        failures = [entry for entry in results if not entry[2].success]

        message_lines: list[str] = []
        for kind, name, roll, dc, _ability in successes:
            summary = f"(Perception roll {roll.total} vs DC {dc})"
            if kind == "trap":
                message_lines.append(
                    f"You carefully search the chamber and uncover the {name}! {summary}"
                )
            elif kind == "loot":
                message_lines.append(f"You discover hidden loot: {name}! {summary}")
            else:
                message_lines.append(f"You reveal a hidden exit: {name}! {summary}")

        if not successes:
            message_lines.append(
                "You can try again, though each failure may make the search more challenging."
            )

        await interaction.followup.send("\n".join(message_lines), ephemeral=True)




    async def handle_disarm(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        current_session = await self.sessions.get(key)
        if current_session is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return
        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return
        added_member = False
        party_snapshot: tuple[int, ...] = ()
        attempted_trap: Optional[Trap] = None
        disarm_result: Optional[SavingThrowResult] = None
        dc = 15
        ability = "DEX"
        room_id: Optional[int] = None
        loot_cursor = 0
        trap_trigger: Optional[Trap] = None
        trigger_reason: Optional[str] = None
        no_trap_available = False

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, party_snapshot, attempted_trap, disarm_result, dc, ability, room_id, loot_cursor, trap_trigger, trigger_reason, no_trap_available
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            party_snapshot = tuple(sorted(run.party_ids))
            room = run.room
            self._ensure_room_trap_state(run, room)
            self._ensure_room_discovery_state(run, room)
            traps = list(room.encounter.traps)
            if not traps:
                no_trap_available = True
                return
            room_id_local = run.current_room
            room_id = room_id_local
            discovered = run.discovered_traps.get(room_id_local, set())
            if not discovered:
                no_trap_available = True
                return
            states = run.trap_states.setdefault(room_id_local, {})
            trap_local: Optional[Trap] = None
            for trap in traps:
                if trap.key not in discovered:
                    continue
                status = states.get(trap.key, "hidden")
                if status in ("disarmed", "sprung"):
                    continue
                trap_local = trap
                break
            if trap_local is None:
                no_trap_available = True
                return
            attempted_trap = trap_local
            run.trap_catalog.setdefault(room_id_local, {}).setdefault(trap_local.key, trap_local)
            saving_throw_data = trap_local.saving_throw or {}
            ability_value = saving_throw_data.get("ability", "DEX")
            ability = str(ability_value).upper()
            dc_raw = saving_throw_data.get("dc", 15)
            try:
                dc_value = int(dc_raw)
            except (TypeError, ValueError):
                dc_value = 15
            dc = dc_value
            loot_cursor = run.loot_cursor if party_snapshot else 0
            disarm_result = saving_throw(save_bonus=5, dc=dc_value)
            if disarm_result.success:
                self._set_trap_status(run, room_id_local, trap_local.key, "disarmed")
                remaining = [trap for trap in traps if trap.key != trap_local.key]
                if len(remaining) != len(traps):
                    run.room.encounter = replace(run.room.encounter, traps=tuple(remaining))
            else:
                self._set_trap_status(run, room_id_local, trap_local.key, "sprung")
                trap_trigger = trap_local
                trigger_reason = "disarm"
                remaining = [trap for trap in traps if trap.key != trap_local.key]
                if len(remaining) != len(traps):
                    run.room.encounter = replace(run.room.encounter, traps=tuple(remaining))

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)
        if attempted_trap is None or disarm_result is None:
            await self._refresh_session_message(interaction, session)
            if no_trap_available:
                await interaction.followup.send(
                    "No revealed traps are ready to be disarmed.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "There are no traps present in this room.",
                    ephemeral=True,
                )
            return

        triggered_lines: list[str] = []
        if trap_trigger is not None:
            triggered_lines = await self._resolve_trap_trigger(
                interaction,
                session,
                trap=trap_trigger,
                party_snapshot=party_snapshot,
            )
            if trigger_reason == "disarm":
                triggered_lines = ["Your attempt destabilises the mechanism!", *triggered_lines]

        check_summary = f"(Disarm roll {disarm_result.total}, DC {dc} {ability} save)"
        if disarm_result.success:
            reward_lines: list[str] = []
            updated_session = await self.sessions.update(
                key, lambda run: setattr(run, "traps_disarmed", run.traps_disarmed + 1)
            )
            if updated_session is not None:
                session = updated_session
            if session.guild_id is not None and party_snapshot:
                characters = await self._load_party_characters(session.guild_id, party_snapshot)
                if characters:
                    order = eligible_order(party_snapshot, loot_cursor, characters.keys())
                    reward_amount = trap_reward_value(attempted_trap, dc)
                    shares = split_gold(reward_amount, order)
                    if shares:
                        (
                            _,
                            gold_lines,
                            delivered_totals,
                        ) = await self._apply_reward_shares(
                            session.guild_id, characters, shares
                        )
                        reward_lines.extend(gold_lines)
                        delivered_items, delivered_gold = delivered_totals
                        if delivered_items or delivered_gold:

                            def record_rewards(run: DungeonSession) -> None:
                                run.treasure_items_claimed += delivered_items
                                run.treasure_gold_claimed += delivered_gold

                            updated_rewards = await self.sessions.update(
                                key, record_rewards
                            )
                            if updated_rewards is not None:
                                session = updated_rewards
                        if order and party_snapshot:
                            remainder = reward_amount % len(order)
                            if remainder:
                                next_cursor = (loot_cursor + remainder) % len(party_snapshot)
                                await self.sessions.update(
                                    key, lambda run: setattr(run, "loot_cursor", next_cursor)
                                )
                                session.loot_cursor = next_cursor
            message_lines = [
                f"You disarm the {attempted_trap.name} with steady hands {check_summary}.",
            ]
            if reward_lines:
                message_lines.append("")
                message_lines.append("The guild awards hazard pay:")
                message_lines.extend(reward_lines)
            await interaction.followup.send("\n".join(message_lines), ephemeral=True)
        else:
            lines = [
                f"You attempt to disarm the {attempted_trap.name} but trigger it instead {check_summary}.",
            ]
            if triggered_lines:
                lines.append("")
                lines.extend(triggered_lines)
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        await self._refresh_session_message(interaction, session)

    async def handle_engage(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        current_session = await self.sessions.get(key)
        if current_session is None:
            await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
            return
        added_member = False
        started_combat = False
        combat_in_progress = False
        no_targets = False
        should_start_combat = False
        trigger_monster_turns = False
        party_snapshot: tuple[int, ...] = ()

        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, combat_in_progress, no_targets, should_start_combat, party_snapshot
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            combat = run.combat_state
            if combat and combat.active:
                combat_in_progress = True
                return
            if not run.room.encounter.monsters:
                no_targets = True
                return
            party_snapshot = tuple(sorted(run.party_ids))
            run.stealthed = False
            should_start_combat = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
            return

        combat: Optional[CombatState] = None
        if should_start_combat and not combat_in_progress and not no_targets and session is not None:
            combat = await self._build_combat_state(interaction, session, party_snapshot)

            def apply_combat(run: DungeonSession) -> None:
                nonlocal started_combat, combat_in_progress, trigger_monster_turns
                if run.combat_state and run.combat_state.active:
                    combat_in_progress = True
                    return
                run.combat_state = combat
                started_combat = True
                trigger_monster_turns = True

            session = await self.sessions.update(key, apply_combat)
            if session is None:
                await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
                return
            if trigger_monster_turns:
                self._schedule_automatic_turns(session, session.combat_state)

        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)

        if no_targets:
            await self._send_ephemeral_message(
                interaction,
                "The room is eerily quiet—there is nothing to fight here.",
            )
            return

        combat = session.combat_state
        if combat_in_progress and combat is not None:
            current = combat.current_combatant()
            if current and combat.active and current.is_player and not current.defeated:
                if current.user_id == interaction.user.id:
                    message = "It's already your turn—use the combat controls to act!"
                else:
                    message = f"Combat is underway! {current.name} is taking their turn."
            elif combat.active:
                message = "Combat is underway—stand by while the monsters act."
            else:
                message = "Combat has already concluded in this room."
            await self._send_ephemeral_message(interaction, message)
            await self._refresh_session_message(interaction, session)
            return

        if not started_combat or combat is None:
            await self._send_ephemeral_message(
                interaction,
                "Unable to begin combat at this time.",
            )
            await self._refresh_session_message(interaction, session)
            return

        if combat.active:
            current = combat.current_combatant()
            if current and current.is_player and not current.defeated:
                if current.user_id == interaction.user.id:
                    message = "You surge forward and act first! Choose your move from the combat controls."
                else:
                    message = f"Combat begins! {current.name} takes the first turn."
            else:
                message = "Combat begins!"
        else:
            message = "The battle is over before it truly begins."

        await self._send_ephemeral_message(interaction, message)
        await self._refresh_session_message(interaction, session)

    async def handle_combat_action(
        self,
        interaction: discord.Interaction,
        action: Literal[
            "weapon",
            "spell",
            "feature",
            "defend",
            "end",
            "attack",
            "target",
            "death_save",
        ],
        selection: Optional[str] = None,
    ) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        current_session = await self.sessions.get(key)
        if current_session is None:
            await self._send_ephemeral_message(
                interaction,
                "No active dungeon for this party.",
            )
            return
        added_member = False
        error: Optional[str] = None
        summary: Optional[str] = None
        trigger_monster_turns = False
        pending_fallen: list[CombatantState] = []

        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, error, summary, trigger_monster_turns, pending_fallen
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            combat = run.combat_state
            if combat is None or not combat.active:
                error = "Combat isn't currently active."
                return
            current = combat.current_combatant()
            if (
                current is None
                or not current.is_player
                or current.user_id != interaction.user.id
                or current.defeated
            ):
                error = "It isn't your turn to act."
                return

            consumes_turn = True
            if action == "weapon" or action == "attack":
                summary = self._player_weapon_attack(run, combat, current, selection)
                pending_fallen.extend(self._identify_newly_fallen(run, combat))
            elif action == "spell":
                summary = self._player_cast_spell(run, combat, current, selection)
                pending_fallen.extend(self._identify_newly_fallen(run, combat))
            elif action == "feature":
                summary = self._player_use_feature(run, combat, current, selection)
                pending_fallen.extend(self._identify_newly_fallen(run, combat))
            elif action == "defend":
                summary = self._player_defend(combat, current)
                pending_fallen.extend(self._identify_newly_fallen(run, combat))
            elif action == "end":
                combat.log.append(f"{current.name} ends their turn without further action.")
                self._trim_combat_log(combat)
                summary = "You end your turn."
            elif action == "death_save":
                if current.current_hp > 0:
                    error = "You are still conscious—you don't need a death save."
                    return
                if current.is_dead:
                    error = "You have already succumbed to your wounds."
                    return
                if current.stable:
                    error = "You are stable and cannot roll another death save."
                    return
                summary = self._player_roll_death_save(run, combat, current)
                pending_fallen.extend(self._identify_newly_fallen(run, combat))
            elif action == "target":
                consumes_turn = False
                target_identifier = self._resolve_target_identifier(selection)
                if not target_identifier:
                    error = "That target cannot be selected."
                    return
                target = self._find_combatant_by_identifier(combat, target_identifier)
                if target is None or target.is_player or target.defeated:
                    error = "That target is no longer available."
                    return
                current.selected_target = target.identifier
                summary = f"You focus on {target.name}."
                combat.current_action = {
                    "actor": current.name,
                    "state": "targeting",
                    "summary": f"Taking aim at {target.name}",
                    "detail": summary,
                    "emoji": "🎯",
                    "team": "player",
                }
                combat.log.append(f"{current.name} focuses on {target.name}.")
                self._trim_combat_log(combat)
            else:  # pragma: no cover - defensive
                error = "Unknown combat action."
                return

            if consumes_turn:
                combat.waiting_for = None
                self._evaluate_combat_state(run, combat)
                if combat.active:
                    next_combatant = combat.advance_turn()
                    if next_combatant is None:
                        self._finish_combat(run, combat, victory=False)
                    else:
                        nonlocal trigger_monster_turns
                        trigger_monster_turns = True
            else:
                combat.waiting_for = current.user_id

        session = await self.sessions.update(key, mutate)
        if session is None:
            await self._send_ephemeral_message(
                interaction,
                "No active dungeon for this party.",
            )
            return

        for fallen in pending_fallen:
            await self._announce_player_death(session, fallen)

        if trigger_monster_turns:
            self._schedule_automatic_turns(session, session.combat_state)

        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)

        if error is not None:
            await self._send_ephemeral_message(interaction, error)
            return

        if summary is None:
            summary = "Your action resolves."  # fallback message

        await self._send_ephemeral_message(interaction, summary)
        await self._refresh_session_message(interaction, session)


async def setup(bot: commands.Bot) -> None:
    cog = DungeonCog(bot)
    await bot.add_cog(cog)
    existing = bot.tree.get_command(
        cog.dungeon_group.name,
        type=discord.AppCommandType.chat_input,
    )
    if existing is not None:
        bot.tree.remove_command(
            cog.dungeon_group.name,
            type=discord.AppCommandType.chat_input,
        )
    bot.tree.add_command(cog.dungeon_group)

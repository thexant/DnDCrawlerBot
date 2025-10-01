"""Dungeon crawling commands and persistent interaction views."""

from __future__ import annotations

import copy
import logging
import random
import re
import secrets
from dataclasses import dataclass, field, replace
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
from dnd.dungeon import Dungeon, DungeonGenerator, Room, Theme, ThemeRegistry
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
SPELLCASTING_ABILITIES: Dict[str, str] = {
    "wizard": "INT",
}


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
    message_id: Optional[int] = None
    combat_state: Optional["CombatState"] = None
    breadcrumbs: list[int] = field(default_factory=list)
    exit_history: list[str] = field(default_factory=list)
    last_exit_taken: Optional[str] = None
    last_travel_description: Optional[str] = None
    last_travel_note: Optional[str] = None
    loot_cursor: int = 0

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
        self._add_exit_controls(session)
        self._add_action_button(
            label="Search",
            style=discord.ButtonStyle.secondary,
            custom_id="dungeon:search",
            disabled=disable_search,
            handler=self._handle_search,
        )
        self._add_action_button(
            label="Disarm Trap",
            style=discord.ButtonStyle.danger,
            custom_id="dungeon:disarm",
            disabled=disable_disarm,
            handler=self._handle_disarm,
        )
        self._add_action_button(
            label="Engage",
            style=discord.ButtonStyle.success,
            custom_id="dungeon:engage",
            disabled=disable_engage,
            handler=self._handle_engage,
        )

    def _add_exit_controls(self, session: DungeonSession) -> None:
        for exit_option in session.room.exits:
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


class CombatActionView(discord.ui.View):
    """Interaction controls shown while combat is active."""

    def __init__(self, cog: "DungeonCog", session: DungeonSession) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        combat = session.combat_state
        current = combat.current_combatant() if combat else None
        self._combat_active = bool(combat and combat.active)
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

    def _configure_controls(
        self, combat: Optional[CombatState], current: Optional[CombatantState]
    ) -> None:
        if not combat or current is None or not current.is_player or current.defeated:
            self._add_weapon_select(None)
            self._add_spell_select(None)
            self._add_feature_select(None)
            self._add_common_buttons(disabled=not self._combat_active)
            return
        self._add_weapon_select(current)
        self._add_spell_select(current)
        self._add_feature_select(current)
        self._add_common_buttons(disabled=not self._combat_active)

    def _disable_children(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

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

    def _session_key(self, guild_id: Optional[int], channel_id: Optional[int]) -> SessionKey:
        return SessionManager.make_key(guild_id, channel_id)

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
    ) -> tuple[list[str], list[str]]:
        item_lines: list[str] = []
        gold_lines: list[str] = []
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
            if share.gold:
                gold_lines.append(
                    f"• <@{share.user_id}> gains {share.gold} gold coins"
                )
        return item_lines, gold_lines

    def _get_tavern_cog(self) -> Optional["Tavern"]:
        cog = self.bot.get_cog("Tavern")
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

    def _party_display(self, interaction: discord.Interaction, session: DungeonSession) -> str:
        if not session.party_ids:
            return "No adventurers yet."

        names: list[str] = []
        for user_id in sorted(session.party_ids):
            names.append(self._display_name_for_user(interaction, user_id))
        return "\n".join(f"• {name}" for name in names)

    def _display_name_for_user(
        self, interaction: discord.Interaction, user_id: int
    ) -> str:
        name: Optional[str] = None
        if interaction.guild:
            member = interaction.guild.get_member(user_id)
            if member is not None:
                name = member.display_name
        if name is None:
            user = self.bot.get_user(user_id)
            if user is not None:
                name = user.display_name
        if name is None:
            name = f"<@{user_id}>"
        return name

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
    def _sync_combatant_state(combatant: CombatantState) -> None:
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

    def _apply_damage_to_combatant(
        self, combatant: CombatantState, amount: int, *, critical: bool = False
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
        self._sync_combatant_state(combatant)
        return dealt

    def _apply_healing_to_combatant(self, combatant: CombatantState, amount: int) -> int:
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
        self._sync_combatant_state(combatant)
        return healed

    @staticmethod
    def _death_save_roll() -> int:
        return random.randint(1, 20)

    def _resolve_player_death_save(self, player: CombatantState) -> str:
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
        self._sync_combatant_state(player)
        return " ".join(message)

    def _handle_player_zero_hp_turn(self, state: CombatState, player: CombatantState) -> bool:
        if player.current_hp > 0:
            return False
        state.waiting_for = None
        if player.is_dead or player.stable:
            return True
        message = self._resolve_player_death_save(player)
        state.log.append(message)
        self._trim_combat_log(state)
        return True

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
        if victory:
            state.log.append("The party is victorious!")
            encounter = session.room.encounter
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

    def _resolve_monster_action(self, state: CombatState, monster: CombatantState) -> None:
        potential_targets = [
            combatant
            for combatant in state.order
            if combatant.is_player and not combatant.is_dead
        ]
        if not potential_targets:
            return
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
        if multiattack:
            message = self._execute_monster_multiattack(
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
                    monster,
                    target,
                    action,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
            elif action_type == "auto":
                message = self._execute_monster_auto_action(
                    monster,
                    target,
                    action,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
            else:
                message = self._execute_monster_attack_action(
                    monster,
                    target,
                    action,
                    target_ac=target_ac,
                    resistances=resistances,
                    vulnerabilities=vulnerabilities,
                    immunities=immunities,
                )
        if not message:
            message = f"{monster.name} hesitates, accomplishing nothing."
        state.log.append(message)
        self._trim_combat_log(state)

    def _execute_monster_multiattack(
        self,
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
                    target,
                    pending_damage,
                    critical=outcome.roll_result.is_critical_hit or was_unconscious,
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
                target,
                pending_damage,
                critical=outcome.roll_result.is_critical_hit or was_unconscious,
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
                target,
                damage,
                critical=target.current_hp <= 0,
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
                target,
                damage,
                critical=target.current_hp <= 0,
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

    def _run_automatic_turns(self, session: DungeonSession, state: CombatState) -> None:
        if not state.active:
            return
        current = self._ensure_current_combatant(state)
        if current is None:
            self._finish_combat(session, state, victory=False)
            return
        while state.active:
            current = state.current_combatant()
            if current is None:
                break
            if current.is_player and current.current_hp <= 0:
                handled = self._handle_player_zero_hp_turn(state, current)
                if handled:
                    self._evaluate_combat_state(session, state)
                    if not state.active:
                        break
                    next_combatant = state.advance_turn()
                    if next_combatant is None:
                        break
                    continue
            if current.defeated:
                next_combatant = self._ensure_current_combatant(state)
                if next_combatant is None:
                    break
                continue
            if current.is_player:
                state.waiting_for = current.user_id
                break
            state.waiting_for = None
            self._resolve_monster_action(state, current)
            self._evaluate_combat_state(session, state)
            if not state.active:
                break
            next_combatant = state.advance_turn()
            if next_combatant is None:
                break
        if not state.active:
            state.waiting_for = None

    def _player_weapon_attack(
        self,
        session: DungeonSession,
        state: CombatState,
        player: CombatantState,
        selection: Optional[str],
    ) -> str:
        targets = [combatant for combatant in state.order if not combatant.is_player and not combatant.defeated]
        if not targets:
            return "There are no foes left to strike."
        target = targets[0]
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
                target, damage, critical=result.is_critical_hit
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
        targets = [combatant for combatant in state.order if not combatant.is_player and not combatant.defeated]
        effect_type = str(spell.get("type", "attack")).lower()
        target_required = effect_type in {"attack", "auto", "save", "damage"}
        target = targets[0] if targets else None
        if target_required and target is None:
            return "There are no valid targets for that spell."
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
                    target, damage, critical=result.is_critical_hit
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
            dealt = self._apply_damage_to_combatant(target, damage)
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
                    dealt = self._apply_damage_to_combatant(target, damage)
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
                dealt = self._apply_damage_to_combatant(target, damage)
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
            self._sync_combatant_state(player)
            summary += " You begin concentrating on the spell."
            log_entry += f" {player.name} begins concentrating on {spell_name}."
        status_text = self._resource_status_text(player, requirement)
        if status_text:
            summary += f" {status_text}"
        self._sync_combatant_state(player)
        state.log.append(log_entry)
        self._trim_combat_log(state)
        self._evaluate_combat_state(session, state)
        return summary

    def _player_use_feature(
        self,
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
        if isinstance(effect, Mapping):
            effect_type = str(effect.get("type", "")).lower()
            if effect_type == "heal":
                heal_expr = str(effect.get("dice", "1d6"))
                healed = self._roll_damage(heal_expr)
                restored = self._apply_healing_to_combatant(player, healed)
                summary = f"You use {feature_name}, regaining {restored} HP."
                log_entry = f"{player.name} uses {feature_name}, regaining {restored} HP."
            elif effect_type == "condition":
                condition = str(effect.get("condition", "")).strip()
                target_scope = str(effect.get("target", "self")).lower()
                if condition:
                    if target_scope == "self":
                        player.conditions.add(condition)
                        self._sync_combatant_state(player)
                        summary = f"{feature_name} grants you {condition}."
                        log_entry = f"{player.name} uses {feature_name}, gaining {condition}."
        status_text = self._resource_status_text(player, requirement if isinstance(requirement, Mapping) else None)
        if status_text:
            summary += f" {status_text}"
        self._sync_combatant_state(player)
        state.log.append(log_entry)
        self._trim_combat_log(state)
        return summary

    def _player_defend(self, state: CombatState, player: CombatantState) -> str:
        message = f"{player.name} takes a defensive stance, ready for the next assault."
        state.log.append(message)
        self._trim_combat_log(state)
        return "You brace yourself, gaining no additional effects but readying for the next turn."

    async def _build_combat_state(
        self,
        interaction: discord.Interaction,
        session: DungeonSession,
        party_order: Optional[Sequence[int]] = None,
    ) -> CombatState:
        combatants: List[CombatantState] = []
        warnings_log: List[str] = []
        guild_id = interaction.guild_id
        party_ids = list(party_order) if party_order is not None else sorted(session.party_ids)
        for user_id in party_ids:
            roll = random.randint(1, 20)
            name = self._display_name_for_user(interaction, user_id)
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
            combatant = CombatantState(
                identifier=f"player:{user_id}",
                name=name,
                initiative_roll=roll,
                initiative_total=initiative_total,
                max_hp=max_hp,
                current_hp=max_hp,
                is_player=True,
                user_id=user_id,
                metadata=metadata,
                conditions=conditions,
                concentration=concentration,
                resources=shared_resources,
            )
            self._sync_combatant_state(combatant)
            combatants.append(combatant)
        for index, monster in enumerate(session.room.encounter.monsters):
            roll = random.randint(1, 20)
            initiative_total = roll
            dex_score = monster.ability_scores.get("DEX") if monster.ability_scores else None
            if dex_score is not None:
                initiative_total += (int(dex_score) - 10) // 2
            monster_resources: Dict[str, object] = {}
            monster_combatant = CombatantState(
                identifier=f"monster:{index}",
                name=monster.name,
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

    def _build_room_embed(self, interaction: discord.Interaction, session: DungeonSession) -> discord.Embed:
        room = session.room
        dungeon = session.dungeon
        embed = discord.Embed(
            title=f"{dungeon.name} — Room {room.id + 1}: {room.name}",
            description=room.description,
            color=discord.Color.dark_purple(),
        )
        embed.add_field(name="Encounter", value=room.encounter.summary or "Quiet for now.", inline=False)

        if room.encounter.monsters:
            monsters = "\n".join(
                f"• {monster.name} (AC {monster.armor_class}, HP {monster.hit_points})"
                for monster in room.encounter.monsters
            )
            embed.add_field(name="Monsters", value=monsters, inline=False)

        if room.encounter.traps:
            trap_lines = []
            for trap in room.encounter.traps:
                dc = trap.saving_throw.get("dc") if trap.saving_throw else None
                ability = trap.saving_throw.get("ability") if trap.saving_throw else None
                detail = f"DC {dc} {ability} save" if dc and ability else "Hidden hazard"
                trap_lines.append(f"• {trap.name} ({detail})")
            embed.add_field(name="Traps", value="\n".join(trap_lines), inline=False)

        if room.encounter.loot:
            loot_lines = [f"• {item.name} ({item.rarity})" for item in room.encounter.loot]
            embed.add_field(name="Loot", value="\n".join(loot_lines), inline=False)

        approach_lines: list[str] = []
        if session.last_travel_note:
            approach_lines.append(session.last_travel_note)
        travel = session.travel_description()
        if travel:
            approach_lines.append(travel)
        if approach_lines:
            embed.add_field(name="Approach", value="\n".join(approach_lines), inline=False)

        embed.add_field(name="Party", value=self._party_display(interaction, session), inline=False)

        combat = session.combat_state
        if combat is not None:
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
                turn_marker = "➡️ " if combat.active and index == combat.turn_index and not combatant.defeated else ""
                initiative_lines.append(
                    f"{turn_marker}{combatant.name} — Init {combatant.initiative_total} "
                    f"(Roll {combatant.initiative_roll}) — {status}"
                )
            if initiative_lines:
                embed.add_field(name="Initiative Order", value="\n".join(initiative_lines), inline=False)
            current = combat.current_combatant()
            if combat.active and current is not None and not current.defeated:
                turn_text = f"Round {combat.round_number}: {current.name} is acting."
            elif combat.active:
                turn_text = f"Round {combat.round_number}: resolving initiative..."
            else:
                turn_text = "Combat has concluded."
            embed.add_field(name="Current Turn", value=turn_text, inline=False)
            if combat.log:
                log_entries = combat.log[-MAX_COMBAT_LOG_ENTRIES:]
                while log_entries and len("\n".join(log_entries)) > 1024:
                    log_entries = log_entries[1:]
                log_text = "\n".join(log_entries) if log_entries else "(log truncated)"
                embed.add_field(name="Combat Log", value=log_text or "No events yet.", inline=False)

        exit_lines: list[str] = []
        visited_rooms = set(session.breadcrumbs)
        previous_room = session.breadcrumbs[-2] if len(session.breadcrumbs) >= 2 else None
        for exit_option in room.exits:
            try:
                destination_room = dungeon.get_room(exit_option.destination)
            except KeyError:
                continue
            status: str
            if exit_option.destination == previous_room:
                status = f"Backtrack to Room {destination_room.id + 1}: {destination_room.name}"
            elif exit_option.destination in visited_rooms:
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
            if room.encounter.traps:
                actions.append("Attempt to disarm the traps.")
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

    def _build_navigation_view(self, session: DungeonSession) -> discord.ui.View:
        combat = session.combat_state
        if combat and combat.active:
            return CombatActionView(self, session)
        room = session.room
        return DungeonNavigationView(
            self,
            session,
            disable_search=not bool(room.encounter.loot),
            disable_disarm=not bool(room.encounter.traps),
            disable_engage=not bool(room.encounter.monsters),
        )

    async def _refresh_session_message(self, interaction: discord.Interaction, session: DungeonSession) -> None:
        if session.message_id is None:
            return
        embed = self._build_room_embed(interaction, session)
        view = self._build_navigation_view(session)
        try:
            await interaction.followup.edit_message(
                message_id=session.message_id,
                embed=embed,
                view=view,
            )
            self.bot.add_view(view, message_id=session.message_id)
        except discord.HTTPException:
            pass

    async def _send_ephemeral_message(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _start_prepared_dungeon(
        self, interaction: discord.Interaction, stored: StoredDungeon
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
        session = DungeonSession(
            dungeon=dungeon,
            guild_id=interaction.guild_id,
            channel_id=party_channel.id,
            seed=stored.seed,
        )
        session.party_ids.add(interaction.user.id)
        await self.sessions.set(key, session)

        await self._sync_party_channel_access(session)

        embed = self._build_room_embed(interaction, session)
        view = self._build_navigation_view(session)
        try:
            message = await party_channel.send(embed=embed, view=view)
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
    async def prepare(
        self,
        interaction: discord.Interaction,
        theme: Optional[str] = None,
        size: app_commands.Range[int, 1, 20] = 5,
        difficulty: Literal["easy", "standard", "hard"] = "standard",
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
        generator = DungeonGenerator(theme_obj, seed=seed, difficulty=difficulty)
        dungeon = generator.generate(
            room_count=int(size), name=name, difficulty=difficulty
        )
        await self.metadata_store.record_session(
            interaction.guild_id,
            theme=theme_obj.key,
            seed=seed,
            difficulty=difficulty,
            name=dungeon.name,
            room_count=int(size),
        )

        details = [f"Theme: {theme_obj.name}"]
        details.append(f"Rooms: {int(size)}")
        if difficulty:
            details.append(f"Difficulty: {difficulty.title()}")
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

    @dungeon_group.command(
        name="category",
        description="Configure the category where delve channels are created.",
    )
    @app_commands.describe(
        category="Category for new delve channels",
        clear="Clear the configured category",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def configure_category(
        self,
        interaction: discord.Interaction,
        category: Optional[discord.CategoryChannel] = None,
        clear: bool = False,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Delve categories can only be managed from within a guild.",
                ephemeral=True,
            )
            return

        if clear:
            await self.metadata_store.set_delve_category(interaction.guild_id, None)
            await interaction.response.send_message(
                "Cleared the configured delve category.",
                ephemeral=True,
            )
            return

        if category is None:
            await interaction.response.send_message(
                "Please choose a category or enable the clear option.",
                ephemeral=True,
            )
            return

        if category.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "Please select a category from this server.",
                ephemeral=True,
            )
            return

        me = interaction.guild.me
        if me is not None:
            permissions = category.permissions_for(me)
            if not permissions.manage_channels:
                await interaction.response.send_message(
                    (
                        f"I cannot create channels in {category.name}. "
                        "Grant me Manage Channels permission for that category or choose another."
                    ),
                    ephemeral=True,
                )
                return

        await self.metadata_store.set_delve_category(interaction.guild_id, category.id)
        await interaction.response.send_message(
            f"New delve channels will be created under {category.name}.",
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

        def mutate(run: DungeonSession) -> None:
            nonlocal moved, backtracked, added_member, exit_label, destination_room
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True

            current_room = run.room
            selected_exit = next((option for option in current_room.exits if option.key == exit_key), None)
            if selected_exit is None:
                return

            origin_room_id = run.current_room
            destination_id = selected_exit.destination
            if destination_id == origin_room_id:
                return

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
            run.last_exit_taken = selected_exit.key
            run.last_travel_description = corridor.description if corridor else None
            exit_label = selected_exit.label
            lower_label = selected_exit.label.lower()
            if backtracked:
                run.last_travel_note = f"The party backtracks through the {lower_label}."
            else:
                run.last_travel_note = f"The party takes the {lower_label}."
            destination_room = destination_room_local
            moved = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.followup.send("No active dungeon for this party.", ephemeral=True)
            return

        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)

        if not moved:
            await interaction.followup.send(
                "That passage isn't accessible right now. Try another direction.",
                ephemeral=True,
            )
            return

        await self._refresh_session_message(interaction, session)

        if destination_room is not None and exit_label is not None:
            target_text = f"Room {destination_room.id + 1}: {destination_room.name}"
            lower_label = exit_label.lower()
            if backtracked:
                message = f"You backtrack through the {lower_label} to {target_text}."
            else:
                message = f"You take the {lower_label} toward {target_text}."
        else:
            message = "You make your way through the chosen passage."

        await interaction.followup.send(message, ephemeral=True)

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

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, collected_loot, party_snapshot, loot_cursor
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            party_snapshot = tuple(sorted(run.party_ids))
            if not run.room.encounter.loot:
                return
            collected_loot = tuple(run.room.encounter.loot)
            loot_cursor = run.loot_cursor if party_snapshot else 0
            run.room.encounter = replace(run.room.encounter, loot=())

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

        item_lines, gold_lines = await self._apply_reward_shares(
            session.guild_id, characters, shares
        )

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
        result: Optional[SavingThrowResult] = None
        dc = 15
        ability = "DEX"
        loot_cursor = 0

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, party_snapshot, attempted_trap, result, dc, ability, loot_cursor
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True
            party_snapshot = tuple(sorted(run.party_ids))
            traps = list(run.room.encounter.traps)
            if not traps:
                return
            trap_local = traps[0]
            attempted_trap = trap_local
            saving_throw_data = trap_local.saving_throw or {}
            ability_value = saving_throw_data.get("ability", "DEX")
            dc_raw = saving_throw_data.get("dc", 15)
            try:
                dc_value = int(dc_raw)
            except (TypeError, ValueError):
                dc_value = 15
            dc = dc_value
            ability = str(ability_value).upper()
            loot_cursor = run.loot_cursor if party_snapshot else 0
            roll_result = saving_throw(save_bonus=5, dc=dc_value)
            result = roll_result
            if roll_result.success:
                traps.pop(0)
                run.room.encounter = replace(run.room.encounter, traps=tuple(traps))

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)
        if attempted_trap is None or result is None:
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                "There are no traps present in this room.",
                ephemeral=True,
            )
            return

        summary = f"(Roll {result.total}, DC {dc} {ability} save)"

        if result.success:
            reward_lines: list[str] = []
            if session.guild_id is not None and party_snapshot:
                characters = await self._load_party_characters(session.guild_id, party_snapshot)
                if characters:
                    order = eligible_order(party_snapshot, loot_cursor, characters.keys())
                    reward_amount = trap_reward_value(attempted_trap, dc)
                    shares = split_gold(reward_amount, order)
                    if shares:
                        _, gold_lines = await self._apply_reward_shares(
                            session.guild_id, characters, shares
                        )
                        reward_lines.extend(gold_lines)
                        if order and party_snapshot:
                            remainder = reward_amount % len(order)
                            if remainder:
                                next_cursor = (loot_cursor + remainder) % len(party_snapshot)
                                await self.sessions.update(
                                    key, lambda run: setattr(run, "loot_cursor", next_cursor)
                                )
                                session.loot_cursor = next_cursor
            await self._refresh_session_message(interaction, session)
            message_lines = [f"You expertly disarm the {attempted_trap.name}! {summary}"]
            if reward_lines:
                message_lines.append("")
                message_lines.append("Coin shares:")
                message_lines.extend(reward_lines)
            await interaction.followup.send("\n".join(message_lines), ephemeral=True)
        else:
            await self._refresh_session_message(interaction, session)
            await interaction.followup.send(
                (
                    f"The {attempted_trap.name} resists your efforts {summary}. "
                    "Perhaps try another approach."
                ),
                ephemeral=True,
            )

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
            should_start_combat = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
            return

        combat: Optional[CombatState] = None
        if should_start_combat and not combat_in_progress and not no_targets and session is not None:
            combat = await self._build_combat_state(interaction, session, party_snapshot)

            def apply_combat(run: DungeonSession) -> None:
                nonlocal started_combat, combat_in_progress
                if run.combat_state and run.combat_state.active:
                    combat_in_progress = True
                    return
                run.combat_state = combat
                started_combat = True
                self._run_automatic_turns(run, combat)

            session = await self.sessions.update(key, apply_combat)
            if session is None:
                await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
                return

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
        action: Literal["weapon", "spell", "feature", "defend", "end", "attack"],
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

        if interaction.user.id not in current_session.party_ids:
            if not await self._ensure_character_available(interaction):
                return

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member, error, summary
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

            if action == "weapon" or action == "attack":
                summary = self._player_weapon_attack(run, combat, current, selection)
            elif action == "spell":
                summary = self._player_cast_spell(run, combat, current, selection)
            elif action == "feature":
                summary = self._player_use_feature(combat, current, selection)
            elif action == "defend":
                summary = self._player_defend(combat, current)
            elif action == "end":
                combat.log.append(f"{current.name} ends their turn without further action.")
                self._trim_combat_log(combat)
                summary = "You end your turn."
            else:  # pragma: no cover - defensive
                error = "Unknown combat action."
                return

            self._evaluate_combat_state(run, combat)
            if combat.active:
                next_combatant = combat.advance_turn()
                if next_combatant is None:
                    self._finish_combat(run, combat, victory=False)
                else:
                    self._run_automatic_turns(run, combat)

        session = await self.sessions.update(key, mutate)
        if session is None:
            await self._send_ephemeral_message(
                interaction,
                "No active dungeon for this party.",
            )
            return

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

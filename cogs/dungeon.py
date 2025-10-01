"""Dungeon crawling commands and persistent interaction views."""

from __future__ import annotations

import logging
import random
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterable, List, Literal, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from dnd.combat import ability_modifier, attack_roll, saving_throw
from dnd.characters import EQUIPMENT, Character
from dnd.repository import CharacterRepository
from dnd.content import ContentLibrary, ContentLoadError
from dnd.dungeon import Dungeon, DungeonGenerator, Room, Theme, ThemeRegistry
from dnd.dungeon.state import DungeonMetadataStore, StoredDungeon
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

    @property
    def defeated(self) -> bool:
        return self.current_hp <= 0


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
        self._combat_active = bool(session.combat_state and session.combat_state.active)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and not self._combat_active:
                child.disabled = True

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

    @discord.ui.button(label="Attack", style=discord.ButtonStyle.danger, custom_id="dungeon:combat:attack")
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_combat_action(interaction, "attack")

    @discord.ui.button(label="Defend", style=discord.ButtonStyle.secondary, custom_id="dungeon:combat:defend")
    async def defend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_combat_action(interaction, "defend")

    @discord.ui.button(label="End Turn", style=discord.ButtonStyle.primary, custom_id="dungeon:combat:end")
    async def end_turn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_combat_action(interaction, "end")


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

    def _roll_damage(self, expression: str) -> int:
        match = _DAMAGE_ROLL_PATTERN.fullmatch(expression.strip())
        if not match:
            return random.randint(1, 8)
        count = max(1, int(match.group("count")))
        sides = max(1, int(match.group("sides")))
        modifier = int(match.group("modifier") or 0)
        total = sum(random.randint(1, sides) for _ in range(count)) + modifier
        return max(0, total)

    def _trim_combat_log(self, state: CombatState) -> None:
        excess = len(state.log) - MAX_COMBAT_LOG_ENTRIES
        if excess > 0:
            del state.log[0:excess]

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

    def _resolve_monster_action(self, state: CombatState, monster: CombatantState) -> None:
        targets = [combatant for combatant in state.order if combatant.is_player and not combatant.defeated]
        if not targets:
            return
        target = random.choice(targets)
        attack_bonus = int(monster.metadata.get("attack_bonus", 4))
        target_ac = int(target.metadata.get("armor_class", DEFAULT_PLAYER_ARMOR_CLASS))
        result = attack_roll(attack_bonus, target_ac)
        if result.hits:
            damage_expr = str(monster.metadata.get("damage", "1d6+1"))
            damage = self._roll_damage(damage_expr)
            target.current_hp = max(0, target.current_hp - damage)
            message = (
                f"{monster.name} hits {target.name} for {damage} damage. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
            if target.defeated:
                message += f" {target.name} is defeated!"
        else:
            message = (
                f"{monster.name} misses {target.name}. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
        state.log.append(message)
        self._trim_combat_log(state)

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
        features = [feature.name for feature in character.character_class.features if feature.level <= 1]
        equipment_summary = []
        for key in equipment_keys:
            item = EQUIPMENT.get(key)
            equipment_summary.append(item.name if item else key.replace("_", " ").title())
        metadata: Dict[str, object] = {
            "armor_class": armor_class,
            "initiative_bonus": initiative_bonus,
            "attack_options": weapon_options,
            "default_attack_index": 0,
            "combat_options": {
                "weapons": weapon_options,
                "spellcasting": spellcasting_profile or {},
                "features": features,
            },
            "equipment": equipment_summary,
            "proficiency_bonus": PROFICIENCY_BONUS,
            "features": features,
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

    def _player_attack(
        self,
        session: DungeonSession,
        state: CombatState,
        player: CombatantState,
    ) -> str:
        targets = [combatant for combatant in state.order if not combatant.is_player and not combatant.defeated]
        if not targets:
            return "There are no foes left to strike."
        target = targets[0]
        attack_bonus = int(player.metadata.get("attack_bonus", DEFAULT_PLAYER_ATTACK_BONUS))
        damage_expr = str(player.metadata.get("damage", DEFAULT_PLAYER_DAMAGE))
        weapon_label = str(player.metadata.get("weapon_name", "weapon"))
        options = player.metadata.get("attack_options")
        if isinstance(options, list) and options:
            index_raw = player.metadata.get("default_attack_index", 0)
            try:
                option_index = int(index_raw)
            except (TypeError, ValueError):
                option_index = 0
            if option_index < 0 or option_index >= len(options):
                option_index = 0
            option = options[option_index]
            attack_bonus = int(option.get("attack_bonus", attack_bonus))
            damage_expr = str(option.get("damage", damage_expr))
            weapon_label = str(option.get("name", weapon_label))
        target_ac = int(target.metadata.get("armor_class", 10))
        result = attack_roll(attack_bonus, target_ac)
        if result.hits:
            damage = self._roll_damage(damage_expr)
            target.current_hp = max(0, target.current_hp - damage)
            weapon_text = "" if weapon_label.lower() == "weapon" else f" with your {weapon_label}"
            summary = (
                f"You hit {target.name}{weapon_text} for {damage} damage! "
                f"(Attack {result.total} vs AC {target_ac})"
            )
            log_entry = (
                f"{player.name} hits {target.name}{weapon_text} for {damage} damage. "
                f"(Attack {result.total} vs AC {target_ac})"
            )
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
                        "spellcasting": {},
                        "features": [],
                    },
                    "proficiency_bonus": PROFICIENCY_BONUS,
                    "features": [],
                    "weapon_name": "Fallback Strike",
                    "attack_bonus": DEFAULT_PLAYER_ATTACK_BONUS,
                    "damage": DEFAULT_PLAYER_DAMAGE,
                    "character_name": name,
                    "max_hp": DEFAULT_PLAYER_HP,
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
                "spellcasting": {},
                "features": metadata.get("features", []),
            })
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
            combatants.append(
                CombatantState(
                    identifier=f"player:{user_id}",
                    name=name,
                    initiative_roll=roll,
                    initiative_total=initiative_total,
                    max_hp=max_hp,
                    current_hp=max_hp,
                    is_player=True,
                    user_id=user_id,
                    metadata=metadata,
                )
            )
        for index, monster in enumerate(session.room.encounter.monsters):
            roll = random.randint(1, 20)
            initiative_total = roll
            dex_score = monster.ability_scores.get("DEX") if monster.ability_scores else None
            if dex_score is not None:
                initiative_total += (int(dex_score) - 10) // 2
            combatants.append(
                CombatantState(
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
                )
            )
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
                status = (
                    f"{combatant.current_hp}/{combatant.max_hp} HP"
                    if not combatant.defeated
                    else "Defeated"
                )
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

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No active dungeon to search.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._refresh_session_message(interaction, session)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)
        loot = session.room.encounter.loot
        if not loot:
            await interaction.followup.send(
                "You find nothing of value after a thorough search.",
                ephemeral=True,
            )
            return

        lines = [f"• {item.name} ({item.rarity})" for item in loot]
        await interaction.followup.send(
            "You uncover hidden items:\n" + "\n".join(lines),
            ephemeral=True,
        )

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

        def mutate(run: DungeonSession) -> None:
            nonlocal added_member
            if interaction.user.id not in run.party_ids:
                run.party_ids.add(interaction.user.id)
                added_member = True

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._refresh_session_message(interaction, session)
        if added_member and interaction.guild_id is not None:
            await self._handle_party_membership_change(interaction.guild_id, session)
        traps = session.room.encounter.traps
        if not traps:
            await interaction.followup.send(
                "There are no traps present in this room.",
                ephemeral=True,
            )
            return

        trap = traps[0]
        dc = int(trap.saving_throw.get("dc", 15)) if trap.saving_throw else 15
        ability = str(trap.saving_throw.get("ability", "DEX")) if trap.saving_throw else "DEX"

        result = saving_throw(save_bonus=5, dc=dc)
        if result.success:
            message = (
                f"You expertly disarm the {trap.name}! "
                f"(Roll {result.total}, DC {dc} {ability} save)"
            )
        else:
            message = (
                f"The {trap.name} resists your efforts (Roll {result.total}, DC {dc} {ability} save). "
                "Perhaps try another approach."
            )
        await interaction.followup.send(message, ephemeral=True)

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
        self, interaction: discord.Interaction, action: Literal["attack", "defend", "end"]
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

            if action == "attack":
                summary = self._player_attack(run, combat, current)
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

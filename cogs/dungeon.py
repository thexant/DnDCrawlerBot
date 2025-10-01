"""Dungeon crawling commands and persistent interaction views."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dnd.combat import saving_throw
from dnd.dungeon import Dungeon, DungeonGenerator, Room, Theme, ThemeRegistry
from dnd.sessions import SessionKey, SessionManager


def _default_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "themes"


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

    @property
    def room(self) -> Room:
        return self.dungeon.rooms[self.current_room]

    @property
    def at_final_room(self) -> bool:
        return self.current_room >= len(self.dungeon.rooms) - 1

    def travel_description(self) -> Optional[str]:
        if self.current_room == 0:
            return None
        for corridor in self.dungeon.corridors:
            if corridor.to_room == self.current_room:
                return corridor.description
        return None


class DungeonNavigationView(discord.ui.View):
    """Button controls for navigating dungeon sessions."""

    def __init__(
        self,
        cog: "DungeonCog",
        *,
        disable_proceed: bool = False,
        disable_search: bool = False,
        disable_disarm: bool = False,
        disable_engage: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        disabled = {
            "dungeon:proceed": disable_proceed,
            "dungeon:search": disable_search,
            "dungeon:disarm": disable_disarm,
            "dungeon:engage": disable_engage,
        }
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id in disabled:
                child.disabled = disabled[child.custom_id]

    @discord.ui.button(label="Proceed", style=discord.ButtonStyle.primary, custom_id="dungeon:proceed")
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_proceed(interaction)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.secondary, custom_id="dungeon:search")
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_search(interaction)

    @discord.ui.button(label="Disarm Trap", style=discord.ButtonStyle.danger, custom_id="dungeon:disarm")
    async def disarm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_disarm(interaction)

    @discord.ui.button(label="Engage", style=discord.ButtonStyle.success, custom_id="dungeon:engage")
    async def engage(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_engage(interaction)


class DungeonCog(commands.Cog):
    """Slash commands to generate and explore procedural dungeons."""

    dungeon_group = app_commands.Group(name="dungeon", description="Procedural dungeon exploration")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        data_path = _default_data_path()
        self.theme_registry = ThemeRegistry.load_from_path(data_path)
        self.sessions: SessionManager[DungeonSession] = SessionManager()
        self.guild_themes: Dict[int, str] = {}

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        try:
            self.bot.tree.remove_command(self.dungeon_group.name, type=self.dungeon_group.type)
        except (app_commands.CommandTreeException, KeyError):
            pass

    # ------------------------------------------------------------------
    def _session_key(self, guild_id: Optional[int], channel_id: Optional[int]) -> SessionKey:
        return SessionManager.make_key(guild_id, channel_id)

    def _resolve_theme(self, theme_name: Optional[str], guild_id: Optional[int]) -> Theme:
        if theme_name:
            return self.theme_registry.get(theme_name)
        if guild_id is not None:
            default = self.guild_themes.get(guild_id)
            if default:
                return self.theme_registry.get(default)
        try:
            return next(iter(self.theme_registry.values()))
        except StopIteration as exc:
            raise RuntimeError("No dungeon themes are available") from exc

    def _party_display(self, interaction: discord.Interaction, session: DungeonSession) -> str:
        if not session.party_ids:
            return "No adventurers yet."

        names: list[str] = []
        for user_id in sorted(session.party_ids):
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
            names.append(name)
        return "\n".join(f"• {name}" for name in names)

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

        travel = session.travel_description()
        if travel:
            embed.add_field(name="Approach", value=travel, inline=False)

        embed.add_field(name="Party", value=self._party_display(interaction, session), inline=False)

        actions: list[str] = []
        if not session.at_final_room:
            actions.append("Proceed deeper into the dungeon.")
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
        if session.seed is not None:
            footer_parts.append(f"Seed: {session.seed}")
        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    def _build_navigation_view(self, session: DungeonSession) -> DungeonNavigationView:
        room = session.room
        return DungeonNavigationView(
            self,
            disable_proceed=session.at_final_room,
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

    # ---- Slash commands --------------------------------------------------
    @dungeon_group.command(name="start", description="Generate a themed dungeon and begin exploring.")
    @app_commands.describe(
        theme="Name of the dungeon theme to use",
        rooms="Number of rooms to generate",
        seed="Optional RNG seed",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        theme: Optional[str] = None,
        rooms: app_commands.Range[int, 1, 20] = 5,
        seed: Optional[int] = None,
    ) -> None:
        if not self.theme_registry.values():
            await interaction.response.send_message(
                "No dungeon themes are available. Please add files under data/themes.",
                ephemeral=True,
            )
            return

        try:
            theme_obj = self._resolve_theme(theme, interaction.guild_id)
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

        key = self._session_key(interaction.guild_id, interaction.channel_id)
        existing = await self.sessions.get(key)
        if existing is not None:
            await interaction.response.send_message(
                "A party is already exploring this channel. Use /dungeon reset to start over.",
                ephemeral=True,
            )
            return

        if seed is None:
            seed = random.randint(0, 999999)
        generator = DungeonGenerator(theme_obj, seed=seed)
        dungeon = generator.generate(room_count=int(rooms))
        session = DungeonSession(
            dungeon=dungeon,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id or interaction.user.id,
            seed=seed,
        )
        session.party_ids.add(interaction.user.id)
        await self.sessions.set(key, session)

        embed = self._build_room_embed(interaction, session)
        view = self._build_navigation_view(session)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        session = await self.sessions.update(key, lambda run: setattr(run, "message_id", message.id))
        if session is not None:
            self.bot.add_view(view, message_id=message.id)

    @start.autocomplete("theme")
    async def theme_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> Iterable[app_commands.Choice[str]]:
        names = [theme.name for theme in self.theme_registry.values()]
        filtered = [name for name in names if current.lower() in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in filtered]

    @dungeon_group.command(name="reset", description="Reset the active dungeon session in this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.sessions.pop(key)
        if session is None:
            await interaction.response.send_message("There is no active dungeon in this channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if session.message_id is not None:
            try:
                await interaction.followup.edit_message(message_id=session.message_id, view=None)
            except discord.HTTPException:
                pass
        await interaction.followup.send("The dungeon session has been reset.", ephemeral=True)

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
            self.guild_themes.pop(interaction.guild_id, None)
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

        self.guild_themes[interaction.guild_id] = theme.name
        await interaction.response.send_message(
            f"Default dungeon theme set to {theme.name}.",
            ephemeral=True,
        )

    # ---- Interaction handlers -------------------------------------------
    async def handle_proceed(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.sessions.get(key)
        if session is None:
            await interaction.response.send_message("No active dungeon for this party.", ephemeral=True)
            return

        await interaction.response.defer()
        advanced = False
        reached_final = session.at_final_room

        def mutate(run: DungeonSession) -> None:
            nonlocal advanced, reached_final
            run.party_ids.add(interaction.user.id)
            if run.at_final_room:
                reached_final = True
                return
            run.current_room += 1
            advanced = True
            reached_final = run.at_final_room

        session = await self.sessions.update(key, mutate)
        if session is None:
            await interaction.followup.send("No active dungeon for this party.", ephemeral=True)
            return

        await self._refresh_session_message(interaction, session)
        if advanced:
            await interaction.followup.send("You press onward into the next chamber...", ephemeral=True)
        elif reached_final:
            await interaction.followup.send(
                "The party has already reached the end of this dungeon!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Unable to proceed right now. Try again in a moment.",
                ephemeral=True,
            )

    async def handle_search(self, interaction: discord.Interaction) -> None:
        key = self._session_key(interaction.guild_id, interaction.channel_id)
        session = await self.sessions.update(key, lambda run: run.party_ids.add(interaction.user.id))
        if session is None:
            await interaction.response.send_message("No active dungeon to search.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._refresh_session_message(interaction, session)
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
        session = await self.sessions.update(key, lambda run: run.party_ids.add(interaction.user.id))
        if session is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._refresh_session_message(interaction, session)
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
        session = await self.sessions.update(key, lambda run: run.party_ids.add(interaction.user.id))
        if session is None:
            await interaction.response.send_message("No foes stand before the party right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._refresh_session_message(interaction, session)
        monsters = session.room.encounter.monsters
        if not monsters:
            await interaction.followup.send(
                "The room is eerily quiet—there is nothing to fight here.",
                ephemeral=True,
            )
            return

        foe = random.choice(monsters)
        attack_roll = random.randint(1, 20) + 5
        if attack_roll >= foe.armor_class:
            message = f"Your strike hits {foe.name}! (Attack {attack_roll} vs AC {foe.armor_class})"
        else:
            message = f"Your blow glances off {foe.name}'s defenses. (Attack {attack_roll} vs AC {foe.armor_class})"
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    cog = DungeonCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.dungeon_group)

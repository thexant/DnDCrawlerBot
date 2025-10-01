"""Tavern hub management for coordinating adventurers between dungeons."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence, Set, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from dnd import CharacterRepository, TavernConfig, TavernConfigStore
from dnd.dungeon.state import StoredDungeon

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from cogs.dungeon import DungeonCog


log = logging.getLogger(__name__)

TAVERN_ROLE_NAME = "Tavern Adventurer"


class DungeonMapSelect(discord.ui.Select):
    """Select menu for choosing a prepared dungeon to explore."""

    def __init__(
        self,
        tavern: "Tavern",
        *,
        guild_id: int,
        dungeons: Sequence[StoredDungeon],
    ) -> None:
        options: list[discord.SelectOption] = []
        for dungeon in dungeons[:25]:
            details: list[str] = []
            if dungeon.difficulty:
                details.append(dungeon.difficulty.title())
            if dungeon.room_count:
                details.append(f"{dungeon.room_count} rooms")
            if dungeon.seed is not None:
                details.append(f"Seed {dungeon.seed}")
            description = ", ".join(details) or "Ready for adventure"
            options.append(
                discord.SelectOption(
                    label=dungeon.name[:100],
                    value=dungeon.name,
                    description=description[:100],
                )
            )
        placeholder = "Select a prepared dungeon to begin"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tavern:dungeon_select",
        )
        self.tavern = tavern
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        dungeon_cog = self.tavern._get_dungeon_cog()
        if dungeon_cog is None:
            await interaction.response.send_message(
                "Dungeon operations are currently unavailable.",
                ephemeral=True,
            )
            return

        choice = self.values[0]
        stored = await dungeon_cog.metadata_store.get_dungeon(self.guild_id, choice)
        if stored is None:
            await interaction.response.send_message(
                "That expedition is no longer on the map. Try refreshing the tavern board.",
                ephemeral=True,
            )
            return

        await dungeon_cog._start_prepared_dungeon(interaction, stored)


class DungeonMapView(discord.ui.View):
    """View wrapper for the dungeon selection dropdown."""

    def __init__(
        self,
        tavern: "Tavern",
        *,
        guild_id: int,
        dungeons: Sequence[StoredDungeon],
    ) -> None:
        super().__init__(timeout=120)
        self.add_item(DungeonMapSelect(tavern, guild_id=guild_id, dungeons=dungeons))


class TavernControlView(discord.ui.View):
    """Interactive controls for the tavern hub embed."""

    def __init__(self, cog: "Tavern") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Visit the Shop",
        style=discord.ButtonStyle.secondary,
        custom_id="tavern:shop",
    )
    async def visit_shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await interaction.response.send_message(
            "The shopkeeper is still stocking the shelves. Check back soon!",
            ephemeral=True,
        )

    @discord.ui.button(
        label="View Dungeon Map",
        style=discord.ButtonStyle.primary,
        custom_id="tavern:map",
    )
    async def view_map(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        if not interaction.guild:
            await interaction.response.send_message(
                "Dungeon information is only available within a server.",
                ephemeral=True,
            )
            return
        embed, view, fallback = await self.cog.build_dungeon_map_components(interaction.guild.id)
        if embed is None or view is None:
            await interaction.response.send_message(fallback, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class Tavern(commands.GroupCog, name="tavern", description="Configure the guild's tavern hub"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        data_path = Path("data")
        self.config_store = TavernConfigStore(data_path / "taverns.json")
        self.characters = CharacterRepository(data_path / "characters.json")
        self.refresh_views.start()

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        self.refresh_views.cancel()

    async def build_dungeon_map_components(
        self, guild_id: int
    ) -> tuple[Optional[discord.Embed], Optional[discord.ui.View], str]:
        dungeon_cog = self._get_dungeon_cog()
        if dungeon_cog is None:
            return (
                None,
                None,
                "Dungeon operations are currently unavailable.",
            )

        dungeons = await dungeon_cog.metadata_store.list_dungeons(guild_id)
        if not dungeons:
            return (
                None,
                None,
                (
                    "No expeditions are prepared yet. Ask an administrator to use "
                    "/dungeon prepare so the map can be charted."
                ),
            )

        display_dungeons = list(dungeons[:25])
        lines: list[str] = []
        for stored in display_dungeons:
            try:
                theme = dungeon_cog.theme_registry.get(stored.theme)
                theme_name = theme.name
            except KeyError:
                theme_name = stored.theme
            details: list[str] = [theme_name]
            if stored.difficulty:
                details.append(stored.difficulty.title())
            if stored.room_count:
                details.append(f"{stored.room_count} rooms")
            if stored.seed is not None:
                details.append(f"Seed {stored.seed}")
            summary = ", ".join(details)
            lines.append(f"• **{stored.name}** — {summary}")

        embed = discord.Embed(
            title="Dungeon Map",
            description="Select a prepared expedition from the map below.",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="Prepared Expeditions",
            value="\n".join(lines),
            inline=False,
        )
        if len(dungeons) > len(display_dungeons):
            embed.set_footer(text="Only the first 25 expeditions are shown. Use /dungeon start for others.")
        else:
            embed.set_footer(text="Choose an expedition to rally the party.")

        view = DungeonMapView(self, guild_id=guild_id, dungeons=display_dungeons)
        return embed, view, ""

    @app_commands.command(name="set", description="Designate a channel as the guild's tavern hub")
    @app_commands.describe(channel="Text channel to host the tavern. Defaults to the current channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_tavern(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Tavern configuration is only available inside a server.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel) or target.guild != interaction.guild:
            await interaction.response.send_message(
                "Please choose a text channel from this server to host the tavern.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        previous = await self.config_store.get_config(interaction.guild.id)
        new_config = await self.config_store.set_channel(interaction.guild.id, target.id)

        if previous and previous.message_id and previous.channel_id != target.id:
            await self._delete_previous_message(interaction.guild, previous)

        await self._refresh_tavern_for_config(new_config)

        channel_mention = target.mention
        await interaction.followup.send(f"The tavern is now located in {channel_mention}.", ephemeral=True)

    @tasks.loop(minutes=5)
    async def refresh_views(self) -> None:
        configs = await self.config_store.all_configs()
        for config in configs:
            await self._refresh_tavern_for_config(config)

    @refresh_views.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    def _get_dungeon_cog(self) -> Optional["DungeonCog"]:
        cog = self.bot.get_cog("DungeonCog")
        if cog is None:
            return None
        try:
            from cogs.dungeon import DungeonCog
        except ImportError:  # pragma: no cover - defensive
            return None
        return cog if isinstance(cog, DungeonCog) else None

    async def refresh_tavern_access(self, guild_id: int) -> None:
        """Synchronise tavern role membership for ``guild_id``."""

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        try:
            role = await self._ensure_tavern_role(guild)
        except discord.HTTPException as exc:
            log.debug("Unable to ensure tavern role in %s: %s", guild.name, exc)
            return
        allowed = await self._eligible_member_ids(guild_id)
        await self._sync_role_membership(role, allowed)

    async def _refresh_tavern_for_config(self, config: TavernConfig) -> None:
        guild = self.bot.get_guild(config.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        await self._sync_channel_access(channel)
        await self._refresh_tavern_embed(channel, config)

    async def _sync_channel_access(self, channel: discord.TextChannel) -> None:
        guild = channel.guild
        try:
            role = await self._ensure_tavern_role(guild)
        except discord.HTTPException:
            log.warning("Missing permissions to manage tavern role in %s", guild.name)
            return
        allowed = await self._eligible_member_ids(guild.id)
        await self._sync_role_membership(role, allowed)

        overwrites = channel.overwrites_for(guild.default_role)
        if overwrites.view_channel is not False:
            try:
                await channel.set_permissions(guild.default_role, view_channel=False)
            except discord.HTTPException:
                log.warning("Failed to update default role permissions for tavern in %s", guild.name)

        role_overwrites = channel.overwrites_for(role)
        changed = False
        if role_overwrites.view_channel is not True or role_overwrites.send_messages is not True:
            role_overwrites.view_channel = True
            role_overwrites.send_messages = True
            role_overwrites.read_message_history = True
            changed = True
        if changed:
            try:
                await channel.set_permissions(role, overwrite=role_overwrites)
            except discord.HTTPException:
                log.warning("Failed to apply tavern role permissions in %s", guild.name)

    async def _refresh_tavern_embed(self, channel: discord.TextChannel, config: TavernConfig) -> None:
        if config.message_id:
            try:
                message = await channel.fetch_message(config.message_id)
            except (discord.NotFound, discord.HTTPException):
                message = None
            else:
                try:
                    await message.delete()
                except discord.HTTPException:
                    log.debug("Could not delete previous tavern embed in %s", channel.name)

        embed = discord.Embed(
            title="The Adventurers' Tavern",
            description=(
                "A warm hearth welcomes heroes between expeditions. Share tales, "
                "recruit allies, and prepare for the next delve."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Planning",
            value="Use the buttons below to browse services before embarking on your next dungeon run.",
            inline=False,
        )
        embed.set_footer(text="The tavern board refreshes every five minutes.")

        view = TavernControlView(self)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.warning("Failed to send tavern embed in %s", channel.name)
            await self.config_store.update_message(channel.guild.id, None)
            return
        await self.config_store.update_message(channel.guild.id, message.id)

    async def _delete_previous_message(self, guild: discord.Guild, config: TavernConfig) -> None:
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(config.message_id)  # type: ignore[arg-type]
        except (discord.NotFound, discord.HTTPException, TypeError):
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("Unable to delete previous tavern embed in %s", channel.name)

    async def _ensure_tavern_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=TAVERN_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(name=TAVERN_ROLE_NAME, reason="Tavern access role")
            except discord.HTTPException as exc:
                log.warning("Failed to create tavern role in %s: %s", guild.name, exc)
                raise
        return role

    async def _eligible_member_ids(self, guild_id: int) -> Set[int]:
        characters = await self.characters.list_guild_characters(guild_id)
        if not characters:
            return set()
        dungeon_cog = self._get_dungeon_cog()
        active: Set[int] = set()
        if dungeon_cog is not None:
            sessions = await dungeon_cog.sessions.values()
            for session in sessions:
                if session.guild_id == guild_id:
                    active.update(session.party_ids)
        return {user_id for user_id in characters if user_id not in active}

    async def _sync_role_membership(self, role: discord.Role, allowed: Set[int]) -> None:
        guild = role.guild
        current_ids = {member.id for member in role.members}
        to_add = allowed - current_ids
        to_remove = current_ids - allowed

        for member_id in to_add:
            member = guild.get_member(member_id)
            if member is None:
                continue
            try:
                await member.add_roles(role, reason="Granted tavern access")
            except discord.HTTPException:
                log.debug("Failed to add tavern role to %s in %s", member, guild.name)

        for member_id in to_remove:
            member = guild.get_member(member_id)
            if member is None:
                continue
            try:
                await member.remove_roles(role, reason="Tavern access revoked")
            except discord.HTTPException:
                log.debug("Failed to remove tavern role from %s in %s", member, guild.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tavern(bot))

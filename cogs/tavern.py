"""Tavern hub management for coordinating adventurers between dungeons."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Set, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from dnd import CharacterRepository, TavernConfig, TavernConfigStore

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from cogs.dungeon import DungeonCog


log = logging.getLogger(__name__)

TAVERN_ROLE_NAME = "Tavern Adventurer"


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
        description = await self.cog.build_dungeon_map_description(interaction.guild.id)
        await interaction.response.send_message(description, ephemeral=True)


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

    async def build_dungeon_map_description(self, guild_id: int) -> str:
        dungeon_cog = self._get_dungeon_cog()
        if dungeon_cog is None:
            return "Dungeon operations are currently unavailable."
        names = await dungeon_cog.metadata_store.list_dungeon_names(guild_id)
        if not names:
            return (
                "No expeditions are prepared yet. Ask an administrator to generate a dungeon "
                "so the map can be charted."
            )
        lines = "\n".join(f"• {name}" for name in names)
        return f"Available dungeon expeditions:\n{lines}"

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

"""Slash commands for coordinating games within a guild."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from dnd import GuildGame, GuildGameConfigStore


log = logging.getLogger(__name__)


def _validate_image_url(image: Optional[str]) -> Optional[str]:
    if image is None:
        return None
    image = image.strip()
    if not image:
        return None
    if not image.lower().startswith(("http://", "https://")):
        raise ValueError("Image must be a valid HTTP or HTTPS URL")
    return image


class LookingForGroup(commands.Cog):
    """Provide commands for managing games and announcing sessions."""

    gconfig = app_commands.Group(
        name="gconfig", description="Manage games that can be announced"
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        data_path = Path("data")
        self.config_store = GuildGameConfigStore(data_path / "games.json")

    async def _resolve_game(
        self, guild_id: int, identifier: str
    ) -> Optional[GuildGame]:
        game = await self.config_store.get_game(guild_id, identifier)
        if game is not None:
            return game
        games = await self.config_store.list_games(guild_id)
        lowered = identifier.casefold()
        for option in games:
            if option.title.casefold() == lowered:
                return option
        return None

    async def _game_choices(
        self, guild_id: int, query: str
    ) -> Sequence[app_commands.Choice[str]]:
        games = await self.config_store.list_games(guild_id)
        if not games:
            return []
        query_lower = query.casefold()
        choices: list[app_commands.Choice[str]] = []
        for game in games:
            if query_lower and query_lower not in game.title.casefold():
                continue
            choices.append(app_commands.Choice(name=game.title, value=game.key))
            if len(choices) >= 25:
                break
        return choices[:25]

    @gconfig.command(name="add", description="Add or update a game configuration")
    @app_commands.describe(
        name="Display name for the game",
        image="Optional image URL to include with announcements",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gconfig_add(
        self,
        interaction: discord.Interaction,
        name: str,
        image: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Game configuration can only be modified inside a server.",
                ephemeral=True,
            )
            return

        try:
            image_url = _validate_image_url(image)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        try:
            game = await self.config_store.upsert_game(
                interaction.guild.id, name=name, image_url=image_url
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"Saved configuration for **{game.title}**.", ephemeral=True
        )

    @gconfig.command(name="remove", description="Remove a configured game")
    @app_commands.describe(name="Name of the game to remove")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gconfig_remove(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Game configuration can only be modified inside a server.",
                ephemeral=True,
            )
            return

        removed = await self.config_store.remove_game(interaction.guild.id, name)
        if not removed:
            await interaction.response.send_message(
                f"I couldn't find a game named **{name}** to remove.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Removed configuration for **{name}**.", ephemeral=True
        )

    @gconfig.command(name="list", description="List configured games")
    async def gconfig_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        games = await self.config_store.list_games(interaction.guild.id)
        if not games:
            await interaction.response.send_message(
                "No games have been configured yet. Use /gconfig add to add one.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Configured Games",
            color=discord.Color.blurple(),
        )
        for game in games:
            description = "Image set" if game.image_url else "No image configured"
            embed.add_field(name=game.title, value=description, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _send_announcement(
        self,
        interaction: discord.Interaction,
        *,
        game: GuildGame,
        title: str,
        description: str,
        color: discord.Color,
    ) -> None:
        embed = discord.Embed(title=title, description=description, color=color)
        if interaction.user and interaction.user.display_avatar:
            embed.set_footer(
                text=f"Requested by {interaction.user.display_name}",
                icon_url=interaction.user.display_avatar.url,
            )
        if game.image_url:
            embed.set_image(url=game.image_url)
        embed.add_field(name="Game", value=game.title, inline=False)

        try:
            await interaction.response.send_message(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Failed to send LFG embed in %s: %s", interaction.channel, exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I couldn't send the announcement. Please try again later.",
                    ephemeral=True,
                )

    async def _autocomplete_games(
        self, interaction: discord.Interaction, current: str
    ) -> Sequence[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        return await self._game_choices(interaction.guild.id, current)

    @app_commands.command(name="nowplaying", description="Share what you're playing")
    @app_commands.describe(
        game="Select the game you are currently playing",
        message="Optional message to include with the announcement",
    )
    @app_commands.autocomplete(game=_autocomplete_games)
    async def now_playing(
        self,
        interaction: discord.Interaction,
        game: str,
        message: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        resolved = await self._resolve_game(interaction.guild.id, game)
        if resolved is None:
            await interaction.response.send_message(
                "I couldn't find that game. Use the autocomplete list to choose a configured game.",
                ephemeral=True,
            )
            return

        description = (
            message
            if message
            else f"{interaction.user.mention} is now playing **{resolved.title}**!"
        )
        await self._send_announcement(
            interaction,
            game=resolved,
            title="Now Playing",
            description=description,
            color=discord.Color.brand_green(),
        )

    @app_commands.command(name="lfg", description="Look for players for a game")
    @app_commands.describe(
        game="Select the game you want to find players for",
        message="Optional message to include with the announcement",
    )
    @app_commands.autocomplete(game=_autocomplete_games)
    async def looking_for_group(
        self,
        interaction: discord.Interaction,
        game: str,
        message: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        resolved = await self._resolve_game(interaction.guild.id, game)
        if resolved is None:
            await interaction.response.send_message(
                "I couldn't find that game. Use the autocomplete list to choose a configured game.",
                ephemeral=True,
            )
            return

        base = f"{interaction.user.mention} is looking for players to join **{resolved.title}**!"
        description = f"{base}\n\n{message}" if message else base
        await self._send_announcement(
            interaction,
            game=resolved,
            title="Looking for Group",
            description=description,
            color=discord.Color.orange(),
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LookingForGroup(bot))

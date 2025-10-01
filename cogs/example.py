import discord
from discord import app_commands
from discord.ext import commands


class Example(commands.Cog):
    """An example cog showcasing a simple slash command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Respond with the bot's current latency."""
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency: {latency_ms}ms", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Example(bot))

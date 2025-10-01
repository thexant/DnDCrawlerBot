import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )


def get_cog_module_names(cogs_path: Path) -> list[str]:
    module_names: list[str] = []
    for path in cogs_path.glob("*.py"):
        if path.name.startswith("__"):
            continue
        module_names.append(f"cogs.{path.stem}")
    return module_names


def load_environment() -> str:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is required. "
            "Set it in the .env file before starting the bot."
        )
    return token


async def load_cogs(bot: commands.Bot, cogs_path: Path) -> None:
    module_names = get_cog_module_names(cogs_path)
    for module_name in module_names:
        await bot.load_extension(module_name)
        logging.info("Loaded cog: %s", module_name)


class SlashCommandBot(commands.Bot):
    """Bot subclass that only supports slash (application) commands."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self._cogs_path = Path(__file__).parent / "cogs"

    async def setup_hook(self) -> None:  # type: ignore[override]
        await load_cogs(self, self._cogs_path)
        logging.info("All cogs loaded")
        synced_commands = await self.tree.sync()
        logging.info("Synced %s application commands", len(synced_commands))

    def add_command(  # type: ignore[override]
        self, command: commands.Command, *args, **kwargs
    ) -> None:
        raise TypeError("SlashCommandBot does not support prefixed commands.")

    async def process_commands(self, message: discord.Message) -> None:  # type: ignore[override]
        """Override to disable prefix command processing entirely."""
        return


def create_bot() -> commands.Bot:
    return SlashCommandBot()


def main() -> None:
    configure_logging()
    token = load_environment()
    bot = create_bot()

    try:
        bot.run(token)
    except KeyboardInterrupt:
        logging.info("Shutting down bot")


if __name__ == "__main__":
    main()

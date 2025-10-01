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


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def setup_hook() -> None:  # type: ignore[override]
        cogs_path = Path(__file__).parent / "cogs"
        await load_cogs(bot, cogs_path)
        logging.info("All cogs loaded")

    return bot


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

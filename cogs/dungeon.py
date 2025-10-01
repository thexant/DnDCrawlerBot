"""Dungeon crawling commands and views."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from dnd.combat import saving_throw
from dnd.dungeon import Dungeon, DungeonGenerator, Room, Theme, ThemeRegistry


def _default_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "themes"


@dataclass
class DungeonRun:
    dungeon: Dungeon
    current_room: int = 0
    seed: int | None = None

    @property
    def room(self) -> Room:
        return self.dungeon.rooms[self.current_room]

    def travel_description(self) -> str | None:
        if self.current_room == 0:
            return None
        for corridor in self.dungeon.corridors:
            if corridor.to_room == self.current_room:
                return corridor.description
        return None


class DungeonNavigationView(discord.ui.View):
    def __init__(self, cog: "DungeonCog", run_key: Tuple[int, int]) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.run_key = run_key
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:  # noqa: D401 - discord.py hook
        run = self.cog.active_runs.pop(self.run_key, None)
        if run is None:
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="The expedition grows quiet as the magic fades.", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Proceed", style=discord.ButtonStyle.primary)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_proceed(interaction, self)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.secondary)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_search(interaction)

    @discord.ui.button(label="Disarm Trap", style=discord.ButtonStyle.danger)
    async def disarm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        await self.cog.handle_disarm(interaction)


class DungeonCog(commands.Cog):
    """Slash commands to generate and explore procedural dungeons."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        data_path = _default_data_path()
        self.theme_registry = ThemeRegistry.load_from_path(data_path)
        self.active_runs: Dict[Tuple[int, int], DungeonRun] = {}

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        try:
            self.bot.tree.remove_command(self.dungeon_group.name, type=self.dungeon_group.type)
        except (app_commands.CommandTreeException, KeyError):
            pass

    # ---- Command helpers -------------------------------------------------
    def _run_key(self, interaction: discord.Interaction) -> Tuple[int, int]:
        guild_id = interaction.guild_id or interaction.user.id
        channel_id = interaction.channel_id or interaction.user.id
        return (guild_id, channel_id)

    def _resolve_theme(self, name: str | None) -> Theme:
        if name:
            return self.theme_registry.get(name)
        try:
            return next(iter(self.theme_registry.values()))
        except StopIteration as exc:
            raise RuntimeError("No dungeon themes are available") from exc

    def _build_room_embed(self, run: DungeonRun) -> discord.Embed:
        room = run.room
        dungeon = run.dungeon
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

        travel = run.travel_description()
        if travel:
            embed.add_field(name="Approach", value=travel, inline=False)

        footer_parts = [f"Theme: {dungeon.theme.name}"]
        if dungeon.seed is not None:
            footer_parts.append(f"Seed: {dungeon.seed}")
        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    # ---- Slash commands --------------------------------------------------
    dungeon_group = app_commands.Group(name="dungeon", description="Procedural dungeon exploration")

    @dungeon_group.command(name="start", description="Generate a themed dungeon and begin exploring.")
    @app_commands.describe(theme="Name of the dungeon theme to use", rooms="Number of rooms to generate", seed="Optional RNG seed")
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
            theme_obj = self._resolve_theme(theme)
        except KeyError:
            available = ", ".join(sorted(t.name for t in self.theme_registry.values()))
            await interaction.response.send_message(
                f"Unknown theme '{theme}'. Available themes: {available}.",
                ephemeral=True,
            )
            return

        if seed is None:
            seed = random.randint(0, 999999)
        generator = DungeonGenerator(theme_obj, seed=seed)
        dungeon = generator.generate(room_count=int(rooms))
        run = DungeonRun(dungeon=dungeon, seed=seed)
        key = self._run_key(interaction)
        self.active_runs[key] = run

        embed = self._build_room_embed(run)
        view = DungeonNavigationView(self, key)

        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @start.autocomplete("theme")
    async def theme_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> Iterable[app_commands.Choice[str]]:
        names = [theme.name for theme in self.theme_registry.values()]
        filtered = [name for name in names if current.lower() in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in filtered]

    # ---- Interaction handlers -------------------------------------------
    async def handle_proceed(self, interaction: discord.Interaction, view: DungeonNavigationView) -> None:
        key = self._run_key(interaction)
        run = self.active_runs.get(key)
        if run is None:
            await interaction.response.send_message("No active dungeon for this party.", ephemeral=True)
            return

        if run.current_room >= len(run.dungeon.rooms) - 1:
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            self.active_runs.pop(key, None)
            await interaction.response.edit_message(view=view)
            await interaction.followup.send(
                "The party has already reached the end of this dungeon!",
                ephemeral=True,
            )
            return

        run.current_room += 1
        embed = self._build_room_embed(run)
        new_view = DungeonNavigationView(self, key)
        new_view.message = interaction.message

        await interaction.response.edit_message(embed=embed, view=new_view)

    async def handle_search(self, interaction: discord.Interaction) -> None:
        key = self._run_key(interaction)
        run = self.active_runs.get(key)
        if run is None:
            await interaction.response.send_message("No active dungeon to search.", ephemeral=True)
            return

        loot = run.room.encounter.loot
        if not loot:
            await interaction.response.send_message("You find nothing of value after a thorough search.", ephemeral=True)
            return

        lines = [f"• {item.name} ({item.rarity})" for item in loot]
        await interaction.response.send_message(
            "You uncover hidden items:\n" + "\n".join(lines),
            ephemeral=True,
        )

    async def handle_disarm(self, interaction: discord.Interaction) -> None:
        key = self._run_key(interaction)
        run = self.active_runs.get(key)
        if run is None:
            await interaction.response.send_message("No traps challenge the party right now.", ephemeral=True)
            return

        traps = run.room.encounter.traps
        if not traps:
            await interaction.response.send_message("There are no traps present in this room.", ephemeral=True)
            return

        trap = traps[0]
        dc = int(trap.saving_throw.get("dc", 15)) if trap.saving_throw else 15
        ability = str(trap.saving_throw.get("ability", "DEX")) if trap.saving_throw else "DEX"

        # Assume a skilled rogue with a +5 bonus attempts the disarm for quick resolution.
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
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    cog = DungeonCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.dungeon_group)

"""Interactive character creation flow using Discord components."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dnd import (
    ABILITY_NAMES,
    AVAILABLE_CLASSES,
    AVAILABLE_RACES,
    AbilityScores,
    Character,
    CharacterRepository,
)


class RaceSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, view: "CharacterCreationView") -> None:
        self._creation_view = view
        options = [
            discord.SelectOption(
                label=race.name,
                description=race.description[:100],
            )
            for race in AVAILABLE_RACES.values()
        ]
        super().__init__(
            placeholder="Choose a race",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self._creation_view.selected_race = self.values[0]
        await self._creation_view.refresh(interaction)


class ClassSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, view: "CharacterCreationView") -> None:
        self._creation_view = view
        options = [
            discord.SelectOption(
                label=character_class.name,
                description=f"Primary: {character_class.primary_ability}",
            )
            for character_class in AVAILABLE_CLASSES.values()
        ]
        super().__init__(
            placeholder="Choose a class",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self._creation_view.selected_class = self.values[0]
        await self._creation_view.refresh(interaction)


class AbilityScoreModal(discord.ui.Modal, title="Assign Ability Scores"):
    def __init__(self, view: "CharacterCreationView") -> None:
        super().__init__(timeout=180)
        self._creation_view = view

        default_lines = []
        if view.ability_scores:
            for ability in ABILITY_NAMES:
                value = view.ability_scores.values.get(ability, "")
                default_lines.append(f"{ability}: {value}")
        default = "\n".join(default_lines)

        placeholder_lines = [f"{ability}: <score>" for ability in ABILITY_NAMES]
        placeholder = "\n".join(placeholder_lines)

        self.assignment_input = discord.ui.TextInput(
            label="Ability assignments",
            placeholder=placeholder,
            default=default,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=300,
        )
        self.add_item(self.assignment_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        raw_assignments = self.assignment_input.value
        ability_lookup = {ability.lower(): ability for ability in ABILITY_NAMES}
        ability_lookup.update({ability[:3].lower(): ability for ability in ABILITY_NAMES})

        assignments: Dict[str, int] = {}
        errors = []
        for line in raw_assignments.splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            if not sep:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0]
                    value = parts[1]
                else:
                    errors.append(f"Could not parse line: '{line}'")
                    continue
            key = key.strip().lower()
            ability = ability_lookup.get(key)
            if not ability:
                errors.append(f"Unknown ability name: '{key}'")
                continue
            value = value.strip()
            try:
                assignments[ability] = int(value)
            except ValueError:
                errors.append(f"Invalid score for {ability}: '{value}'")

        missing = [ability for ability in ABILITY_NAMES if ability not in assignments]
        if missing:
            errors.append("Missing scores for: " + ", ".join(missing))

        if errors:
            await interaction.response.send_message(
                "❌ " + "\n".join(errors),
                ephemeral=True,
            )
            return

        try:
            scores = AbilityScores.from_assignments(assignments, method="standard_array")
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        self._creation_view.set_ability_scores(scores)
        await interaction.response.send_message(
            "✅ Ability scores updated using the standard array.",
            ephemeral=True,
        )
        await self._creation_view.refresh()


class AbilityScoreButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, view: "CharacterCreationView") -> None:
        super().__init__(label="Assign ability scores", style=discord.ButtonStyle.primary)
        self._creation_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        modal = AbilityScoreModal(self._creation_view)
        await interaction.response.send_modal(modal)


class ResetButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, view: "CharacterCreationView") -> None:
        super().__init__(label="Reset choices", style=discord.ButtonStyle.secondary)
        self._creation_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        self._creation_view.reset()
        await interaction.response.edit_message(
            embed=self._creation_view.build_embed(),
            view=self._creation_view,
        )


class ConfirmButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, view: "CharacterCreationView") -> None:
        super().__init__(label="Confirm character", style=discord.ButtonStyle.success, disabled=True)
        self._creation_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.response.defer(ephemeral=True, thinking=True)

        view = self._creation_view
        if not interaction.guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        if await view.repository.exists(interaction.guild.id, interaction.user.id):
            await interaction.followup.send(
                "You already have a character saved. Reset first if you want to recreate it.",
                ephemeral=True,
            )
            return

        ability_scores = view.ability_scores
        if not (view.selected_race and view.selected_class and ability_scores):
            await interaction.followup.send(
                "Please complete all steps before confirming.",
                ephemeral=True,
            )
            return

        race = AVAILABLE_RACES[view.selected_race]
        character_class = AVAILABLE_CLASSES[view.selected_class]
        character = Character(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            race=race,
            character_class=character_class,
            ability_scores=ability_scores,
            name=f"{interaction.user.display_name}'s Adventurer",
        )
        await view.repository.save(character)

        confirmation_embed = view.build_confirmation_embed(interaction.user, character)
        if view.message:
            await view.message.edit(embed=confirmation_embed, view=None)
        await interaction.followup.send("Character saved successfully!", ephemeral=True)
        view.stop()


class CharacterCreationView(discord.ui.View):
    def __init__(self, repository: CharacterRepository, user: discord.abc.User) -> None:
        super().__init__(timeout=600)
        self.repository = repository
        self.user = user
        self.selected_race: Optional[str] = None
        self.selected_class: Optional[str] = None
        self.ability_scores: Optional[AbilityScores] = None
        self.message: Optional[discord.Message] = None

        self.race_select = RaceSelect(self)
        self.class_select = ClassSelect(self)
        self.ability_button = AbilityScoreButton(self)
        self.reset_button = ResetButton(self)
        self.confirm_button = ConfirmButton(self)

        self.add_item(self.race_select)
        self.add_item(self.class_select)
        self.add_item(self.ability_button)
        self.add_item(self.reset_button)
        self.add_item(self.confirm_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the user who initiated the creation can interact with this view.",
                ephemeral=True,
            )
            return False
        return True

    async def start(self, interaction: discord.Interaction) -> None:
        embed = self.build_embed()
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
        self.message = await interaction.original_response()

    def set_ability_scores(self, scores: AbilityScores) -> None:
        self.ability_scores = scores
        self._update_confirm_state()

    def reset(self) -> None:
        self.selected_race = None
        self.selected_class = None
        self.ability_scores = None
        self._update_confirm_state()

    async def refresh(self, interaction: Optional[discord.Interaction] = None) -> None:
        self._update_confirm_state()
        embed = self.build_embed()
        if interaction is not None:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        description = (
            "Use the menus and buttons below to assemble your adventurer. "
            "Ability scores use the D&D 5e standard array (15, 14, 13, 12, 10, 8)."
        )
        embed = discord.Embed(title="D&D Character Creation", description=description, colour=discord.Colour.blurple())
        embed.add_field(
            name="Race",
            value=self.selected_race or "Not selected",
            inline=False,
        )
        embed.add_field(
            name="Class",
            value=self.selected_class or "Not selected",
            inline=False,
        )
        if self.ability_scores:
            embed.add_field(
                name="Ability Scores",
                value="\n".join(self.ability_scores.as_lines()),
                inline=False,
            )
        else:
            embed.add_field(
                name="Ability Scores",
                value="Not assigned",
                inline=False,
            )
        embed.set_footer(text="Confirm to save your character once all steps are complete.")
        return embed

    def build_confirmation_embed(
        self, user: discord.abc.User, character: Character
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{character.name}",
            description=f"{user.mention}'s freshly forged adventurer",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Race", value=character.race.name, inline=True)
        embed.add_field(name="Class", value=character.character_class.name, inline=True)
        embed.add_field(
            name="Ability Scores",
            value="\n".join(character.ability_scores.as_lines()),
            inline=False,
        )
        embed.set_footer(text="Character saved. Use future commands to view or manage it.")
        return embed

    def _update_confirm_state(self) -> None:
        self.confirm_button.disabled = not (
            self.selected_race and self.selected_class and self.ability_scores
        )

    async def on_timeout(self) -> None:
        if self.message:
            timeout_embed = self.build_embed()
            timeout_embed.set_footer(
                text="Session expired. Run /character create again to restart."
            )
            await self.message.edit(embed=timeout_embed, view=None)


class CharacterCreation(commands.GroupCog, name="character", description="Create and manage D&D characters"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self.repository = CharacterRepository(Path("data") / "characters.json")

    @app_commands.command(name="create", description="Create a new D&D character")
    async def character_create(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Character creation is only available inside servers.",
                ephemeral=True,
            )
            return

        existing = await self.repository.exists(interaction.guild.id, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                "You already have a saved character. Reset or delete it before creating another.",
                ephemeral=True,
            )
            return

        view = CharacterCreationView(self.repository, interaction.user)
        await view.start(interaction)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CharacterCreation(bot))

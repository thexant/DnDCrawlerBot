"""Interactive character creation flow using Discord components."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import random
from typing import Dict, Mapping, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from dnd import (
    ABILITY_NAMES,
    AVAILABLE_BACKGROUNDS,
    AVAILABLE_CLASSES,
    AVAILABLE_RACES,
    AbilityScores,
    Character,
    CharacterRepository,
)
from dnd.characters import EquipmentChoice, EquipmentChoiceOption, SkillSelection

class CreationStateError(ValueError):
    """Raised when invalid state transitions occur during creation."""


@dataclass
class CreationState:
    """Pure data container describing a user's creation progress."""

    method: str = "rolled"
    base_scores: AbilityScores | None = None
    ability_scores: AbilityScores | None = None
    race_key: str | None = None
    race_languages: tuple[str, ...] = field(default_factory=tuple)
    class_key: str | None = None
    class_skill_choices: tuple[str, ...] = field(default_factory=tuple)
    background_key: str | None = None
    equipment_choices: Dict[str, tuple[str, ...]] = field(default_factory=dict)
    racial_bonuses: Dict[str, int] = field(default_factory=dict)

    def assign_scores(self, assignments: Mapping[str, int]) -> AbilityScores:
        normalized = {
            ability.upper(): int(value)
            for ability, value in assignments.items()
        }
        base = AbilityScores(normalized)
        self.base_scores = base
        if self.race_key:
            self.apply_race(self.race_key)
        else:
            self.ability_scores = base
            self.racial_bonuses = {}
        return base

    def roll_scores(self) -> AbilityScores:
        assignments = {
            ability: self._roll_ability_score()
            for ability in ABILITY_NAMES
        }
        return self.assign_scores(assignments)

    @staticmethod
    def _roll_ability_score() -> int:
        rolls = sorted(random.randint(1, 6) for _ in range(4))
        return sum(rolls[1:])

    def _require_base_scores(self) -> AbilityScores:
        if not self.base_scores:
            raise CreationStateError("Assign ability scores before selecting a race")
        return self.base_scores

    def apply_race(self, race_key: str) -> None:
        base_scores = self._require_base_scores()
        key = race_key.lower()
        if key not in AVAILABLE_RACES:
            raise CreationStateError("Unknown race selection")
        race = AVAILABLE_RACES[key]
        languages = tuple(dict.fromkeys(race.languages.fixed)) or ("Common",)
        if "Common" not in languages:
            languages = ("Common",) + languages
        self.race_languages = languages
        bonuses: Dict[str, int] = {}
        for bonus in race.ability_bonuses:
            bonuses[bonus.ability] = bonuses.get(bonus.ability, 0) + bonus.bonus
        self.racial_bonuses = bonuses
        self.ability_scores = base_scores.with_bonuses(bonuses)
        self.race_key = key

    def set_class(self, class_key: str) -> None:
        key = class_key.lower()
        if key not in AVAILABLE_CLASSES:
            raise CreationStateError("Unknown class selection")
        self.class_key = key
        self.class_skill_choices = tuple()
        self.equipment_choices = {}

    def set_class_skills(self, skills: Sequence[str]) -> None:
        if not self.class_key:
            raise CreationStateError("Select a class before choosing skills")
        character_class = AVAILABLE_CLASSES[self.class_key]
        required = character_class.skill_proficiency_options.count
        options = set(character_class.skill_proficiency_options.options)
        if required == 0:
            self.class_skill_choices = tuple()
            return
        cleaned = tuple(dict.fromkeys(str(skill) for skill in skills))
        if len(cleaned) != required:
            raise CreationStateError(
                f"Select exactly {required} skill proficiency{'ies' if required != 1 else ''}"
            )
        if not set(cleaned).issubset(options):
            raise CreationStateError("Invalid skill selection for class")
        self.class_skill_choices = cleaned

    def set_background(self, background_key: str) -> None:
        key = background_key.lower()
        if key not in AVAILABLE_BACKGROUNDS:
            raise CreationStateError("Unknown background selection")
        self.background_key = key

    def set_equipment_choice(self, choice_key: str, option_keys: Sequence[str]) -> None:
        if not self.class_key:
            raise CreationStateError("Select a class before choosing equipment")
        character_class = AVAILABLE_CLASSES[self.class_key]
        choice = self._find_equipment_choice(character_class.equipment_choices, choice_key)
        cleaned = tuple(dict.fromkeys(str(option).lower() for option in option_keys))
        if len(cleaned) != choice.choose:
            raise CreationStateError(
                f"Select exactly {choice.choose} option{'s' if choice.choose != 1 else ''} for this equipment choice"
            )
        valid_keys = {option.key for option in choice.options}
        if not set(cleaned).issubset(valid_keys):
            raise CreationStateError("Invalid equipment selection")
        self.equipment_choices[choice.key] = cleaned

    def _find_equipment_choice(
        self, choices: Sequence[EquipmentChoice], choice_key: str
    ) -> EquipmentChoice:
        key = choice_key.lower()
        for choice in choices:
            if choice.key == key:
                return choice
        raise CreationStateError("Unknown equipment choice")

    # -- step helpers -------------------------------------------------------
    def needs_ability_scores(self) -> bool:
        return self.base_scores is None

    def needs_race(self) -> bool:
        return self.race_key is None

    def needs_class(self) -> bool:
        return self.class_key is None

    def needs_class_skills(self) -> bool:
        if not self.class_key:
            return False
        required = AVAILABLE_CLASSES[self.class_key].skill_proficiency_options.count
        return required > 0 and len(self.class_skill_choices) != required

    def needs_background(self) -> bool:
        return self.background_key is None

    def needs_equipment(self) -> bool:
        if not self.class_key:
            return False
        character_class = AVAILABLE_CLASSES[self.class_key]
        for choice in character_class.equipment_choices:
            if choice.key not in self.equipment_choices:
                return True
            if len(self.equipment_choices[choice.key]) != choice.choose:
                return True
        return False

    def current_step(self) -> int:
        if self.needs_ability_scores():
            return 1
        if self.needs_race():
            return 2
        if self.needs_class() or self.needs_class_skills():
            return 3
        if self.needs_background():
            return 4
        if self.needs_equipment():
            return 5
        return 6

    def is_ready(self) -> bool:
        return self.current_step() == 6 and self.ability_scores is not None

    # -- finalisation -------------------------------------------------------
    def build_character(
        self,
        *,
        guild_id: int,
        user_id: int,
        name: str,
    ) -> Character:
        if not self.is_ready():
            raise CreationStateError("Complete all steps before finalising the character")
        assert self.base_scores is not None
        assert self.ability_scores is not None
        assert self.race_key is not None
        assert self.class_key is not None
        assert self.background_key is not None
        proficiencies = self._compile_proficiencies()
        equipment = self._compile_equipment()
        return Character(
            guild_id=guild_id,
            user_id=user_id,
            race_key=self.race_key,
            class_key=self.class_key,
            background_key=self.background_key,
            ability_method=self.method,
            base_ability_scores=self.base_scores,
            ability_scores=self.ability_scores,
            racial_bonuses=dict(self.racial_bonuses),
            proficiencies=tuple(proficiencies),
            inventory=tuple(equipment),
            name=name,
        )

    def _compile_proficiencies(self) -> list[str]:
        entries: list[str] = []
        if self.race_key:
            race = AVAILABLE_RACES[self.race_key]
            for grant in race.proficiencies:
                label = f"Race {race.name}: {grant.category.title()} - {grant.name}"
                if label not in entries:
                    entries.append(label)
        if self.class_key:
            character_class = AVAILABLE_CLASSES[self.class_key]
            for armor in character_class.armor_proficiencies:
                label = f"Class {character_class.name}: Armor - {armor}"
                if label not in entries:
                    entries.append(label)
            for weapon in character_class.weapon_proficiencies:
                label = f"Class {character_class.name}: Weapon - {weapon}"
                if label not in entries:
                    entries.append(label)
            for tool in character_class.tool_proficiencies:
                label = f"Class {character_class.name}: Tool - {tool}"
                if label not in entries:
                    entries.append(label)
            for skill in self.class_skill_choices:
                label = f"Class {character_class.name}: Skill - {skill}"
                if label not in entries:
                    entries.append(label)
        if self.background_key:
            background = AVAILABLE_BACKGROUNDS[self.background_key]
            for skill in background.skill_proficiencies:
                label = f"Background {background.name}: Skill - {skill}"
                if label not in entries:
                    entries.append(label)
            for tool in background.tool_proficiencies:
                label = f"Background {background.name}: Tool - {tool}"
                if label not in entries:
                    entries.append(label)
        return entries

    def _compile_equipment(self) -> list[str]:
        items: list[str] = []
        if self.class_key:
            character_class = AVAILABLE_CLASSES[self.class_key]
            for stack in character_class.fixed_equipment:
                items.append(stack.as_label())
            for choice in character_class.equipment_choices:
                selections = self.equipment_choices.get(choice.key, ())
                for option_key in selections:
                    option = self._find_equipment_option(choice, option_key)
                    for stack in option.items:
                        items.append(stack.as_label())
        if self.background_key:
            background = AVAILABLE_BACKGROUNDS[self.background_key]
            for stack in background.equipment:
                items.append(stack.as_label())
        return items

    def _find_equipment_option(self, choice: EquipmentChoice, option_key: str) -> EquipmentChoiceOption:
        key = option_key.lower()
        for option in choice.options:
            if option.key == key:
                return option
        raise CreationStateError("Unknown equipment option")


class SkillSelectionView(discord.ui.View):
    def __init__(
        self,
        creation_view: "CharacterCreationView",
        selection: SkillSelection,
    ) -> None:
        super().__init__(timeout=300)
        self.creation_view = creation_view
        self.selection = selection

        skill_select = discord.ui.Select(
            placeholder=f"Choose {selection.count} class skill(s)",
            min_values=selection.count,
            max_values=selection.count,
            options=[
                discord.SelectOption(
                    label=skill,
                    value=skill,
                    default=skill in self.creation_view.state.class_skill_choices,
                )
                for skill in selection.options
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            try:
                self.creation_view.state.set_class_skills(skill_select.values)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await self.creation_view.refresh()
            await interaction.response.edit_message(
                content="Class skills updated.", view=None
            )

        skill_select.callback = callback  # type: ignore[assignment]
        self.add_item(skill_select)


class EquipmentSelectionView(discord.ui.View):
    def __init__(
        self,
        creation_view: "CharacterCreationView",
        choice: EquipmentChoice,
        *,
        index: int,
    ) -> None:
        super().__init__(timeout=300)
        self.creation_view = creation_view
        self.choice = choice
        self.index = index

        placeholder = f"Equipment choice {index}: select {choice.choose} option(s)"
        current_values = self.creation_view.state.equipment_choices.get(choice.key, ())
        equipment_select = discord.ui.Select(
            placeholder=placeholder,
            min_values=choice.choose,
            max_values=choice.choose,
            options=[
                discord.SelectOption(
                    label=option.name,
                    value=option.key,
                    description=", ".join(
                        stack.as_label() for stack in option.items
                    )[:100],
                    default=option.key in current_values,
                )
                for option in choice.options
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            try:
                self.creation_view.state.set_equipment_choice(
                    self.choice.key, equipment_select.values
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await self.creation_view.refresh()
            await interaction.response.edit_message(
                content=f"Equipment choice {self.index} updated.", view=None
            )

        equipment_select.callback = callback  # type: ignore[assignment]
        self.add_item(equipment_select)


class CharacterCreationView(discord.ui.View):
    def __init__(self, repository: CharacterRepository, user: discord.abc.User) -> None:
        super().__init__(timeout=900)
        self.repository = repository
        self.user = user
        self.message: Optional[discord.Message] = None
        self.state = CreationState()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the user who initiated the creation can interact with this view.",
                ephemeral=True,
            )
            return False
        return True

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.build_embed(), view=self, ephemeral=True)
        self.message = await interaction.original_response()

    async def refresh(self, interaction: Optional[discord.Interaction] = None) -> None:
        self.rebuild_items()
        embed = self.build_embed()
        if interaction is not None:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    def rebuild_items(self) -> None:
        self.clear_items()
        step = self.state.current_step()
        roll_label = "Roll Ability Scores" if self.state.base_scores is None else "Re-roll Ability Scores"
        assign_button = discord.ui.Button(
            label=roll_label,
            style=discord.ButtonStyle.primary,
            row=0,
        )

        async def assign_callback(interaction: discord.Interaction) -> None:
            scores = self.state.roll_scores()
            await self.refresh(interaction)
            summary = ", ".join(f"{ability}: {scores.values[ability]}" for ability in ABILITY_NAMES)
            await interaction.followup.send(
                f"New ability scores: {summary}", ephemeral=True
            )

        assign_button.callback = assign_callback  # type: ignore[assignment]
        assign_button.disabled = False
        self.add_item(assign_button)

        # Race selection
        race_options = [
            discord.SelectOption(
                label=race.name,
                value=race.key,
                description=race.description[:100],
                default=self.state.race_key == race.key,
            )
            for race in AVAILABLE_RACES.values()
        ]
        race_select = discord.ui.Select(
            placeholder="Select a race",
            min_values=1,
            max_values=1,
            options=race_options,
            row=2,
        )

        async def race_callback(interaction: discord.Interaction) -> None:
            self.state.apply_race(race_select.values[0])
            await self.refresh(interaction)

        race_select.callback = race_callback  # type: ignore[assignment]
        race_select.disabled = step < 2
        self.add_item(race_select)

        # Class selection
        class_options = [
            discord.SelectOption(
                label=character_class.name,
                value=character_class.key,
                description=f"Hit Die d{character_class.hit_die}",
                default=self.state.class_key == character_class.key,
            )
            for character_class in AVAILABLE_CLASSES.values()
        ]
        class_select = discord.ui.Select(
            placeholder="Select a class",
            min_values=1,
            max_values=1,
            options=class_options,
            row=3,
        )

        async def class_callback(interaction: discord.Interaction) -> None:
            self.state.set_class(class_select.values[0])
            await self.refresh(interaction)

        class_select.callback = class_callback  # type: ignore[assignment]
        class_select.disabled = step < 3
        self.add_item(class_select)

        # Class skill select
        if self.state.class_key:
            character_class = AVAILABLE_CLASSES[self.state.class_key]
            skill_selection = character_class.skill_proficiency_options
            if skill_selection.count > 0:
                skill_button = discord.ui.Button(
                    label=(
                        f"Select Class Skills "
                        f"({len(self.state.class_skill_choices)}/{skill_selection.count})"
                    ),
                    style=discord.ButtonStyle.secondary,
                    row=1,
                )

                async def skill_callback(
                    interaction: discord.Interaction,
                    selection: SkillSelection = skill_selection,
                ) -> None:
                    await interaction.response.send_message(
                        "Choose class skills:",
                        ephemeral=True,
                        view=SkillSelectionView(self, selection),
                    )

                skill_button.callback = skill_callback  # type: ignore[assignment]
                skill_button.disabled = step < 3
                self.add_item(skill_button)

        # Background selection
        background_options = [
            discord.SelectOption(
                label=background.name,
                value=background.key,
                description=background.description[:100],
                default=self.state.background_key == background.key,
            )
            for background in AVAILABLE_BACKGROUNDS.values()
        ]
        background_select = discord.ui.Select(
            placeholder="Select a background",
            min_values=1,
            max_values=1,
            options=background_options,
            row=4,
        )

        async def background_callback(interaction: discord.Interaction) -> None:
            self.state.set_background(background_select.values[0])
            await self.refresh(interaction)

        background_select.callback = background_callback  # type: ignore[assignment]
        background_select.disabled = step < 4
        self.add_item(background_select)

        # Equipment selection
        if self.state.class_key:
            character_class = AVAILABLE_CLASSES[self.state.class_key]
            for index, choice in enumerate(character_class.equipment_choices, start=1):
                selected = self.state.equipment_choices.get(choice.key, ())
                label = (
                    f"Equipment Choice {index} "
                    f"({len(selected)}/{choice.choose})"
                )
                equipment_button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    row=1,
                )

                async def equipment_callback(
                    interaction: discord.Interaction,
                    choice_obj: EquipmentChoice = choice,
                    idx: int = index,
                ) -> None:
                    await interaction.response.send_message(
                        f"Choose equipment for option {idx}:",
                        ephemeral=True,
                        view=EquipmentSelectionView(self, choice_obj, index=idx),
                    )

                equipment_button.callback = equipment_callback  # type: ignore[assignment]
                equipment_button.disabled = step < 5
                self.add_item(equipment_button)

        # Reset and confirm buttons
        reset_button = discord.ui.Button(
            label="Reset",
            style=discord.ButtonStyle.danger,
            row=0,
        )

        async def reset_callback(interaction: discord.Interaction) -> None:
            self.state = CreationState()
            await self.refresh(interaction)

        reset_button.callback = reset_callback  # type: ignore[assignment]
        self.add_item(reset_button)

        confirm_button = discord.ui.Button(
            label="Confirm Character",
            style=discord.ButtonStyle.success,
            disabled=not self.state.is_ready(),
            row=0,
        )

        async def confirm_callback(interaction: discord.Interaction) -> None:
            await self.handle_confirm(interaction)

        confirm_button.callback = confirm_callback  # type: ignore[assignment]
        self.add_item(confirm_button)

    def build_embed(self) -> discord.Embed:
        step = self.state.current_step()
        title = "D&D Character Creation"
        description = self._build_step_description(step)
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())
        ability_method_label = "Rolled (4d6 drop lowest)"
        embed.add_field(name="Ability Method", value=ability_method_label, inline=True)
        embed.add_field(
            name="Base Ability Scores",
            value=self._format_scores(self.state.base_scores),
            inline=True,
        )
        embed.add_field(
            name="Final Ability Scores",
            value=self._format_scores(self.state.ability_scores),
            inline=True,
        )
        embed.add_field(
            name="Race",
            value=self._format_race_field(),
            inline=False,
        )
        embed.add_field(
            name="Class",
            value=self._format_class_field(),
            inline=False,
        )
        embed.add_field(
            name="Background",
            value=self._format_background_field(),
            inline=False,
        )
        equipment = self.state._compile_equipment() if self.state.class_key else []
        if equipment:
            embed.add_field(
                name="Starting Equipment",
                value="\n".join(equipment),
                inline=False,
            )
        embed.set_footer(text="Confirm once all steps are complete to save your character.")
        return embed

    def _build_step_description(self, step: int) -> str:
        messages = {
            1: "Step 1: Roll your ability scores until you're happy with the results.",
            2: "Step 2: Select a race.",
            3: "Step 3: Select a class and choose the required skill proficiencies.",
            4: "Step 4: Choose a background.",
            5: "Step 5: Pick your starting equipment options.",
            6: "Review your selections and confirm to save your character.",
        }
        return messages.get(step, "")

    def _format_scores(self, scores: AbilityScores | None) -> str:
        if not scores:
            return "Not set"
        return "\n".join(f"{ability}: {scores.values[ability]}" for ability in ABILITY_NAMES)

    def _format_race_field(self) -> str:
        if not self.state.race_key:
            return "Not selected"
        race = AVAILABLE_RACES[self.state.race_key]
        language_value = ", ".join(self.state.race_languages) or "None"
        bonuses = ", ".join(
            f"{bonus.ability}+{bonus.bonus}" for bonus in race.ability_bonuses
        )
        return (
            f"{race.name}\n"
            f"Speed: {race.speed} ft.\n"
            f"Ability Bonuses: {bonuses}\n"
            f"Languages: {language_value}"
        )

    def _format_class_field(self) -> str:
        if not self.state.class_key:
            return "Not selected"
        character_class = AVAILABLE_CLASSES[self.state.class_key]
        skills = ", ".join(self.state.class_skill_choices) or "None"
        return (
            f"{character_class.name}\n"
            f"Hit Die: d{character_class.hit_die}\n"
            f"Class Skills: {skills}"
        )

    def _format_background_field(self) -> str:
        if not self.state.background_key:
            return "Not selected"
        background = AVAILABLE_BACKGROUNDS[self.state.background_key]
        return (
            f"{background.name}\n"
            f"Skills: {', '.join(background.skill_proficiencies) or 'None'}\n"
            f"Tools: {', '.join(background.tool_proficiencies) or 'None'}"
        )

    async def handle_confirm(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return
        if not self.state.is_ready():
            await interaction.response.send_message(
                "Complete all steps before confirming your character.",
                ephemeral=True,
            )
            return
        if await self.repository.exists(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(
                "You already have a character saved. Reset first if you want to recreate it.",
                ephemeral=True,
            )
            return
        character = self.state.build_character(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            name=f"{interaction.user.display_name}'s Adventurer",
        )
        await self.repository.save(character)
        confirmation = discord.Embed(
            title=character.name,
            description="Character saved successfully!",
            colour=discord.Colour.green(),
        )
        confirmation.add_field(name="Race", value=character.race.name, inline=True)
        confirmation.add_field(name="Class", value=character.character_class.name, inline=True)
        confirmation.add_field(
            name="Ability Scores",
            value="\n".join(character.ability_scores.as_lines()),
            inline=False,
        )
        confirmation.add_field(
            name="Proficiencies",
            value="\n".join(character.proficiencies) or "None",
            inline=False,
        )
        confirmation.add_field(
            name="Equipment",
            value="\n".join(character.equipment) or "None",
            inline=False,
        )
        confirmation.set_footer(text="Use /character view to inspect your saved hero.")
        if self.message:
            await self.message.edit(embed=confirmation, view=None)
        await interaction.response.send_message("Character saved successfully!", ephemeral=True)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            timeout_embed = self.build_embed()
            timeout_embed.set_footer(text="Session expired. Run /character create again to restart.")
            await self.message.edit(embed=timeout_embed, view=None)


class CharacterDeleteConfirmation(discord.ui.View):
    """Confirmation dialog for deleting a stored character."""

    def __init__(
        self,
        repository: CharacterRepository,
        *,
        requester_id: int,
        guild_id: int,
        user_id: int,
    ) -> None:
        super().__init__(timeout=60)
        self.repository = repository
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D401
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the player who requested this deletion can respond.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # noqa: D401
        await self.repository.clear(self.guild_id, self.user_id)
        await interaction.response.edit_message(
            content="Your saved character has been deleted.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # noqa: D401
        await interaction.response.edit_message(
            content="Deletion cancelled.",
            view=None,
        )
        self.stop()


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
        view.rebuild_items()
        await view.start(interaction)

    @app_commands.command(name="view", description="View your saved D&D character")
    async def character_view(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "You can only view saved characters from within a server.",
                ephemeral=True,
            )
            return
        character = await self.repository.get(interaction.guild.id, interaction.user.id)
        if not character:
            await interaction.response.send_message(
                "You don't have a saved character yet. Run /character create to begin.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=character.name,
            description="Saved character overview",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Race", value=character.race.name, inline=True)
        embed.add_field(name="Class", value=character.character_class.name, inline=True)
        if character.background:
            embed.add_field(name="Background", value=character.background.name, inline=True)
        embed.add_field(
            name="Ability Method",
            value=character.ability_method.replace("_", " ").title(),
            inline=True,
        )
        embed.add_field(
            name="Base Ability Scores",
            value="\n".join(character.base_ability_scores.as_lines()),
            inline=False,
        )
        if character.racial_bonuses:
            bonuses = ", ".join(
                f"{ability}+{bonus}"
                for ability, bonus in sorted(character.racial_bonuses.items())
            )
            embed.add_field(name="Racial Bonuses", value=bonuses, inline=False)
        embed.add_field(
            name="Final Ability Scores",
            value="\n".join(character.ability_scores.as_lines()),
            inline=False,
        )
        proficiencies = "\n".join(character.proficiencies) or "None"
        embed.add_field(name="Proficiencies", value=proficiencies, inline=False)
        equipment = "\n".join(character.equipment) or "None"
        embed.add_field(name="Equipment", value=equipment, inline=False)
        embed.set_footer(text="Use /character delete if you want to remove this hero.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delete", description="Delete your saved D&D character")
    async def character_delete(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "You can only delete saved characters from within a server.",
                ephemeral=True,
            )
            return

        exists = await self.repository.exists(interaction.guild.id, interaction.user.id)
        if not exists:
            await interaction.response.send_message(
                "You don't have a saved character yet.",
                ephemeral=True,
            )
            return

        view = CharacterDeleteConfirmation(
            self.repository,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            "Are you sure you want to delete your saved character?",
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CharacterCreation(bot))

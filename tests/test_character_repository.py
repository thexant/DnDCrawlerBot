import asyncio
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dnd import AbilityScores, Character, CharacterRepository


def _make_character(*, name: str) -> Character:
    assignments = {
        "STR": 15,
        "DEX": 14,
        "CON": 13,
        "INT": 12,
        "WIS": 10,
        "CHA": 8,
    }
    scores = AbilityScores.from_assignments(assignments, method="standard_array")
    return Character(
        guild_id=123,
        user_id=456,
        race_key="human",
        class_key="fighter",
        background_key="acolyte",
        ability_method="standard_array",
        base_ability_scores=scores,
        ability_scores=scores,
        racial_bonuses={"STR": 1},
        proficiencies=tuple(),
        inventory=tuple(),
        gold_coins=50,
        name=name,
    )


def test_repository_detects_external_updates(tmp_path) -> None:
    async def scenario() -> None:
        storage = tmp_path / "characters.json"
        repo_one = CharacterRepository(storage)
        repo_two = CharacterRepository(storage)

        original = _make_character(name="Hero")

        assert not await repo_two.exists(original.guild_id, original.user_id)

        await repo_one.save(original)

        assert await repo_two.exists(original.guild_id, original.user_id)
        assert await repo_two.get(original.guild_id, original.user_id) == original

        characters = await repo_two.list_guild_characters(original.guild_id)
        assert characters == {original.user_id: original}

        await repo_one.clear(original.guild_id, original.user_id)

        assert not await repo_two.exists(original.guild_id, original.user_id)
        assert await repo_two.list_guild_characters(original.guild_id) == {}

        recreated = replace(original, name="Returned Hero")
        await repo_two.save(recreated)

        assert await repo_one.exists(recreated.guild_id, recreated.user_id)
        assert await repo_one.get(recreated.guild_id, recreated.user_id) == recreated

    asyncio.run(scenario())

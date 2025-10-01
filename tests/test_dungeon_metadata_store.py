import asyncio
import json
from pathlib import Path

from dnd.dungeon.state import DungeonMetadataStore


def test_record_multiple_dungeons(tmp_path: Path) -> None:
    store = DungeonMetadataStore(tmp_path / "metadata.json")

    async def run() -> None:
        await store.record_session(
            123,
            theme="crypts",
            seed=111,
            difficulty="hard",
            name="Alpha Run",
        )
        await store.record_session(
            123,
            theme="catacombs",
            seed=222,
            difficulty="easy",
            name="Bravo Run",
        )

        names = await store.list_dungeon_names(123)
        assert names == ("Alpha Run", "Bravo Run")

        stored = await store.get_dungeon(123, "alpha run")
        assert stored is not None
        assert stored.theme == "crypts"
        assert stored.seed == 111
        assert stored.difficulty == "hard"

    asyncio.run(run())

    # Ensure data persisted to disk
    metadata_file = tmp_path / "metadata.json"
    assert json.loads(metadata_file.read_text()) == {
        "123": {
            "dungeons": {
                "Alpha Run": {"difficulty": "hard", "seed": 111, "theme": "crypts"},
                "Bravo Run": {"difficulty": "easy", "seed": 222, "theme": "catacombs"},
            },
            "last_difficulty": "easy",
            "last_name": "Bravo Run",
            "last_seed": 222,
            "last_theme": "catacombs",
        }
    }


def test_delete_dungeon(tmp_path: Path) -> None:
    store = DungeonMetadataStore(tmp_path / "metadata.json")

    async def run() -> None:
        await store.record_session(
            55,
            theme="depths",
            seed=987,
            difficulty="standard",
            name="Weekly Crawl",
        )

        deleted = await store.delete_dungeon(55, "weekly crawl")
        assert deleted is True

        names = await store.list_dungeon_names(55)
        assert names == ()

        stored = await store.get_dungeon(55, "Weekly Crawl")
        assert stored is None

    asyncio.run(run())

    metadata_file = tmp_path / "metadata.json"
    assert json.loads(metadata_file.read_text()) == {
        "55": {
            "last_difficulty": "standard",
            "last_seed": 987,
            "last_theme": "depths",
        }
    }


def test_delete_nonexistent_dungeon(tmp_path: Path) -> None:
    store = DungeonMetadataStore(tmp_path / "metadata.json")

    async def run() -> None:
        await store.record_session(
            77,
            theme="vault",
            seed=1,
            difficulty="standard",
            name="Emerald Vault",
        )

        deleted = await store.delete_dungeon(77, "Unknown")
        assert deleted is False

        names = await store.list_dungeon_names(77)
        assert names == ("Emerald Vault",)

    asyncio.run(run())

import asyncio
import json
from pathlib import Path

from dnd.dungeon.generator import DIFFICULTY_PROFILES
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
            room_count=7,
        )
        await store.record_session(
            123,
            theme="catacombs",
            seed=222,
            difficulty="easy",
            name="Bravo Run",
            room_count=4,
        )

        names = await store.list_dungeon_names(123)
        assert names == ("Alpha Run", "Bravo Run")

        stored = await store.get_dungeon(123, "alpha run")
        assert stored is not None
        assert stored.theme == "crypts"
        assert stored.seed == 111
        assert stored.difficulty == "hard"
        assert stored.room_count == 7

        listed = await store.list_dungeons(123)
        assert [dungeon.name for dungeon in listed] == ["Alpha Run", "Bravo Run"]
        assert listed[1].room_count == 4

    asyncio.run(run())

    # Ensure data persisted to disk
    metadata_file = tmp_path / "metadata.json"
    assert json.loads(metadata_file.read_text()) == {
        "123": {
            "dungeons": {
                "Alpha Run": {
                    "difficulty": "hard",
                    "room_count": 7,
                    "seed": 111,
                    "theme": "crypts",
                },
                "Bravo Run": {
                    "difficulty": "easy",
                    "room_count": 4,
                    "seed": 222,
                    "theme": "catacombs",
                },
            },
            "last_difficulty": "easy",
            "last_name": "Bravo Run",
            "last_room_count": 4,
            "last_seed": 222,
            "last_theme": "catacombs",
        }
    }


def test_record_all_difficulties(tmp_path: Path) -> None:
    store = DungeonMetadataStore(tmp_path / "metadata.json")

    async def run() -> None:
        for index, difficulty in enumerate(DIFFICULTY_PROFILES.keys(), start=1):
            await store.record_session(
                321,
                theme=f"theme-{index}",
                seed=index * 10,
                difficulty=difficulty,
                name=f"Run {index}",
                room_count=3 + index,
            )

        stored = await store.list_dungeons(321)
        assert [dungeon.difficulty for dungeon in stored] == list(
            DIFFICULTY_PROFILES.keys()
        )

    asyncio.run(run())

    metadata_file = tmp_path / "metadata.json"
    payload = json.loads(metadata_file.read_text())
    assert payload["321"]["last_difficulty"] == list(DIFFICULTY_PROFILES.keys())[-1]
    assert set(payload["321"]["dungeons"].keys()) == {
        f"Run {index}" for index in range(1, len(DIFFICULTY_PROFILES) + 1)
    }
    assert {
        entry["difficulty"]
        for entry in payload["321"]["dungeons"].values()
    } == set(DIFFICULTY_PROFILES.keys())


def test_delete_dungeon(tmp_path: Path) -> None:
    store = DungeonMetadataStore(tmp_path / "metadata.json")

    async def run() -> None:
        await store.record_session(
            55,
            theme="depths",
            seed=987,
            difficulty="standard",
            name="Weekly Crawl",
            room_count=6,
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
            room_count=10,
        )

        deleted = await store.delete_dungeon(77, "Unknown")
        assert deleted is False

        names = await store.list_dungeon_names(77)
        assert names == ("Emerald Vault",)

    asyncio.run(run())

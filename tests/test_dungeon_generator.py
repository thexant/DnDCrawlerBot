from __future__ import annotations

from pathlib import Path
from statistics import mean
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dnd.content.loader import ContentLibrary
from dnd.content.models import Theme
from dnd.dungeon.generator import DIFFICULTY_PROFILES, DungeonGenerator


@pytest.fixture(scope="module")
def arcane_theme() -> Theme:
    library = ContentLibrary.load_from_path(Path("data"))
    return library.themes.get("arcane_ruins")


def _avg(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _rarity_score(rarity: str) -> int:
    order = {
        "common": 0,
        "uncommon": 1,
        "rare": 2,
        "very rare": 3,
        "legendary": 4,
        "artifact": 5,
    }
    return order.get(rarity.strip().lower(), 0)


def _trap_dc(trap) -> float:
    saving_throw = trap.saving_throw or {}
    dc = saving_throw.get("dc") if isinstance(saving_throw, dict) else None
    if isinstance(dc, (int, float)):
        return float(dc)
    return 10.0


def test_dungeon_records_difficulty(arcane_theme: Theme) -> None:
    generator = DungeonGenerator(arcane_theme, seed=42, difficulty="hard")
    dungeon = generator.generate(room_count=2)
    assert dungeon.difficulty == "hard"

    for difficulty in DIFFICULTY_PROFILES:
        override = generator.generate(room_count=1, difficulty=difficulty)
        assert override.difficulty == difficulty


def test_combat_scaling_by_difficulty(arcane_theme: Theme) -> None:
    difficulties = list(DIFFICULTY_PROFILES.keys())
    challenge_avgs: list[float] = []

    for diff in difficulties:
        profile = DIFFICULTY_PROFILES[diff]
        counts: list[int] = []
        challenges: list[float] = []
        for seed in range(10):
            generator = DungeonGenerator(arcane_theme, seed=seed, difficulty=diff)
            encounter = generator._build_encounter("combat", diff)
            counts.append(len(encounter.monsters))
            if encounter.monsters:
                challenges.append(
                    sum(monster.challenge for monster in encounter.monsters)
                    / len(encounter.monsters)
                )
        assert all(
            profile.monster_count[0] <= count <= profile.monster_count[1]
            for count in counts
        )
        challenge_avgs.append(_avg(challenges))

    assert challenge_avgs == sorted(challenge_avgs)
    assert challenge_avgs[0] < challenge_avgs[-1]


def test_trap_scaling_by_difficulty(arcane_theme: Theme) -> None:
    difficulties = list(DIFFICULTY_PROFILES.keys())
    average_counts: list[float] = []
    average_dcs: list[float] = []

    for diff in difficulties:
        profile = DIFFICULTY_PROFILES[diff]
        counts: list[int] = []
        dcs: list[float] = []
        for seed in range(10, 20):
            generator = DungeonGenerator(arcane_theme, seed=seed, difficulty=diff)
            encounter = generator._build_encounter("trap", diff)
            counts.append(len(encounter.traps))
            dcs.extend(_trap_dc(trap) for trap in encounter.traps)
        assert all(
            profile.trap_count[0] <= count <= profile.trap_count[1]
            for count in counts
        )
        average_counts.append(_avg([float(count) for count in counts]))
        average_dcs.append(_avg(dcs))

    assert average_counts == sorted(average_counts)
    assert average_counts[0] < average_counts[-1]
    assert average_dcs == sorted(average_dcs)
    assert average_dcs[0] < average_dcs[-1]


def test_treasure_scaling_by_difficulty(arcane_theme: Theme) -> None:
    difficulties = list(DIFFICULTY_PROFILES.keys())
    average_counts: list[float] = []
    average_rarities: list[float] = []

    for diff in difficulties:
        profile = DIFFICULTY_PROFILES[diff]
        counts: list[int] = []
        rarity_values: list[int] = []
        for seed in range(30, 40):
            generator = DungeonGenerator(arcane_theme, seed=seed, difficulty=diff)
            encounter = generator._build_encounter("treasure", diff)
            counts.append(len(encounter.loot))
            rarity_values.extend(_rarity_score(item.rarity) for item in encounter.loot)
        assert all(
            profile.loot_treasure[0] <= count <= profile.loot_treasure[1]
            for count in counts
        )
        average_counts.append(_avg([float(count) for count in counts]))
        average_rarities.append(_avg([float(value) for value in rarity_values]))

    assert average_counts == sorted(average_counts)
    assert average_counts[0] < average_counts[-1]
    assert average_rarities == sorted(average_rarities)
    assert average_rarities[0] < average_rarities[-1]


def test_sparse_theme_gracefully_degrades(arcane_theme: Theme) -> None:
    sparse_theme = Theme(
        key="sparse",
        name="Sparse",
        description="",
        room_templates=arcane_theme.room_templates[:1],
        monsters=(arcane_theme.monsters[0],),
        traps=(arcane_theme.traps[0],),
        loot=(arcane_theme.loot[0],),
        encounter_table=arcane_theme.encounter_table,
    )
    generator = DungeonGenerator(sparse_theme, seed=55, difficulty="deadly")
    combat = generator._build_encounter("combat", "deadly")
    trap = generator._build_encounter("trap", "deadly")
    treasure = generator._build_encounter("treasure", "deadly")

    assert len(combat.monsters) >= 1
    assert len(trap.traps) >= 1
    assert len(treasure.loot) >= 1


def test_empty_theme_gracefully_degrades(arcane_theme: Theme) -> None:
    empty_theme = Theme(
        key="empty",
        name="Empty",
        description="",
        room_templates=arcane_theme.room_templates[:1],
        monsters=(),
        traps=(),
        loot=(),
        encounter_table=arcane_theme.encounter_table,
    )
    generator = DungeonGenerator(empty_theme, seed=99, difficulty="deadly")

    combat = generator._build_encounter("combat", "deadly")
    trap = generator._build_encounter("trap", "deadly")
    treasure = generator._build_encounter("treasure", "deadly")

    assert combat.monsters == ()
    assert trap.traps == ()
    assert treasure.loot == ()

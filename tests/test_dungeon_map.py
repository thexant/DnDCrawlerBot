import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.dungeon import DungeonCog, DungeonSession
from dnd.content.models import EncounterTable, Theme
from dnd.dungeon.generator import Dungeon, EncounterResult, Room


@pytest.fixture()
def simple_session() -> DungeonSession:
    empty_encounter = EncounterResult(kind="empty", summary="Quiet chamber.")
    room_a = Room(id=0, name="Alpha", description="", encounter=empty_encounter)
    room_b = Room(id=1, name="Beta", description="", encounter=empty_encounter)
    positions = {0: (0, 0), 1: (1, 0)}
    room_a.position = positions[0]
    room_b.position = positions[1]

    theme = Theme(
        key="test",
        name="Test Theme",
        description="",
        room_templates=(),
        monsters=(),
        traps=(),
        loot=(),
        encounter_table=EncounterTable({"empty": 1}),
    )
    dungeon = Dungeon(
        name="Test Dungeon",
        seed=None,
        theme=theme,
        difficulty="standard",
        rooms=(room_a, room_b),
        corridors=(),
        room_positions=positions,
    )
    return DungeonSession(dungeon=dungeon, guild_id=None, channel_id=1)


def test_map_highlights_current_room(simple_session: DungeonSession) -> None:
    cog = DungeonCog.__new__(DungeonCog)
    simple_session.current_room = 0
    first_map = cog._build_map_string(simple_session)
    assert "[01]" in first_map
    assert " 02" in first_map

    simple_session.current_room = 1
    second_map = cog._build_map_string(simple_session)
    assert "[02]" in second_map
    assert " 01" in second_map
    assert first_map != second_map


def test_session_embeds_include_map_first(simple_session: DungeonSession) -> None:
    cog = DungeonCog.__new__(DungeonCog)
    embeds = cog._build_session_embeds(simple_session)
    assert embeds
    assert embeds[0].title == "Dungeon Map"
    assert embeds[0].description is not None
    assert "```" in embeds[0].description
    assert any(embed.title and "Room" in embed.title for embed in embeds[1:])

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs import dungeon as dungeon_module
from cogs.dungeon import DungeonCog, DungeonSession
from dnd.content.models import EncounterTable, Theme, Trap
from dnd.dungeon.generator import Dungeon, EncounterResult, Room, RoomExit
from dnd.sessions import SessionManager


class DummyResponse:
    def __init__(self) -> None:
        self._done = False

    async def defer(self, *_, **__) -> None:
        self._done = True

    async def send_message(self, *_args, **_kwargs) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done


class DummyFollowup:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, message: str, **_kwargs) -> None:
        self.sent_messages.append(message)

    async def edit_message(self, *args, **kwargs) -> None:  # pragma: no cover - stub
        return None


class DummyInteraction:
    def __init__(self, *, channel_id: int = 1, user_id: int = 100) -> None:
        self.guild_id = None
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=user_id)
        self.response = DummyResponse()
        self.followup = DummyFollowup()
        self.guild = None


def _make_cog(monkeypatch: pytest.MonkeyPatch) -> DungeonCog:
    cog = DungeonCog.__new__(DungeonCog)
    cog.sessions = SessionManager()
    cog.bot = SimpleNamespace(add_view=lambda *args, **kwargs: None, get_user=lambda _uid: None)

    async def noop_refresh(_interaction, _session) -> None:
        return None

    async def noop_membership_change(_guild_id, _session) -> None:
        return None

    cog._refresh_session_message = noop_refresh  # type: ignore[assignment]
    cog._handle_party_membership_change = noop_membership_change  # type: ignore[assignment]
    return cog


def _make_trap_session(user_id: int = 100) -> DungeonSession:
    trap = Trap(
        key="pit",
        name="Hidden Pit",
        description="A concealed pit trap.",
        saving_throw={"ability": "DEX", "dc": 13},
        damage="2d6 bludgeoning",
    )
    trap_encounter = EncounterResult(
        kind="trap",
        summary="A precarious hazard lurks here.",
        traps=(trap,),
        loot=(),
        monsters=(),
    )
    exits = (RoomExit(key="forward", label="Forward", destination=0),)
    room = Room(id=0, name="Trap Room", description="", encounter=trap_encounter, exits=exits)
    theme = Theme(
        key="test",
        name="Test",
        description="",
        room_templates=(),
        monsters=(),
        traps=(trap,),
        loot=(),
        encounter_table=EncounterTable({"trap": 1}),
    )
    dungeon = Dungeon(
        name="Test Dungeon",
        seed=None,
        theme=theme,
        difficulty="standard",
        rooms=[room],
        corridors=(),
    )
    session = DungeonSession(dungeon=dungeon, guild_id=None, channel_id=1)
    session.party_ids.add(user_id)
    return session


def test_trap_hidden_until_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    cog._ensure_room_trap_state(session, session.room)

    embed = cog._build_room_embed(None, session)
    assert all(field.name != "Traps" for field in embed.fields)

    trap = session.room.encounter.traps[0]
    room_id = session.room.id
    session.trap_catalog.setdefault(room_id, {})[trap.key] = trap
    session.trap_states.setdefault(room_id, {})[trap.key] = "discovered"

    detected_embed = cog._build_room_embed(None, session)
    trap_field = next(field for field in detected_embed.fields if field.name == "Traps")
    assert trap.name in trap_field.value
    assert "detected" in trap_field.value

    session.trap_states[room_id][trap.key] = "sprung"
    session.room.encounter = replace(session.room.encounter, traps=())

    sprung_embed = cog._build_room_embed(None, session)
    sprung_field = next(field for field in sprung_embed.fields if field.name == "Traps")
    assert "sprung" in sprung_field.value


def test_failed_disarm_triggers_damage(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)
    trap_key = session.room.encounter.traps[0].key

    async def runner() -> None:
        await cog.sessions.set(key, session)

        results = iter(
            [
                dungeon_module.SavingThrowResult(total=5, roll=5, natural=5, success=False),
                dungeon_module.SavingThrowResult(total=8, roll=8, natural=8, success=False),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            return next(results)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)
        monkeypatch.setattr(cog, "_roll_damage", lambda *_args, **_kwargs: 6)

        await cog.handle_disarm(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    trap_key = "pit"
    assert session.trap_states[room_id][trap_key] == "sprung"
    assert not session.room.encounter.traps
    assert interaction.followup.sent_messages
    last_message = interaction.followup.sent_messages[-1]
    assert "springs" in last_message
    assert "damage" in last_message


def test_successful_detection_and_disarm_updates_state(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)
    trap_key = session.room.encounter.traps[0].key

    async def runner() -> None:
        await cog.sessions.set(key, session)

        results = iter(
            [
                dungeon_module.SavingThrowResult(total=18, roll=18, natural=18, success=True),
                dungeon_module.SavingThrowResult(total=19, roll=19, natural=19, success=True),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            return next(results)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)

        await cog.handle_disarm(interaction)
        await cog.handle_disarm(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    assert session.trap_states[room_id][trap_key] == "disarmed"
    assert not session.room.encounter.traps
    assert interaction.followup.sent_messages
    assert "uncover" in interaction.followup.sent_messages[0]
    assert "disarm" in interaction.followup.sent_messages[-1]

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs import dungeon as dungeon_module
from cogs.dungeon import CombatState, DungeonCog, DungeonSession
from dnd.content.models import EncounterTable, Monster, Theme
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


def _make_session(user_id: int = 100) -> DungeonSession:
    monster = Monster(
        key="goblin",
        name="Goblin",
        challenge=0.25,
        armor_class=13,
        hit_points=7,
        attack_bonus=4,
        damage="1d6+2",
        ability_scores={"WIS": 10},
        tags=(),
    )
    empty_encounter = EncounterResult(kind="none", summary="", monsters=(), traps=(), loot=())
    combat_encounter = EncounterResult(
        kind="combat",
        summary="A lurking goblin.",
        monsters=(monster,),
        traps=(),
        loot=(),
    )
    exits = (RoomExit(key="forward", label="Forward", destination=1),)
    room_start = Room(id=0, name="Entry", description="", encounter=empty_encounter, exits=exits)
    room_second = Room(id=1, name="Lair", description="", encounter=combat_encounter, exits=())
    theme = Theme(
        key="test",
        name="Test",
        description="",
        room_templates=(),
        monsters=(),
        traps=(),
        loot=(),
        encounter_table=EncounterTable({"none": 1}),
    )
    dungeon = Dungeon(
        name="Test Dungeon",
        seed=None,
        theme=theme,
        difficulty="standard",
        rooms=[room_start, room_second],
        corridors=(),
    )
    session = DungeonSession(dungeon=dungeon, guild_id=None, channel_id=1)
    session.party_ids.add(user_id)
    return session


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


def test_handle_exit_stealth_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        cog = _make_cog(monkeypatch)
        session = _make_session()
        interaction = DummyInteraction()
        key = cog._session_key(interaction.guild_id, interaction.channel_id)
        await cog.sessions.set(key, session)

        async def fake_attempt(_interaction, _session, _party):
            return True, "The party remains hidden (Stealth 18 — roll 15+3 vs passive Goblin 10)."

        monkeypatch.setattr(cog, "_attempt_room_stealth", fake_attempt)

        await cog.handle_exit(interaction, "forward")

        assert session.current_room == 1
        assert session.stealthed is True
        assert session.combat_state is None
        assert interaction.followup.sent_messages
        assert "remains hidden" in interaction.followup.sent_messages[-1]

    asyncio.run(runner())


def test_handle_exit_stealth_failure_triggers_combat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        cog = _make_cog(monkeypatch)
        session = _make_session()
        interaction = DummyInteraction()
        key = cog._session_key(interaction.guild_id, interaction.channel_id)
        await cog.sessions.set(key, session)

        async def fake_attempt(_interaction, _session, _party):
            return False, "The monsters spot the party (Stealth 9 — roll 6+3 vs passive Goblin 10)."

        combat_state = CombatState()

        async def fake_build_state(_interaction, _session, _party):
            return combat_state

        run_calls: list[tuple[DungeonSession, CombatState]] = []

        def fake_run_turns(run_session: DungeonSession, state: CombatState) -> None:
            run_calls.append((run_session, state))

        monkeypatch.setattr(cog, "_attempt_room_stealth", fake_attempt)
        monkeypatch.setattr(cog, "_build_combat_state", fake_build_state)
        monkeypatch.setattr(cog, "_run_automatic_turns", fake_run_turns)

        await cog.handle_exit(interaction, "forward")

        assert session.current_room == 1
        assert session.stealthed is False
        assert session.combat_state is combat_state
        assert run_calls == [(session, combat_state)]
        assert interaction.followup.sent_messages
        last_message = interaction.followup.sent_messages[-1]
        assert "spot the party" in last_message
        assert "Initiative is rolled" in last_message

    asyncio.run(runner())


def test_attempt_room_stealth_uses_modifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        cog = _make_cog(monkeypatch)
        session = _make_session()
        session.guild_id = 42
        session.current_room = 1
        session.breadcrumbs = [0, 1]
        interaction = DummyInteraction()

        async def fake_load_party_characters(_guild_id, party_ids):
            return {party_ids[0]: SimpleNamespace(ability_scores=SimpleNamespace(values={"DEX": 16}))}

        rolls = iter([12])
        monkeypatch.setattr(dungeon_module.random, "randint", lambda *_args, **_kwargs: next(rolls))
        monkeypatch.setattr(cog, "_load_party_characters", fake_load_party_characters)

        success, summary = await cog._attempt_room_stealth(
            interaction, session, tuple(session.party_ids)
        )

        assert success is True
        assert "Stealth" in summary
        assert "passive Goblin 10" in summary
        assert "remains hidden" in summary

    asyncio.run(runner())

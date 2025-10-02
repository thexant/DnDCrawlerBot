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
from dnd.content import Item
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


def _make_trap_session(
    user_id: int = 100,
    *,
    traps: tuple[Trap, ...] | None = None,
    loot: tuple[Item, ...] = (),
    exits: tuple[RoomExit, ...] | None = None,
) -> DungeonSession:
    default_trap = Trap(
        key="pit",
        name="Hidden Pit",
        description="A concealed pit trap.",
        saving_throw={"ability": "DEX", "dc": 13},
        damage="2d6 bludgeoning",
    )
    trap_pool = traps if traps is not None else (default_trap,)
    exit_pool = exits if exits is not None else (RoomExit(key="forward", label="Forward", destination=0),)
    trap_encounter = EncounterResult(
        kind="trap",
        summary="A precarious hazard lurks here.",
        traps=trap_pool,
        loot=loot,
        monsters=(),
    )
    room = Room(id=0, name="Trap Room", description="", encounter=trap_encounter, exits=exit_pool)
    theme = Theme(
        key="test",
        name="Test",
        description="",
        room_templates=(),
        monsters=(),
        traps=trap_pool or (default_trap,),
        loot=loot,
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


def _find_field(embed, name: str):
    for field in embed.fields:
        if field.name == name:
            return field
    return None


def test_starting_room_reveals_at_least_one_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    exits = (
        RoomExit(key="forward", label="Forward", destination=0),
        RoomExit(key="secret", label="Secret Passage", destination=0),
    )
    session = _make_trap_session(exits=exits)

    cog._ensure_room_discovery_state(session, session.room)

    discovered = session.discovered_exits.get(session.room.id, set())
    assert discovered, "Starting room should reveal at least one exit"
    assert exits[0].key in discovered, "First exit should be visible by default"


def test_non_starting_room_exits_remain_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)

    empty_encounter = EncounterResult(kind="empty", summary="Quiet chamber.")
    start_exit = RoomExit(key="start-forward", label="Forward", destination=1)
    return_exit = RoomExit(key="return", label="Back", destination=0)

    start_room = Room(id=0, name="Foyer", description="", encounter=empty_encounter, exits=(start_exit,))
    second_room = Room(id=1, name="Chamber", description="", encounter=empty_encounter, exits=(return_exit,))

    theme = Theme(
        key="test",
        name="Test",
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
        rooms=(start_room, second_room),
        corridors=(),
    )

    session = DungeonSession(dungeon=dungeon, guild_id=None, channel_id=1)
    session.current_room = 1

    cog._ensure_room_discovery_state(session, session.room)

    discovered = session.discovered_exits.get(session.room.id, set())
    assert not discovered, "Non-starting rooms should keep exits hidden until discovered"


def test_trap_hidden_until_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    cog._ensure_room_trap_state(session, session.room)
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )

    embed = cog._build_room_embed(None, session)
    assert _find_field(embed, "Traps") is None

    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)

    async def runner() -> None:
        await cog.sessions.set(key, session)
        results = iter(
            [
                dungeon_module.SavingThrowResult(total=18, roll=18, natural=18, success=True),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)
        await cog.handle_perception(interaction)

    asyncio.run(runner())

    detected_embed = cog._build_room_embed(None, session)
    trap_field = _find_field(detected_embed, "Traps")
    assert trap_field is not None
    assert "detected" in trap_field.value

    trap = session.room.encounter.traps[0]
    room_id = session.room.id
    session.trap_states.setdefault(room_id, {})[trap.key] = "sprung"
    session.room.encounter = replace(session.room.encounter, traps=())

    sprung_embed = cog._build_room_embed(None, session)
    sprung_field = _find_field(sprung_embed, "Traps")
    assert sprung_field is not None
    assert "sprung" in sprung_field.value


def test_failed_disarm_triggers_damage(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)

    async def runner() -> None:
        await cog.sessions.set(key, session)

        results = iter(
            [
                dungeon_module.SavingThrowResult(total=18, roll=18, natural=18, success=True),
                dungeon_module.SavingThrowResult(total=5, roll=5, natural=5, success=False),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)
        monkeypatch.setattr(cog, "_roll_damage", lambda *_args, **_kwargs: 6)

        await cog.handle_perception(interaction)
        await cog.handle_disarm(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    trap_key = "pit"
    assert session.trap_states[room_id][trap_key] == "sprung"
    assert not session.room.encounter.traps
    assert interaction.followup.sent_messages
    last_message = interaction.followup.sent_messages[-1]
    assert "sprung" in last_message
    assert "damage" in last_message


def test_failed_perception_keeps_trap_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)
    user_id = interaction.user.id

    async def runner() -> None:
        await cog.sessions.set(key, session)

        results = iter(
            [
                dungeon_module.SavingThrowResult(total=4, roll=4, natural=4, success=False),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)

        await cog.handle_perception(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    trap_key = session.room.encounter.traps[0].key
    assert session.trap_states[room_id][trap_key] == "hidden"
    assert session.room.encounter.traps
    attempts = session.perception_attempts[room_id][user_id]
    assert attempts == 1
    assert interaction.followup.sent_messages
    message = interaction.followup.sent_messages[-1]
    assert "fail to spot" in message


def test_perception_attempt_limit_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)
    user_id = interaction.user.id

    async def runner() -> None:
        await cog.sessions.set(key, session)

        results = iter(
            [
                dungeon_module.SavingThrowResult(total=6, roll=6, natural=6, success=False),
                dungeon_module.SavingThrowResult(total=7, roll=7, natural=7, success=False),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)

        await cog.handle_perception(interaction)
        await cog.handle_perception(interaction)
        await cog.handle_perception(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    trap_key = session.room.encounter.traps[0].key
    attempts = session.perception_attempts[room_id][user_id]
    assert attempts == dungeon_module.MAX_TRAP_DETECTION_ATTEMPTS
    assert session.trap_states[room_id][trap_key] == "hidden"
    assert session.room.encounter.traps
    assert interaction.followup.sent_messages
    assert "scoured the chamber" in interaction.followup.sent_messages[-1]


def test_successful_detection_and_disarm_updates_state(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )
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
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)

        await cog.handle_perception(interaction)
        await cog.handle_disarm(interaction)

    asyncio.run(runner())

    room_id = session.room.id
    assert session.trap_states[room_id][trap_key] == "disarmed"
    assert not session.room.encounter.traps
    assert interaction.followup.sent_messages
    assert "uncover" in interaction.followup.sent_messages[0]
    assert "disarm" in interaction.followup.sent_messages[-1]


def test_loot_hidden_until_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    loot_item = Item(key="amulet", name="Jeweled Amulet", rarity="Rare")
    cog = _make_cog(monkeypatch)
    session = _make_trap_session(traps=(), loot=(loot_item,))
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)
    session.discovered_exits.setdefault(session.room.id, set()).update(
        exit_option.key for exit_option in session.room.exits
    )

    embed = cog._build_room_embed(None, session)
    assert _find_field(embed, "Loot") is None

    async def runner() -> None:
        await cog.sessions.set(key, session)
        await cog.handle_search(interaction)

    asyncio.run(runner())

    assert session.room.encounter.loot == (loot_item,)
    assert "nothing of value" in interaction.followup.sent_messages[-1]

    interaction.followup.sent_messages.clear()

    async def discover_and_search() -> None:
        await cog.sessions.set(key, session)
        results = iter(
            [
                dungeon_module.SavingThrowResult(total=16, roll=16, natural=16, success=True),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)

        await cog.handle_perception(interaction)
        await cog.handle_search(interaction)

    asyncio.run(discover_and_search())

    embed_after = cog._build_room_embed(None, session)
    loot_field = _find_field(embed_after, "Loot")
    assert loot_field is not None
    assert loot_item.name in loot_field.value
    assert session.room.encounter.loot == (loot_item,)
    assert any("guild roster" in message for message in interaction.followup.sent_messages)


def test_exit_hidden_until_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_exit = RoomExit(key="secret", label="Secret Door", destination=0)
    cog = _make_cog(monkeypatch)
    session = _make_trap_session(traps=(), loot=(), exits=(secret_exit,))
    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)

    embed = cog._build_room_embed(None, session)
    exit_field = _find_field(embed, "Exits")
    assert exit_field is not None
    assert "No obvious exits" in exit_field.value

    async def _build_view(target_session: DungeonSession):
        return dungeon_module.DungeonNavigationView(cog, target_session)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        view = loop.run_until_complete(_build_view(session))
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    exit_custom_id = f"dungeon:exit:{session.channel_id}:{secret_exit.key}"
    assert all(
        getattr(item, "custom_id", None) != exit_custom_id for item in view.children
    )

    async def runner() -> None:
        await cog.sessions.set(key, session)
        results = iter(
            [
                dungeon_module.SavingThrowResult(total=17, roll=17, natural=17, success=True),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)
        await cog.handle_perception(interaction)

    asyncio.run(runner())

    updated_session = asyncio.run(cog.sessions.get(key))
    assert updated_session is not None

    embed_after = cog._build_room_embed(None, updated_session)
    exit_field_after = _find_field(embed_after, "Exits")
    assert exit_field_after is not None
    assert "Secret Door" in exit_field_after.value

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        view_after = loop.run_until_complete(_build_view(updated_session))
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert any(
        getattr(item, "custom_id", None) == exit_custom_id for item in view_after.children
    )


def test_disarm_button_requires_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = _make_cog(monkeypatch)
    session = _make_trap_session()

    async def _build_view(target_session: DungeonSession):
        return dungeon_module.DungeonNavigationView(cog, target_session)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        view = loop.run_until_complete(_build_view(session))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    disarm_button = next(
        item for item in view.children if getattr(item, "custom_id", None) == "dungeon:disarm"
    )
    assert disarm_button.disabled

    interaction = DummyInteraction()
    key = cog._session_key(interaction.guild_id, interaction.channel_id)

    async def runner() -> None:
        await cog.sessions.set(key, session)
        results = iter(
            [
                dungeon_module.SavingThrowResult(total=18, roll=18, natural=18, success=True),
            ]
        )

        def fake_saving_throw(*_args, **_kwargs):
            try:
                return next(results)
            except StopIteration:
                return dungeon_module.SavingThrowResult(total=1, roll=1, natural=1, success=False)

        monkeypatch.setattr(dungeon_module, "saving_throw", fake_saving_throw)
        await cog.handle_perception(interaction)

    asyncio.run(runner())

    updated_session = asyncio.run(cog.sessions.get(key))
    assert updated_session is not None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        view_after = loop.run_until_complete(_build_view(updated_session))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    disarm_button_after = next(
        item for item in view_after.children if getattr(item, "custom_id", None) == "dungeon:disarm"
    )
    assert not disarm_button_after.disabled

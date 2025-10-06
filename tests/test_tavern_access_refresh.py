"""Coverage for tavern access refresh hooks."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.character_creation import DeletionConfirmationView
from cogs import dungeon


def test_deletion_confirmation_refreshes_tavern_access() -> None:
    class DummyRepository:
        def __init__(self) -> None:
            self.cleared: list[tuple[int, int]] = []

        async def clear(self, guild_id: int, user_id: int) -> None:
            self.cleared.append((guild_id, user_id))

    class DummyTavern:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def refresh_tavern_access(self, guild_id: int) -> None:
            self.calls.append(guild_id)

    class DummyClient:
        def __init__(self, tavern: DummyTavern | None) -> None:
            self._tavern = tavern

        def get_cog(self, name: str):  # type: ignore[override]
            return self._tavern if name == "Tavern" else None

    class DummyResponse:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        async def edit_message(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class DummyInteraction:
        def __init__(self, client: DummyClient) -> None:
            self.client = client
            self.guild_id = 123
            self.response = DummyResponse()

    repository = DummyRepository()
    tavern = DummyTavern()
    interaction = DummyInteraction(DummyClient(tavern))

    view = DeletionConfirmationView.__new__(DeletionConfirmationView)
    view.repository = repository
    view.requester_id = 1
    view.guild_id = 123
    view.user_id = 456
    view.stop = lambda: None  # type: ignore[assignment]

    async def runner() -> None:
        await view.confirm(interaction, None)

    asyncio.run(runner())

    assert repository.cleared == [(123, 456)]
    assert tavern.calls == [123]
    assert interaction.response.kwargs == {"content": "Your saved character has been deleted.", "view": None}


def test_dungeon_death_refreshes_tavern_access() -> None:
    class DummyCharacters:
        def __init__(self) -> None:
            self.cleared: list[tuple[int, int]] = []

        async def clear(self, guild_id: int, user_id: int) -> None:
            self.cleared.append((guild_id, user_id))

    class DummyChannel:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send(self, **kwargs: object):
            self.sent.append(kwargs)
            return SimpleNamespace(id=9876)

    channel = DummyChannel()

    class DummyBot:
        def get_channel(self, channel_id: int):  # type: ignore[override]
            return channel

        def add_view(self, view, *, message_id: int) -> None:  # noqa: D401
            return None

    characters = DummyCharacters()
    update_calls: list[int] = []

    cog = dungeon.DungeonCog.__new__(dungeon.DungeonCog)
    cog.bot = DummyBot()
    cog.characters = characters
    cog._build_player_death_embed = lambda *args, **kwargs: object()

    async def fake_update(guild_id: int) -> None:
        update_calls.append(guild_id)

    async def noop(*args, **kwargs) -> None:
        return None

    cog._update_tavern_access = fake_update  # type: ignore[assignment]
    cog._send_player_to_manage_channel = noop  # type: ignore[assignment]
    cog._handle_party_failure_if_needed = noop  # type: ignore[assignment]

    session = SimpleNamespace(
        guild_id=321,
        channel_id=654,
        dungeon=SimpleNamespace(name="Arcane Depths"),
        room=SimpleNamespace(id=0, name="Entrance"),
        party_ids=set(),
        party_fall_announced=False,
        combat_state=None,
    )
    combatant = SimpleNamespace(
        user_id=999,
        is_player=True,
        is_dead=True,
        name="Hero",
        identifier="hero",
        current_hp=0,
        max_hp=10,
    )

    original_view = dungeon.ReturnToTavernView

    class DummyView:
        def __init__(self, *args, **kwargs) -> None:
            pass

    dungeon.ReturnToTavernView = DummyView

    async def runner() -> None:
        await cog._announce_player_death(session, combatant)

    try:
        asyncio.run(runner())
    finally:
        dungeon.ReturnToTavernView = original_view

    assert characters.cleared == [(321, 999)]
    assert update_calls == [321]
    assert channel.sent  # ensures the announcement attempted delivery

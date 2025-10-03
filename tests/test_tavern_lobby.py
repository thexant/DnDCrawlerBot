import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.tavern import PartyManager
from dnd.sessions import SessionManager


def test_party_manager_join_and_limit() -> None:
    manager = PartyManager(max_size=2)

    party, status, changed = manager.create_party(1, "Alice")
    assert status == "created"
    assert changed is True
    assert party.members == [1]

    join_status, joined_party, changed = manager.join_any(2)
    assert join_status == "added"
    assert joined_party is party
    assert changed is True
    assert party.members == [1, 2]

    join_status, _, changed = manager.join_any(3)
    assert join_status == "full"
    assert changed is False

    exists_status, exists_party, changed = manager.join_any(1)
    assert exists_status == "exists"
    assert exists_party is party
    assert changed is False

    leave_status, left_party, changed = manager.leave_member(2)
    assert leave_status == "removed"
    assert left_party is party
    assert changed is True
    assert party.members == [1]


def test_party_manager_vote_majority() -> None:
    manager = PartyManager()

    party, _, _ = manager.create_party(10, "Leader")
    manager.join_any(20)
    manager.join_any(30)

    now = datetime.now(timezone.utc)
    first = manager.record_vote(10, "Dungeon Alpha", now=now)
    assert first.status == "started"
    assert first.choice == "Dungeon Alpha"
    assert first.votes_for == 1
    assert first.required == 2
    assert first.party_name == party.name
    assert first.party_members == tuple(sorted(party.members))

    second = manager.record_vote(20, "Dungeon Alpha", now=now)
    assert second.status == "majority"
    assert second.choice == "Dungeon Alpha"
    assert second.votes_for == 2
    assert second.required == 2
    assert second.party_name == party.name
    assert second.party_members == tuple(sorted(party.members))


def test_party_manager_vote_timeout_resets_ballots() -> None:
    manager = PartyManager(vote_ttl=timedelta(minutes=5))

    party, _, _ = manager.create_party(100, "Scout")
    manager.join_any(200)
    manager.join_any(300)

    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    first = manager.record_vote(100, "Forgotten Depths", now=start)
    assert first.status == "started"
    assert party.active_vote is not None
    assert first.party_members == tuple(sorted(party.members))

    later = start + timedelta(minutes=6)
    progress = manager.record_vote(200, "Forgotten Depths", now=later)
    assert progress.status == "started"
    assert progress.votes_for == 1
    assert progress.required == 2
    assert party.active_vote is not None
    assert party.active_vote.ballots == {200: "Forgotten Depths"}
    assert progress.party_members == tuple(sorted(party.members))

    final = manager.record_vote(300, "Forgotten Depths", now=later)
    assert final.status == "majority"
    assert final.votes_for == 2
    assert final.required == 2
    assert final.party_members == tuple(sorted(party.members))


def test_party_manager_creates_unique_names() -> None:
    manager = PartyManager()

    party_one, status_one, _ = manager.create_party(1, "Alex")
    assert status_one == "created"
    party_two, status_two, _ = manager.create_party(2, "Alex")
    assert status_two == "created"
    assert party_one.name != party_two.name
    assert party_one.name.startswith("Alex")
    assert party_two.name.startswith("Alex")


def test_session_manager_separates_party_channels() -> None:
    manager: SessionManager[dict[str, object]] = SessionManager()

    async def runner() -> None:
        key_one = SessionManager.make_key(123, 1001)
        key_two = SessionManager.make_key(123, 1002)

        session_one = {"dungeon": "Forgotten Depths", "channel": 1001, "party": (1, 2)}
        session_two = {"dungeon": "Forgotten Depths", "channel": 1002, "party": (3, 4)}

        await manager.set(key_one, session_one)
        await manager.set(key_two, session_two)

        stored_one = await manager.get(key_one)
        stored_two = await manager.get(key_two)

        assert stored_one is session_one
        assert stored_two is session_two
        assert stored_one is not stored_two

        values = await manager.values()
        assert len(values) == 2
        assert any(value["channel"] == 1001 for value in values)
        assert any(value["channel"] == 1002 for value in values)

    asyncio.run(runner())

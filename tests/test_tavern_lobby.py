import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.tavern import PartyManager


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

    second = manager.record_vote(20, "Dungeon Alpha", now=now)
    assert second.status == "majority"
    assert second.choice == "Dungeon Alpha"
    assert second.votes_for == 2
    assert second.required == 2
    assert second.party_name == party.name


def test_party_manager_vote_timeout_resets_ballots() -> None:
    manager = PartyManager(vote_ttl=timedelta(minutes=5))

    party, _, _ = manager.create_party(100, "Scout")
    manager.join_any(200)
    manager.join_any(300)

    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    first = manager.record_vote(100, "Forgotten Depths", now=start)
    assert first.status == "started"
    assert party.active_vote is not None

    later = start + timedelta(minutes=6)
    progress = manager.record_vote(200, "Forgotten Depths", now=later)
    assert progress.status == "started"
    assert progress.votes_for == 1
    assert progress.required == 2
    assert party.active_vote is not None
    assert party.active_vote.ballots == {200: "Forgotten Depths"}

    final = manager.record_vote(300, "Forgotten Depths", now=later)
    assert final.status == "majority"
    assert final.votes_for == 2
    assert final.required == 2


def test_party_manager_creates_unique_names() -> None:
    manager = PartyManager()

    party_one, status_one, _ = manager.create_party(1, "Alex")
    assert status_one == "created"
    party_two, status_two, _ = manager.create_party(2, "Alex")
    assert status_two == "created"
    assert party_one.name != party_two.name
    assert party_one.name.startswith("Alex")
    assert party_two.name.startswith("Alex")

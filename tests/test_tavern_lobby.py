from datetime import datetime, timedelta, timezone

from cogs.tavern import PartyLobby


def test_party_lobby_join_and_limit() -> None:
    lobby = PartyLobby(max_size=4)

    assert lobby.join(1) == "added"
    assert lobby.join(1) == "exists"
    assert lobby.join(2) == "added"
    assert lobby.join(3) == "added"
    assert lobby.join(4) == "added"
    assert lobby.join(5) == "full"

    assert lobby.leave(5) == "missing"
    assert lobby.leave(2) == "removed"
    assert 2 not in lobby.members


def test_party_lobby_vote_majority() -> None:
    lobby = PartyLobby()
    for user_id in (10, 20, 30):
        assert lobby.join(user_id) == "added"

    now = datetime.now(timezone.utc)
    first = lobby.record_vote(10, "Dungeon Alpha", now=now)
    assert first.status == "started"
    assert first.choice == "Dungeon Alpha"
    assert first.votes_for == 1
    assert first.required == 2

    second = lobby.record_vote(20, "Dungeon Alpha", now=now)
    assert second.status == "majority"
    assert second.choice == "Dungeon Alpha"
    assert second.votes_for == 2
    assert second.required == 2


def test_party_lobby_vote_timeout_resets_ballots() -> None:
    lobby = PartyLobby(vote_ttl=timedelta(minutes=5))
    for user_id in (100, 200, 300):
        lobby.join(user_id)

    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    first = lobby.record_vote(100, "Forgotten Depths", now=start)
    assert first.status == "started"
    assert lobby.active_vote is not None

    # Advance beyond the TTL and ensure a new vote starts
    later = start + timedelta(minutes=6)
    progress = lobby.record_vote(200, "Forgotten Depths", now=later)
    assert progress.status == "started"
    assert progress.votes_for == 1
    assert progress.required == 2
    assert lobby.active_vote is not None
    assert lobby.active_vote.ballots == {200: "Forgotten Depths"}

    # Ensure a fresh vote can still reach majority
    final = lobby.record_vote(300, "Forgotten Depths", now=later)
    assert final.status == "majority"
    assert final.votes_for == 2
    assert final.required == 2

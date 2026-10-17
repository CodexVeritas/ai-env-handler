import re
from datetime import timedelta

import pytest

import envh.core.state as state_module
from envh.core.state import MAX_PENDING_PER_USER, Provenance, StateError, StateTable
from envh.core.words import ADJECTIVES, NOUNS
from tests.conftest import FakeClock

PROVENANCE = Provenance(pid=1, uid=1000, cmdline="x")


async def test_decisions_are_final() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    first = table.new_request("run", {"A": "A"}, PROVENANCE, "why")
    table.decide(first, approved=True, by="console")
    assert first.decision.result().outcome == "approved"
    assert first not in table.pending()
    with pytest.raises(StateError):
        table.decide(first, approved=False, by="console")


async def test_withdraw_and_detached_grace() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    request = table.new_request("session", {"A": "A"}, PROVENANCE, None, granted=timedelta(minutes=5))
    table.mark_detached(request)
    assert table.sweep_abandoned() == []
    clock.advance(timedelta(minutes=11))
    assert table.sweep_abandoned() == [request]
    assert request.decision.result().outcome == "withdrawn"
    assert table.pending() == []


async def test_sessions_expire_and_end() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    request = table.new_request("session", {"A": "A"}, PROVENANCE, None, granted=timedelta(minutes=30))
    session = table.create_session(request)
    assert table.session(session.id) is session
    assert table.live_sessions() == [session]
    clock.advance(timedelta(minutes=31))
    with pytest.raises(StateError, match="expired"):
        table.session(session.id)
    assert table.live_sessions() == []
    other = table.create_session(request)
    table.end_session(other.id)
    with pytest.raises(StateError, match="ended"):
        table.session(other.id)
    with pytest.raises(StateError, match="unknown"):
        table.session("nope")



async def test_session_ids_are_an_adjective_and_a_noun() -> None:
    table = StateTable(FakeClock())
    request = table.new_request("session", {"A": "A"}, PROVENANCE, None, granted=timedelta(minutes=30))
    adjective, noun = table.create_session(request).id.split("-")
    assert adjective in ADJECTIVES and noun in NOUNS


async def test_a_session_id_already_held_is_drawn_again(monkeypatch: pytest.MonkeyPatch) -> None:
    table = StateTable(FakeClock())
    request = table.new_request("session", {"A": "A"}, PROVENANCE, None, granted=timedelta(minutes=30))
    draws = iter(["brave", "otter", "brave", "otter", "calm", "otter"])
    monkeypatch.setattr(state_module.random_secrets, "choice", lambda options: next(draws))
    first = table.create_session(request)
    second = table.create_session(request)
    assert (first.id, second.id) == ("brave-otter", "calm-otter")
    assert table.session("brave-otter") is first


def test_session_words_are_short_lowercase_and_distinct() -> None:
    for words in (ADJECTIVES, NOUNS):
        assert all(re.fullmatch("[a-z]{3,8}", word) for word in words)
        assert len(set(words)) == len(words)
    assert not set(ADJECTIVES) & set(NOUNS)


async def test_runs() -> None:
    table = StateTable(FakeClock())
    run = table.start_run({"A": "A"}, None, "r", ("python", "x.py"), PROVENANCE)
    assert table.active_runs() == [run]
    table.end_run(run)
    assert table.active_runs() == []


async def test_prune_history() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    decided = table.new_request("run", {"A": "A"}, PROVENANCE, None)
    table.decide(decided, approved=True, by="console")
    pending = table.new_request("run", {"A": "A"}, PROVENANCE, None)
    run = table.start_run({"A": "A"}, None, None, (), PROVENANCE)
    table.end_run(run)
    clock.advance(timedelta(hours=2))
    assert table.prune_history() == 2
    assert (PROVENANCE.uid, decided.id) not in table.requests and (PROVENANCE.uid, pending.id) in table.requests and (PROVENANCE.uid, run.id) not in table.runs


async def test_ids_are_per_user_sequential_and_do_not_leak_other_users_activity() -> None:
    # Ids count per user (readable #1, #2, …), shared across that user's requests and runs, with the
    # tables keyed by (uid, seq). The key property: one user's ids must reveal nothing about how many
    # requests/runs another user allocated — each user's counter advances only on its own actions.
    table = StateTable(FakeClock())
    alice = Provenance(pid=1, uid=1000, cmdline="a")
    bob = Provenance(pid=2, uid=1001, cmdline="b")
    a1 = table.new_request("run", {"A": "A"}, alice, None)
    a2 = table.start_run({"A": "A"}, None, None, (), alice)
    # Bob's five allocations happen between two of Alice's but do not advance Alice's counter.
    bob_ids = [table.new_request("run", {"A": "A"}, bob, None).id for _ in range(5)]
    a3 = table.new_request("run", {"A": "A"}, alice, None)
    assert [a1.id, a2.id, a3.id] == [1, 2, 3]  # contiguous per user, unaffected by Bob's activity
    assert bob_ids == [1, 2, 3, 4, 5]  # Bob has his own sequence, also starting at 1
    # The same seq for two users does not collide: the tables are keyed by (uid, seq).
    assert table.requests[(alice.uid, 1)] is a1 and table.runs[(alice.uid, 2)] is a2
    assert (bob.uid, 1) in table.requests and table.requests[(bob.uid, 1)] is not a1


async def test_pending_requests_are_capped_per_user() -> None:
    table = StateTable(FakeClock())
    requests = [table.new_request("run", {"A": "A"}, PROVENANCE, None) for _ in range(MAX_PENDING_PER_USER)]
    with pytest.raises(StateError, match="waiting on the console"):
        table.new_request("run", {"A": "A"}, PROVENANCE, None)
    table.new_request("run", {"A": "A"}, Provenance(pid=2, uid=1001, cmdline="y"), None)
    table.decide(requests[0], approved=False, by="console")
    table.new_request("run", {"A": "A"}, PROVENANCE, None)

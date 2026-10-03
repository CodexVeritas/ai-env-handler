from datetime import timedelta

import pytest

from envh.core.state import MAX_PENDING_PER_USER, Provenance, StateError, StateTable
from tests.conftest import FakeClock

PROVENANCE = Provenance(pid=1, uid=1000, cmdline="x")


async def test_codes_unique_and_decisions() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    requests = [table.new_request("run", {"A": "A"}, PROVENANCE, "why") for _ in range(20)]
    assert len({request.code for request in requests}) == 20
    assert all(len(request.code) == 4 and request.code.isdigit() for request in requests)
    first = requests[0]
    assert table.find_by_code(first.code) is first
    table.decide(first, approved=True, by="console")
    assert first.decision.result().outcome == "approved"
    assert table.find_by_code(first.code) is None
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


async def test_runs() -> None:
    table = StateTable(FakeClock())
    run = table.start_run({"A": "A"}, None, "r", ("python", "x.py"), PROVENANCE)
    assert table.active_runs() == [run]
    table.end_run(run.id)
    assert table.active_runs() == []


async def test_prune_history() -> None:
    clock = FakeClock()
    table = StateTable(clock)
    decided = table.new_request("run", {"A": "A"}, PROVENANCE, None)
    table.decide(decided, approved=True, by="console")
    pending = table.new_request("run", {"A": "A"}, PROVENANCE, None)
    run = table.start_run({"A": "A"}, None, None, (), PROVENANCE)
    table.end_run(run.id)
    clock.advance(timedelta(hours=2))
    assert table.prune_history() == 2
    assert decided.id not in table.requests and pending.id in table.requests and run.id not in table.runs


async def test_pending_requests_are_capped_per_user() -> None:
    table = StateTable(FakeClock())
    requests = [table.new_request("run", {"A": "A"}, PROVENANCE, None) for _ in range(MAX_PENDING_PER_USER)]
    with pytest.raises(StateError, match="waiting on the console"):
        table.new_request("run", {"A": "A"}, PROVENANCE, None)
    table.new_request("run", {"A": "A"}, Provenance(pid=2, uid=1001, cmdline="y"), None)
    table.decide(requests[0], approved=False, by="console")
    table.new_request("run", {"A": "A"}, PROVENANCE, None)

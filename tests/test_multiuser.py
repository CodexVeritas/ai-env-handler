import pwd
from pathlib import Path

import pytest

import envh.server.control as control_module
from envh.platform import PeerCredentials
from tests.conftest import Client, Harness, approve_next

NOBODY_UID = pwd.getpwnam("nobody").pw_uid


@pytest.fixture
def as_uid(monkeypatch: pytest.MonkeyPatch):
    """Make the broker see connections as coming from another uid (SO_PEERCRED cannot be faked for real)."""
    state = {"uid": None}

    def fake_peer_credentials(sock):
        import os

        uid = state["uid"] if state["uid"] is not None else os.getuid()
        return PeerCredentials(pid=os.getpid(), uid=uid, gid=os.getgid())

    monkeypatch.setattr(control_module, "peer_credentials", fake_peer_credentials)

    def set_uid(uid: int | None) -> None:
        state["uid"] = uid

    return set_uid


async def open_session(control: Path, harness: Harness) -> str:
    async with Client(control) as client:
        await client.send(op="session_start", preset="minibench", minutes=30, reason="mine")
        await client.recv()
        await approve_next(harness)
        return (await client.recv())["session_id"]


async def test_unlisted_uid_is_rejected(control: Path, harness: Harness, as_uid) -> None:
    as_uid(4242424)
    async with Client(control) as client:
        await client.send(op="list")
        reply = await client.recv()
    assert reply["ok"] is False and "not in the broker's users list" in reply["error"]
    assert any("rejected_uid" in line for line in harness.echoed)


async def test_other_user_cannot_see_or_use_my_session(control: Path, harness: Harness, as_uid) -> None:
    session_id = await open_session(control, harness)
    as_uid(NOBODY_UID)
    async with Client(control) as client:
        await client.send(op="list")
        assert (await client.recv())["sessions"] == []
    async with Client(control) as client:
        await client.send(op="status")
        payload = await client.recv()
        assert payload["sessions"] == [] and payload["pending"] == []
    async with Client(control) as client:
        await client.send(op="run", session=session_id, **{"with": ["OPENAI_API_KEY"]}, command=["true"])
        reply = await client.recv()
        assert reply["ok"] is False and "belongs to another user" in reply["error"]
    async with Client(control) as client:
        await client.send(op="session_end", session_id=session_id)
        assert "belongs to another user" in (await client.recv())["error"]
    as_uid(None)
    async with Client(control) as client:
        await client.send(op="run", session=session_id, **{"with": ["OPENAI_API_KEY"]}, command=["true"])
        assert (await client.recv())["env"] == {"OPENAI_API_KEY": "sk-openai"}


async def test_other_user_cannot_wait_on_my_request(control: Path, harness: Harness, as_uid) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", **{"with": ["OPENAI_API_KEY"]}, minutes=5)
        request_id = (await client.recv())["request_id"]
    as_uid(NOBODY_UID)
    async with Client(control) as client:
        await client.send(op="session_wait", request_id=request_id)
        assert "unknown session request id" in (await client.recv())["error"]

import asyncio
import os
from pathlib import Path

from envh.server.control import ControlServer
from tests.conftest import Client, Harness, approve_next


async def test_session_lifecycle(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", presets=["team"], minutes=30, reason="research", command=["envh", "session", "start"])
        first = await client.recv()
        assert first["pending"] is True
        request_id = first["request_id"]
        pending = harness.broker.state.pending()[0]
        assert pending.provenance.pid == os.getpid()
        harness.broker.approve(pending)
        final = await client.recv()
        assert final["ok"] is True and final["request_id"] == request_id
        session_id = final["session_id"]
    async with Client(control) as client:
        await client.send(op="run", session=session_id, with_=None, **{"with": ["OPENAI_API_KEY"]}, command=["python", "x.py"])
        reply = await client.recv()
        assert reply["env"] == {"OPENAI_API_KEY": "sk-openai"}
        assert len(harness.broker.state.active_runs()) == 1
        await client.send(op="run_done", exit_code=0)
        await asyncio.sleep(0.05)
    assert harness.broker.state.active_runs() == []
    async with Client(control) as client:
        await client.send(op="run", session=session_id, **{"with": ["DATABASE_URL"]})
        reply = await client.recv()
        assert reply["ok"] is False and "not covered" in reply["error"]
    async with Client(control) as client:
        await client.send(op="session_end", session_id=session_id)
        assert (await client.recv())["ok"] is True
    async with Client(control) as client:
        await client.send(op="run", session=session_id, **{"with": ["OPENAI_API_KEY"]})
        reply = await client.recv()
        assert reply["ok"] is False and "ended" in reply["error"]


async def test_run_without_session_prompts_and_denial(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="run", **{"with": ["OPENAI_API_KEY"]}, reason="one-off", command=["python", "y.py"])
        assert (await client.recv())["pending"] is True
        pending = harness.broker.state.pending()[0]
        assert pending.kind == "run"
        harness.broker.deny(pending)
        reply = await client.recv()
        assert reply == {"ok": False, "request_id": pending.id, "error": "denied"}
    async with Client(control) as client:
        await client.send(op="run", presets=["team"], command=["python", "y.py"])
        assert (await client.recv())["pending"] is True
        await approve_next(harness)
        reply = await client.recv()
        assert reply["env"] == {"OPENROUTER_API_KEY": "sk-or-team", "OPENAI_API_KEY": "sk-openai"}
    await asyncio.sleep(0.05)
    assert harness.broker.state.active_runs() == []
    assert any("run_end" in line and "client gone" in line for line in harness.echoed)


async def test_run_withdrawn_when_client_leaves(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="run", **{"with": ["OPENAI_API_KEY"]})
        assert (await client.recv())["pending"] is True
        request = harness.broker.state.pending()[0]
    await asyncio.sleep(0.05)
    assert request.decision.result().outcome == "withdrawn"
    assert harness.broker.state.pending() == []


async def test_session_wait_resumes_after_disconnect(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", **{"with": ["OPENAI_API_KEY"]}, minutes=10)
        request_id = (await client.recv())["request_id"]
    await asyncio.sleep(0.05)
    request = harness.broker.state.requests[request_id]
    assert request.pending and request.abandon_at is not None
    async with Client(control) as client:
        await client.send(op="session_wait", request_id=request_id)
        assert (await client.recv())["pending"] is True
        assert request.abandon_at is None
        harness.broker.approve(request)
        final = await client.recv()
        assert final["ok"] is True and "session_id" in final
    async with Client(control) as client:
        await client.send(op="session_wait", request_id=request_id)
        assert (await client.recv())["pending"] is False
        assert (await client.recv())["session_id"] == final["session_id"]
    async with Client(control) as client:
        await client.send(op="session_wait", request_id=999)
        assert "unknown" in (await client.recv())["error"]


async def test_list_status_validate_and_errors(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="list")
        payload = await client.recv()
        assert {entry["name"] for entry in payload["secrets"]} == {"DATABASE_URL", "TEAM_OPENROUTER_KEY", "OPENAI_API_KEY"}
        assert "sk-openai" not in str(payload)
    async with Client(control) as client:
        await client.send(op="status")
        payload = await client.recv()
        assert payload["pending"] == [] and payload["runs"] == []
    async with Client(control) as client:
        await client.send(op="preset_validate", yaml="presets: {ok: {env: {A: OPENAI_API_KEY}}}")
        assert (await client.recv())["presets"] == ["ok"]
    async with Client(control) as client:
        await client.send(op="preset_validate", yaml="presets: {bad: {env: {A: NOPE}}}")
        assert "not in the vault" in (await client.recv())["error"]
    async with Client(control) as client:
        await client.send(op="nonsense")
        assert "unknown operation" in (await client.recv())["error"]
    async with Client(control) as client:
        assert client.writer is not None
        client.writer.write(b"not json\n")
        await client.writer.drain()
        assert "malformed" in (await client.recv())["error"]


async def test_import_and_propose_over_socket(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="import", secrets={"NEW_KEY": "value"}, presets={"p": {"env": {"X": "NEW_KEY"}}}, reason="wizard")
        assert (await client.recv())["pending"] is True
        await approve_next(harness)
        reply = await client.recv()
        assert reply["added"] == ["NEW_KEY"] and reply["presets"] == ["p"] and reply["renames"] == {}
    assert "NEW_KEY" in harness.broker.vault
    async with Client(control) as client:
        await client.send(op="import", secrets={"NEW_KEY": "other", "SAME_AS_OPENAI": "sk-openai"}, presets={}, reason="second")
        assert (await client.recv())["pending"] is True
        await approve_next(harness)
        assert (await client.recv())["renames"] == {"NEW_KEY": "NEW_KEY_2", "SAME_AS_OPENAI": "OPENAI_API_KEY"}
    async with Client(control) as client:
        await client.send(op="preset_propose", yaml="presets: {q: {env: {Y: NEW_KEY}}}", reason="draft")
        assert (await client.recv())["presets"] == ["q"]
        await approve_next(harness)
        assert (await client.recv())["ok"] is True
    assert "q" in harness.broker.config.presets


async def test_preset_with_override_mismatch_is_rejected(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", presets=["team"], minutes=5, **{"with": ["OPENAI_API_KEY=TEAM_OPENROUTER_KEY"]})
        reply = await client.recv()
        assert reply["ok"] is False and "maps to OPENAI_API_KEY in preset team" in reply["error"]
    async with Client(control) as client:
        await client.send(op="session_start", presets=["team"], minutes=5, **{"with": ["OPENAI_API_KEY=OPENAI_API_KEY"]})
        assert (await client.recv())["pending"] is True
        assert harness.broker.state.pending()[0].mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}


async def test_non_string_reason_is_refused_before_any_request_exists(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", **{"with": ["OPENAI_API_KEY"]}, minutes=5, reason=["x"])
        reply = await client.recv()
        assert reply["ok"] is False and "reason must be a string" in reply["error"]
    assert harness.broker.state.pending() == []


async def test_close_hangs_up_on_waiting_clients(harness: Harness, tmp_path: Path) -> None:
    server = ControlServer(harness.broker, tmp_path / "closing.sock")
    await server.start()
    async with Client(tmp_path / "closing.sock") as client:
        await client.send(op="run", **{"with": ["OPENAI_API_KEY"]})
        assert (await client.recv())["pending"] is True
        await asyncio.wait_for(server.close(), timeout=5)
        assert client.reader is not None and await client.reader.readline() == b""
    assert harness.broker.state.pending() == []


async def test_session_combines_several_presets(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", presets=["team", "dbwork"], minutes=30, reason="mixed")
        assert (await client.recv())["excluded_per_run"] == ["DATABASE_URL"]
        await approve_next(harness)
        assert (await client.recv())["ok"] is True
    async with Client(control) as client:
        await client.send(op="list")
        session = (await client.recv())["sessions"][0]
        assert session["presets"] == ["team", "dbwork"] and session["vars"] == ["OPENAI_API_KEY", "OPENROUTER_API_KEY"]
    async with Client(control) as client:
        await client.send(op="session_start", presets="team", minutes=5)
        reply = await client.recv()
        assert reply["ok"] is False and "list of preset names" in reply["error"]

"""Regression tests for the CONFIRMED findings of the 2026-10-19 red-team pass.

Each test fails on the pre-fix code and passes after the fix. See redteam/2026-10-19/FINDINGS.md
and REMEDIATION.md. Hook-bypass regressions (MEDIUM) live in test_hook.py next to the hook helpers.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

import envh.core.state as state_mod
from envh.core.config import ConfigError, parse_presets
from envh.core.state import MAX_LIVE_SESSIONS_PER_USER, Provenance, StateError, StateTable
from tests.conftest import Client, FakeClock, Harness, make_auto


# --- D1: audit-log disk-fill via uncapped run_end fields + vars (V4 bypass) ------------------

async def test_run_done_fields_are_clamped_so_a_client_cannot_flood_the_audit_log(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")  # auto: the run is granted with no console and reaches run_done
    async with Client(control) as client:
        await client.send(op="run", reason="x", command=["true"], **{"with": ["OPENAI_API_KEY"]})
        reply = await client.recv()
        assert reply["ok"] is True and reply["env"] == {"OPENAI_API_KEY": "sk-openai"}
        blob = "9" * 500_000  # attacker-controlled run_done fields, half a megabyte each
        await client.send(op="run_done", exit_code=blob, lingering_terminated=blob)
        run_end = None
        for _ in range(500):  # wait (up to ~5s) for the broker to actually record the run_end event
            run_end = next((line for line in harness.echoed if "run_end" in line), None)
            if run_end is not None:
                break
            await asyncio.sleep(0.01)
    assert run_end is not None, "the broker never recorded a run_end event"
    assert len(run_end) < 10_000, "run_end must not carry the half-megabyte blob into the audit log"


async def test_a_run_with_too_many_with_items_is_refused(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="run", reason="x", command=["true"], **{"with": [f"V{i}=OPENAI_API_KEY" for i in range(300)]})
        reply = await client.recv()
    assert reply["ok"] is False and "too many --with items" in reply["error"]


# --- D2: session-id namespace exhaustion wedges the broker -----------------------------------

async def test_live_sessions_are_capped_per_user_to_prevent_the_id_exhaustion_wedge() -> None:
    state = StateTable(FakeClock())
    prov = Provenance(pid=1, uid=4242, cmdline="x")
    for _ in range(MAX_LIVE_SESSIONS_PER_USER):
        request = state.new_request(kind="session", mapping={"V": "S"}, provenance=prov, reason=None, granted=timedelta(minutes=5))
        state.create_session(request)
        state.decide(request, approved=True, by="test")  # clear pending so the per-user PENDING cap isn't what fires
    request = state.new_request(kind="session", mapping={"V": "S"}, provenance=prov, reason=None, granted=timedelta(minutes=5))
    with pytest.raises(StateError, match="live sessions"):
        state.create_session(request)


async def test_new_session_id_terminates_when_the_word_space_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the id vocabulary to a single pair, occupy it, and confirm generation falls back to a
    # wide random id instead of spinning the event loop forever (the pre-fix `while True` wedge).
    monkeypatch.setattr(state_mod, "ADJECTIVES", ["sole"])
    monkeypatch.setattr(state_mod, "NOUNS", ["pair"])
    state = StateTable(FakeClock())
    state.sessions["sole-pair"] = object()  # type: ignore[assignment]  # only membership is checked
    session_id = state._new_session_id()
    assert session_id != "sole-pair" and session_id.startswith("sole-pair-")


# --- D4 / D3: YAML parse DoS in preset validation (bypasses V1/V2) ---------------------------

async def test_a_yaml_merge_key_is_refused_before_it_can_expand() -> None:
    # A small merge-key "billion laughs": each level merges several copies of the level below, which
    # PyYAML would expand to width**depth in the constructor BEFORE any validation. It must be rejected.
    lines = ["a0: &a0 {x: 1}"]
    for level in range(1, 8):
        copies = ", ".join([f"*a{level - 1}"] * 6)
        lines.append(f"a{level}: &a{level} {{<<: [{copies}]}}")
    bomb = "presets:\n  p: {env: {V: S}}\n" + "\n".join(lines) + "\n"
    with pytest.raises(ConfigError) as info:
        parse_presets(bomb, None)
    assert "merge" in str(info.value).lower()


async def test_an_aliased_env_reused_across_many_presets_is_refused_quickly() -> None:
    # One shared env node aliased into many presets would make validation re-walk it per preset,
    # O(presets x vars). The total-entry cap refuses it during a cheap len() count, before that work.
    big_env = "{" + ", ".join(f"V{i}: S{i}" for i in range(2000)) + "}"
    lines = ["presets:", f"  p0: {{env: &E {big_env}}}"]
    for i in range(1, 50):
        lines.append(f"  p{i}: {{env: *E}}")
    with pytest.raises(ConfigError) as info:
        parse_presets("\n".join(lines) + "\n", None)
    assert "too many" in str(info.value).lower()


async def test_deeply_nested_yaml_is_a_clean_config_error_not_a_recursion_crash() -> None:
    text = "presets:\n  p:\n    env:\n      V: " + "[" * 5000 + "]" * 5000 + "\n"
    with pytest.raises(ConfigError):
        parse_presets(text, None)


# --- Robustness (LOW): client inputs must not raise uncaught exceptions ----------------------

async def test_a_non_finite_minutes_is_a_clean_error_not_an_overflow(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        await client.send(op="session_start", presets=["team"], minutes=float("inf"), reason="x")
        reply = await client.recv()
    assert reply["ok"] is False and "whole number" in reply["error"]


async def test_a_huge_json_number_is_a_clean_error_not_an_internal_error(control: Path, harness: Harness) -> None:
    async with Client(control) as client:
        assert client.writer is not None
        client.writer.write(b'{"op": "status", "n": ' + b"1" * 4400 + b"}\n")  # exceeds CPython's int-string limit
        await client.writer.drain()
        reply = await client.recv()
    assert reply["ok"] is False and "malformed request" in reply["error"]


async def test_a_session_run_with_non_string_with_items_is_a_clean_error(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    async with Client(control) as starter:
        await starter.send(op="session_start", minutes=30, reason="x", **{"with": ["OPENAI_API_KEY"]})
        await starter.recv()  # pending reply
        granted = await starter.recv()  # auto-granted decision reply with the session id
        session_id = granted["session_id"]
    async with Client(control) as runner:
        await runner.send(op="run", session=session_id, **{"with": [123]})  # non-string item
        reply = await runner.recv()
    assert reply["ok"] is False and "internal error" not in reply["error"]

from datetime import timedelta

import pytest

from envh.core.broker import RequestError, fingerprint
from envh.core.config import ConfigError, load_config
from envh.core.vault import Vault
from tests.conftest import PASSPHRASE, Harness


def test_mapping_from_with(harness: Harness) -> None:
    broker = harness.broker
    assert broker.mapping_from_with(["OPENAI_API_KEY", "OPENROUTER_API_KEY=TEAM_OPENROUTER_KEY"]) == {
        "OPENAI_API_KEY": "OPENAI_API_KEY",
        "OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY",
    }
    with pytest.raises(RequestError, match="not in the vault"):
        broker.mapping_from_with(["MISSING"])
    with pytest.raises(RequestError, match="valid environment variable"):
        broker.mapping_from_with(["1bad=OPENAI_API_KEY"])
    with pytest.raises(RequestError, match="no variables"):
        broker.mapping_from_with([])


def test_preset_mapping_and_caps(harness: Harness) -> None:
    broker = harness.broker
    mapping, preset = broker.mapping_from_preset("team")
    assert mapping == {"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY", "OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert broker.session_cap(mapping, preset) == timedelta(hours=1)
    assert broker.session_cap({"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY"}, preset) == timedelta(hours=2)
    with pytest.raises(RequestError, match="unknown preset"):
        broker.mapping_from_preset("nope")


async def test_session_request_excludes_per_run(harness: Harness) -> None:
    broker = harness.broker
    mapping, preset = broker.mapping_from_preset("dbwork")
    request = broker.request_session(mapping, preset, 600, "work", (), harness.provenance())
    assert request.mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert request.summary["excluded_per_run"] == ["DATABASE_URL"]
    assert request.granted == timedelta(hours=1)
    broker.approve(request)
    session = broker.state.session(request.result["session_id"])
    assert session.mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    with pytest.raises(RequestError, match="every requested secret is per-run"):
        broker.request_session({"DATABASE_URL": "DATABASE_URL"}, preset, 10, None, (), harness.provenance())
    with pytest.raises(RequestError, match="positive"):
        broker.request_session(mapping, preset, 0, None, (), harness.provenance())


async def test_run_in_session_resolution(harness: Harness) -> None:
    broker = harness.broker
    mapping, preset = broker.mapping_from_preset("team")
    request = broker.request_session(mapping, preset, 30, None, (), harness.provenance())
    broker.approve(request)
    session_id = request.result["session_id"]
    uid = harness.provenance().uid
    session, resolved = broker.resolve_run_in_session(session_id, ["OPENAI_API_KEY"], uid)
    assert resolved == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    _, whole = broker.resolve_run_in_session(session_id, [], uid)
    assert whole == mapping
    with pytest.raises(RequestError, match="not covered"):
        broker.resolve_run_in_session(session_id, ["DATABASE_URL"], uid)
    with pytest.raises(RequestError, match="maps to"):
        broker.resolve_run_in_session(session_id, ["OPENAI_API_KEY=TEAM_OPENROUTER_KEY"], uid)
    with pytest.raises(RequestError, match="belongs to another user"):
        broker.resolve_run_in_session(session_id, [], uid + 1)
    with pytest.raises(RequestError, match="belongs to another user"):
        broker.end_session(session_id, by="test", uid=uid + 1)
    assert broker.list_payload(for_uid=uid + 1)["sessions"] == []
    assert len(broker.list_payload(for_uid=uid)["sessions"]) == 1
    assert broker.env_for(resolved) == {"OPENAI_API_KEY": "sk-openai"}


async def test_import_then_approve_writes_vault_and_presets(harness: Harness) -> None:
    broker = harness.broker
    presets = {"news-bot-personal": {"env": {"OPENROUTER_API_KEY": "PERSONAL_OPENROUTER_KEY", "OPENAI_API_KEY": "OPENAI_API_KEY"}}}
    request = broker.request_import({"PERSONAL_OPENROUTER_KEY": "sk-or-personal", "OPENAI_API_KEY": "sk-openai"}, presets, "import", harness.provenance())
    assert request.summary["added"] == ["PERSONAL_OPENROUTER_KEY"]
    assert request.summary["unchanged"] == ["OPENAI_API_KEY"]
    assert "news-bot-personal" in request.summary["diff"]
    broker.approve(request)
    reopened = Vault.open(harness.data_dir / "vault.age", PASSPHRASE)
    assert reopened.get("PERSONAL_OPENROUTER_KEY") == "sk-or-personal"
    assert "news-bot-personal" in broker.config.presets
    assert "news-bot-personal" in load_config(harness.data_dir, set(reopened.names())).presets
    with pytest.raises(RequestError, match="not in the vault"):
        broker.request_import({}, {"p": {"env": {"X": "NOPE"}}}, None, harness.provenance())
    with pytest.raises(RequestError, match="nothing to import"):
        broker.request_import({}, {}, None, harness.provenance())


async def test_preset_proposal(harness: Harness) -> None:
    broker = harness.broker
    text = "presets: {research: {max_session: 2h, env: {OPENAI_API_KEY: OPENAI_API_KEY}}}"
    request = broker.request_preset(text, "draft", harness.provenance())
    assert request.summary["presets"] == ["research"]
    assert "+  research:" in request.summary["diff"]
    broker.deny(request)
    assert "research" not in broker.config.presets
    request = broker.request_preset(text, "draft", harness.provenance())
    broker.approve(request)
    assert broker.config.presets["research"].max_session == timedelta(hours=2)
    with pytest.raises(RequestError, match="changes nothing"):
        broker.request_preset(text, None, harness.provenance())
    with pytest.raises(RequestError, match="not in the vault"):
        broker.request_preset("presets: {x: {env: {A: NOPE}}}", None, harness.provenance())
    with pytest.raises(ConfigError):
        broker.validate_preset_yaml("presets: {x: {env: {}}}")


def test_list_payload_and_fingerprint(harness: Harness) -> None:
    payload = harness.broker.list_payload()
    names = [entry["name"] for entry in payload["secrets"]]
    assert names == ["DATABASE_URL", "OPENAI_API_KEY", "TEAM_OPENROUTER_KEY"]
    team_preset = next(preset for preset in payload["presets"] if preset["name"] == "team")
    assert team_preset["max_session"] == "1h"
    assert all(entry["in_vault"] for entry in team_preset["env"])
    assert "sk-openai" not in str(payload)
    assert fingerprint("sk-proj-abcdefghijkl").startswith("sk-p…")
    assert fingerprint("short") == f"s…{fingerprint('short').split('…')[1]}"


async def test_approval_refuses_a_stale_presets_snapshot(harness: Harness) -> None:
    broker = harness.broker
    shown: list[int] = []
    broker.request_listeners.append(lambda request: shown.append(request.id))
    first = broker.request_preset("presets: {alpha: {env: {OPENAI_API_KEY: OPENAI_API_KEY}}}", "a", harness.provenance())
    second = broker.request_preset("presets: {beta: {env: {OPENAI_API_KEY: OPENAI_API_KEY}}}", "b", harness.provenance())
    broker.approve(first)
    with pytest.raises(RequestError, match="changed since"):
        broker.approve(second)
    assert second.pending and shown.count(second.id) == 2
    assert "alpha" in second.summary["merged"] and "-  alpha:" not in second.summary["diff"]
    broker.approve(second)
    assert {"alpha", "beta"} <= set(broker.config.presets)


async def test_approval_revalidates_against_the_current_vault(harness: Harness) -> None:
    broker = harness.broker
    request = broker.request_preset("presets: {gamma: {env: {X: TEAM_OPENROUTER_KEY}}}", None, harness.provenance())
    broker.vault.remove("TEAM_OPENROUTER_KEY")
    with pytest.raises(RequestError, match="no longer validates"):
        broker.approve(request)
    assert request.pending and "gamma" not in broker.config.presets

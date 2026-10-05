import re
from dataclasses import replace
from datetime import timedelta

import pytest

from envh.common import peek
from envh.core.broker import RequestError, fingerprint
from envh.core.config import ConfigError, SecretEntry, load_config
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
    mapping, presets = broker.mapping_from_presets(["team"], [])
    assert mapping == {"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY", "OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert broker.session_cap(mapping, presets) == timedelta(hours=1)
    assert broker.session_cap({"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY"}, presets) == timedelta(hours=2)
    with pytest.raises(RequestError, match="unknown preset"):
        broker.mapping_from_presets(["nope"], [])


async def test_session_request_excludes_per_run(harness: Harness) -> None:
    broker = harness.broker
    mapping, presets = broker.mapping_from_presets(["dbwork"], [])
    request = broker.request_session(mapping, presets, 600, "work", (), harness.provenance())
    assert request.mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert request.summary["excluded_per_run"] == ["DATABASE_URL"]
    assert request.granted == timedelta(hours=1)
    broker.approve(request)
    session = broker.state.session(request.result["session_id"])
    assert session.mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    with pytest.raises(RequestError, match="every requested secret is per-run"):
        broker.request_session({"DATABASE_URL": "DATABASE_URL"}, presets, 10, None, (), harness.provenance())
    with pytest.raises(RequestError, match="positive"):
        broker.request_session(mapping, presets, 0, None, (), harness.provenance())


async def test_run_in_session_resolution(harness: Harness) -> None:
    broker = harness.broker
    mapping, presets = broker.mapping_from_presets(["team"], [])
    request = broker.request_session(mapping, presets, 30, None, (), harness.provenance())
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
    assert re.fullmatch(r"sk…kl \(20 chars, id [0-9a-f]{6}\)", fingerprint("sk-proj-abcdefghijkl"))
    assert re.fullmatch(r"… \(5 chars, id [0-9a-f]{6}\)", fingerprint("short"))


@pytest.mark.parametrize(("value", "shown"), [("a" * 11, "…"), ("abcdefghijkl", "a…l"), ("ab" + "x" * 16 + "yz", "ab…yz"), ("abc" + "x" * 26 + "xyz", "abc…xyz"), ("sk-p" + "x" * 52 + "a3f9", "sk-p…a3f9")])
def test_peek_shows_the_ends_of_a_value_and_at_most_a_fifth_of_it(value: str, shown: str) -> None:
    assert peek(value) == shown
    assert len(shown) - 1 <= len(value) / 5


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


async def test_import_approval_refuses_when_an_added_secret_now_exists(harness: Harness) -> None:
    broker = harness.broker
    shown: list[int] = []
    broker.request_listeners.append(lambda request: shown.append(request.id))
    first = broker.request_import({"NEW_KEY": "value-one"}, {}, "mine", harness.provenance())
    second = broker.request_import({"NEW_KEY": "value-two"}, {}, "other", harness.provenance())
    assert second.summary["added"] == ["NEW_KEY"]
    broker.approve(first)
    with pytest.raises(RequestError, match="vault changed since"):
        broker.approve(second)
    assert second.pending and shown.count(second.id) == 2
    assert second.summary["added"] == ["NEW_KEY_2"] and second.summary["renamed"] == {"NEW_KEY": "NEW_KEY_2"}
    broker.approve(second)
    assert broker.vault.get("NEW_KEY") == "value-one"
    assert broker.vault.get("NEW_KEY_2") == "value-two"


async def test_import_reuses_stored_values_and_never_overwrites(harness: Harness) -> None:
    broker = harness.broker
    secrets = {"OPENAI_API_KEY": "sk-newsbot-own", "NEWS_BOT_OPENAI_KEY": "sk-openai", "FRESH_KEY": "sk-fresh"}
    presets = {"news-bot": {"env": {"OPENAI_API_KEY": "OPENAI_API_KEY", "ALT_OPENAI": "NEWS_BOT_OPENAI_KEY", "FRESH": {"secret": "FRESH_KEY", "approval": "per-run"}}}}
    request = broker.request_import(secrets, presets, "second project", harness.provenance())
    assert request.summary["reused"] == {"NEWS_BOT_OPENAI_KEY": "OPENAI_API_KEY"}
    assert request.summary["renamed"] == {"OPENAI_API_KEY": "OPENAI_API_KEY_2"}
    assert request.summary["added"] == ["FRESH_KEY", "OPENAI_API_KEY_2"]
    broker.approve(request)
    assert broker.vault.get("OPENAI_API_KEY") == "sk-openai"
    assert broker.vault.get("OPENAI_API_KEY_2") == "sk-newsbot-own"
    assert "NEWS_BOT_OPENAI_KEY" not in broker.vault
    env = broker.config.presets_raw["news-bot"]["env"]
    assert env["OPENAI_API_KEY"] == "OPENAI_API_KEY_2" and env["ALT_OPENAI"] == "OPENAI_API_KEY"
    assert env["FRESH"] == {"secret": "FRESH_KEY", "approval": "per-run"}


async def test_import_skips_numbers_that_are_taken(harness: Harness) -> None:
    request = harness.broker.request_import({"OPENAI_API_KEY": "sk-one", "OPENAI_API_KEY_2": "sk-two"}, {}, None, harness.provenance())
    assert request.summary["renamed"] == {"OPENAI_API_KEY": "OPENAI_API_KEY_3"}
    assert request.summary["added"] == ["OPENAI_API_KEY_2", "OPENAI_API_KEY_3"]


async def test_import_does_not_leak_whether_a_submitted_value_matches_a_stored_secret(harness: Harness) -> None:
    # The import flow classifies a submitted value by equality with the stored one and invents a
    # rename target (NAME_2) for a value that differs. Validating the renamed result before approval
    # let a client point a preset at that target and read the equality bit from error-vs-pending — an
    # unapproved value oracle on any stored secret. The pre-approval outcome must now be identical
    # whether or not the submitted value matches the stored one (both refused: the client never
    # submitted OPENAI_API_KEY_2 and it is not in the vault).
    broker = harness.broker
    preset = {"p": {"env": {"X": "OPENAI_API_KEY_2"}}}
    with pytest.raises(RequestError, match="OPENAI_API_KEY_2 is not in the vault"):
        broker.request_import({"OPENAI_API_KEY": "sk-openai"}, preset, "correct guess", harness.provenance())
    with pytest.raises(RequestError, match="OPENAI_API_KEY_2 is not in the vault"):
        broker.request_import({"OPENAI_API_KEY": "wrong-value"}, preset, "wrong guess", harness.provenance())
    assert broker.state.pending() == []


def test_oversized_preset_document_is_refused_before_parsing(harness: Harness) -> None:
    huge = "presets:\n  p:\n    env:\n      A: OPENAI_API_KEY\n# " + "x" * (64 * 1024) + "\n"
    with pytest.raises(RequestError, match="larger than"):
        harness.broker.validate_preset_yaml(huge)
    with pytest.raises(RequestError, match="larger than"):
        harness.broker.request_preset(huge, "big", harness.provenance())


async def test_session_runs_refuse_secrets_that_became_per_run(harness: Harness) -> None:
    broker = harness.broker
    mapping, presets = broker.mapping_from_presets(["team"], [])
    request = broker.request_session(mapping, presets, 30, None, (), harness.provenance())
    broker.approve(request)
    session_id = request.result["session_id"]
    uid = harness.provenance().uid
    config_path = harness.data_dir / "config.yaml"
    config_path.write_text(config_path.read_text().replace("OPENAI_API_KEY: {max_session: 1h}", "OPENAI_API_KEY: {approval: per-run}"))
    broker.reload()
    with pytest.raises(RequestError, match="approval is now per-run for OPENAI_API_KEY"):
        broker.resolve_run_in_session(session_id, [], uid)
    _, resolved = broker.resolve_run_in_session(session_id, ["OPENROUTER_API_KEY"], uid)
    assert resolved == {"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY"}


async def test_rename_moves_the_secret_its_policy_presets_and_live_sessions(harness: Harness) -> None:
    broker = harness.broker
    mapping, presets = broker.mapping_from_presets(["team"], [])
    request = broker.request_session(mapping, presets, 30, "work", ("x",), harness.provenance())
    broker.approve(request)
    waiting = broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, [], "why", ("x",), harness.provenance())
    broker.rename_secret("OPENAI_API_KEY", "PERSONAL_OPENAI_KEY")
    assert "OPENAI_API_KEY" not in broker.vault and broker.vault.get("PERSONAL_OPENAI_KEY") == "sk-openai"
    assert Vault.open(harness.data_dir / "vault.age", PASSPHRASE).get("PERSONAL_OPENAI_KEY") == "sk-openai"
    assert broker.config.policy_for("PERSONAL_OPENAI_KEY").max_session == timedelta(hours=1)
    assert broker.config.presets["team"].env["OPENAI_API_KEY"].secret == "PERSONAL_OPENAI_KEY"
    assert broker.config.presets["dbwork"].env["OPENAI_API_KEY"].secret == "PERSONAL_OPENAI_KEY"
    session, run_mapping = broker.resolve_run_in_session(request.result["session_id"], [], harness.provenance().uid)
    assert broker.env_for(run_mapping)["OPENAI_API_KEY"] == "sk-openai"
    assert waiting.mapping == {"OPENAI_API_KEY": "PERSONAL_OPENAI_KEY"}
    assert any("secret_renamed" in line for line in harness.echoed)
    reloaded = load_config(harness.data_dir, set(broker.vault.names()))
    assert "PERSONAL_OPENAI_KEY" in reloaded.secret_policies and "OPENAI_API_KEY" not in reloaded.secret_policies


@pytest.mark.parametrize(("old", "new", "message"), [("MISSING", "NEW_NAME", "not in the vault"), ("DATABASE_URL", "bad-name", "valid secret name"), ("DATABASE_URL", "OPENAI_API_KEY", "already taken")])
def test_rename_refuses_without_changing_anything(harness: Harness, old: str, new: str, message: str) -> None:
    before = {name: harness.broker.vault.get(name) for name in harness.broker.vault.names()}
    presets_before = (harness.data_dir / "presets.yaml").read_text()
    with pytest.raises(RequestError, match=message):
        harness.broker.rename_secret(old, new)
    assert {name: harness.broker.vault.get(name) for name in harness.broker.vault.names()} == before
    assert (harness.data_dir / "presets.yaml").read_text() == presets_before


def test_a_rename_that_cannot_save_the_vault_changes_nothing(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    files_before = {path.name: path.read_text() for path in harness.data_dir.glob("*.yaml")}

    def disk_full() -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(harness.broker.vault, "save", disk_full)
    with pytest.raises(OSError, match="No space left"):
        harness.broker.rename_secret("OPENAI_API_KEY", "PERSONAL_OPENAI_KEY")
    assert "OPENAI_API_KEY" in harness.broker.vault and "PERSONAL_OPENAI_KEY" not in harness.broker.vault
    assert {path.name: path.read_text() for path in harness.data_dir.glob("*.yaml")} == files_before
    assert not list(harness.data_dir.glob("*.tmp"))


async def test_combined_presets_merge_variables_under_the_strictest_policy(harness: Harness) -> None:
    broker = harness.broker
    broker.write_presets({**broker.config.presets_raw, "ops": {"max_session": "30m", "env": {"DATABASE_URL": "DATABASE_URL", "OPENAI_API_KEY": "OPENAI_API_KEY"}}})
    mapping, presets = broker.mapping_from_presets(["team", "dbwork", "team"], [])
    assert [preset.name for preset in presets] == ["team", "dbwork"]
    assert mapping == {"OPENROUTER_API_KEY": "TEAM_OPENROUTER_KEY", "OPENAI_API_KEY": "OPENAI_API_KEY", "DATABASE_URL": "DATABASE_URL"}
    ops_only = broker.mapping_from_presets(["ops"], [])[1]
    assert broker.split_per_run({"DATABASE_URL": "DATABASE_URL"}, ops_only) == ({"DATABASE_URL": "DATABASE_URL"}, {})
    mapping, presets = broker.mapping_from_presets(["ops", "dbwork"], [])
    request = broker.request_session(mapping, presets, 600, "mixed", (), harness.provenance())
    assert request.presets == ("ops", "dbwork")
    assert request.mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert request.summary["excluded_per_run"] == ["DATABASE_URL"]
    assert request.granted == timedelta(minutes=30)
    broker.approve(request)
    assert broker.state.session(request.result["session_id"]).presets == ("ops", "dbwork")


def test_combined_presets_refuse_a_variable_mapped_to_two_secrets(harness: Harness) -> None:
    broker = harness.broker
    broker.write_presets({**broker.config.presets_raw, "other": {"env": {"OPENROUTER_API_KEY": "OPENAI_API_KEY", "OPENAI_API_KEY": "OPENAI_API_KEY"}}})
    with pytest.raises(RequestError, match="OPENROUTER_API_KEY is TEAM_OPENROUTER_KEY in team and OPENAI_API_KEY in other; leave one"):
        broker.mapping_from_presets(["team", "other"], [])
    assert broker.mapping_from_presets(["team", "other"], ["OPENAI_API_KEY"])[0] == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert broker.mapping_from_presets(["team", "other"], ["OPENROUTER_API_KEY=OPENAI_API_KEY"])[0] == {"OPENROUTER_API_KEY": "OPENAI_API_KEY"}
    with pytest.raises(RequestError, match="maps to TEAM_OPENROUTER_KEY or OPENAI_API_KEY in presets team, other, not DATABASE_URL"):
        broker.mapping_from_presets(["team", "other"], ["OPENROUTER_API_KEY=DATABASE_URL"])
    with pytest.raises(RequestError, match="not in presets team, other: DATABASE_URL"):
        broker.mapping_from_presets(["team", "other"], ["DATABASE_URL"])


async def test_rename_refuses_a_name_with_a_leftover_policy_or_still_in_use(harness: Harness) -> None:
    broker = harness.broker
    config_path = harness.data_dir / "config.yaml"
    config_path.write_text(config_path.read_text().replace("secrets: {", "secrets: {LEFTOVER_KEY: {approval: session, max_session: 24h}, "))
    broker.reload()
    with pytest.raises(RequestError, match="config.yaml still has settings for LEFTOVER_KEY"):
        broker.rename_secret("DATABASE_URL", "LEFTOVER_KEY")
    broker.request_run({"VAR": "TEAM_OPENROUTER_KEY"}, [], "why", ("x",), harness.provenance())
    broker.vault.remove("TEAM_OPENROUTER_KEY")
    with pytest.raises(RequestError, match="still uses the name TEAM_OPENROUTER_KEY"):
        broker.rename_secret("DATABASE_URL", "TEAM_OPENROUTER_KEY")
    assert broker.vault.get("DATABASE_URL") == "postgres://x"


async def test_rename_follows_waiting_preset_proposals_and_approved_runs(harness: Harness) -> None:
    broker = harness.broker
    proposal = broker.request_preset("presets:\n  newp:\n    env:\n      KEY: OPENAI_API_KEY\n", "add newp", harness.provenance())
    run = broker.request_run({"API": "OPENAI_API_KEY"}, [], "why", ("x",), harness.provenance())
    broker.approve(run)
    broker.rename_secret("OPENAI_API_KEY", "MY_OPENAI")
    assert run.mapping == {"API": "MY_OPENAI"}
    assert "KEY: MY_OPENAI" in proposal.summary["diff"] and ": OPENAI_API_KEY\n" not in proposal.summary["diff"]
    broker.approve(proposal)
    assert broker.config.presets["newp"].env["KEY"].secret == "MY_OPENAI"


def test_rename_uses_the_config_it_checked_rather_than_rereading_the_files(harness: Harness) -> None:
    broker = harness.broker
    (harness.data_dir / "presets.yaml").write_text("presets: {broken: {env: {X: NOT_IN_VAULT}}}\n")
    broker.rename_secret("OPENAI_API_KEY", "MY_OPENAI")
    assert broker.config.policy_for("MY_OPENAI").max_session == timedelta(hours=1)
    assert broker.config.presets["team"].env["OPENAI_API_KEY"].secret == "MY_OPENAI"
    assert any("secret_renamed" in line for line in harness.echoed)


def test_check_rename_changes_nothing(harness: Harness) -> None:
    broker = harness.broker
    files_before = {path.name: path.read_bytes() for path in harness.data_dir.iterdir()}
    renamed_config, presets_text, config = broker.check_rename("OPENAI_API_KEY", "MY_OPENAI")
    assert renamed_config is not None and "MY_OPENAI" in renamed_config and presets_text is not None
    assert config.policy_for("MY_OPENAI").max_session == timedelta(hours=1)
    assert {path.name: path.read_bytes() for path in harness.data_dir.iterdir()} == files_before
    assert "OPENAI_API_KEY" in broker.vault and broker.config.presets["team"].env["OPENAI_API_KEY"].secret == "OPENAI_API_KEY"


def test_save_presets_checks_the_whole_file_before_writing_it(harness: Harness) -> None:
    presets_path = harness.data_dir / "presets.yaml"
    before = presets_path.read_text()
    with pytest.raises(ConfigError, match="not in the vault"):
        harness.broker.save_presets({"broken": {"env": {"A": "NOT_STORED"}}})
    assert presets_path.read_text() == before
    harness.broker.save_presets({"solo": {"max_session": "30m", "env": {"DB": {"secret": "DATABASE_URL", "approval": "per-run"}}}})
    assert list(harness.broker.config.presets) == ["solo"]
    assert load_config(harness.data_dir, set(harness.broker.vault.names())).presets["solo"].env["DB"].approval == "per-run"
    assert any("presets_saved" in line and "dbwork" in line and "solo" in line for line in harness.echoed)


def test_saving_settings_or_renaming_refuses_a_config_changed_on_disk(harness: Harness) -> None:
    config_path = harness.data_dir / "config.yaml"
    config_path.write_text(config_path.read_text().replace("max_session: 2h", "max_session: 3h"))
    with pytest.raises(RequestError, match="changed on disk"):
        harness.broker.save_settings(harness.broker.config.settings)
    with pytest.raises(RequestError, match="changed on disk"):
        harness.broker.check_rename("OPENAI_API_KEY", "MY_OPENAI")
    assert "3h" in config_path.read_text()


def test_settings_saved_from_the_console_are_what_the_broker_loads(harness: Harness) -> None:
    settings = harness.broker.config.settings
    entries = {**settings.secrets, "DATABASE_URL": SecretEntry(approval="per-run", description="Production, read-only")}
    harness.broker.save_settings(replace(settings, notify=False, secrets=entries))
    reloaded = load_config(harness.data_dir, set(harness.broker.vault.names()))
    assert reloaded.notify is False and harness.broker.config.notify is False
    assert reloaded.policy_for("DATABASE_URL").approval == "per-run"
    assert reloaded.description_for("DATABASE_URL") == "Production, read-only"
    assert harness.broker.list_payload()["secrets"][0] == {"name": "DATABASE_URL", "approval": "per-run", "max_session": "2h", "description": "Production, read-only"}


def test_removing_a_secret_takes_its_settings_and_refuses_one_a_preset_uses(harness: Harness) -> None:
    broker = harness.broker
    with pytest.raises(RequestError, match="used by presets dbwork, team"):
        broker.remove_secret("OPENAI_API_KEY")
    broker.save_presets({"db": {"env": {"DB": "DATABASE_URL"}}})
    broker.remove_secret("OPENAI_API_KEY")
    assert "OPENAI_API_KEY" not in Vault.open(harness.data_dir / "vault.age", PASSPHRASE).names()
    assert "OPENAI_API_KEY" not in broker.config.settings.secrets
    assert "OPENAI_API_KEY" not in (harness.data_dir / "config.yaml").read_text()

import argparse
import base64
import io
import re
import json
import os
import stat
import time
from pathlib import Path

import pytest

from envh.common import peek
from envh.tools.saved_files import ask_where
from tests.conftest import HEX, random_text
from envh.tools.scanner import COMPILED_PATTERNS, STANDALONE_KINDS, VALUE_PATTERNS, Progress, classify_name, looks_fake, looks_placeholder, main, place_of, scan_paths, scan_text, shown_value, value_id

OPENAI = "sk-proj-" + random_text("openai", 48)
OPENROUTER = "sk-or-v1-" + random_text("openrouter", 64, HEX)
ANTHROPIC = "sk-ant-api03-" + random_text("anthropic", 48)
GEMINI = "AIza" + random_text("gemini", 35)
PPLX = "pplx-" + random_text("perplexity", 48)
FRED = random_text("fred", 32, HEX)
TRANSCRIPT = json.dumps({"role": "assistant", "content": f"use export OPENAI_API_KEY={OPENAI} then run"})


def test_value_patterns_found_in_transcript_and_history(tmp_path: Path) -> None:
    (tmp_path / "transcript.jsonl").write_text(TRANSCRIPT + "\n")
    (tmp_path / ".bash_history").write_text(f"curl -H 'Authorization: Bearer {OPENROUTER}' https://openrouter.ai\nls\n")
    (tmp_path / "notes.md").write_text(f"anthropic key {ANTHROPIC}\ngemini {GEMINI}\nperplexity {PPLX}\n")
    hits, scanned, unreadable = scan_paths([tmp_path], 1024 * 1024)
    kinds = {(hit.path.name, hit.kind) for hit in hits}
    assert ("transcript.jsonl", "openai") in kinds
    assert (".bash_history", "openrouter") in kinds
    assert {("notes.md", "anthropic"), ("notes.md", "google-api-key"), ("notes.md", "perplexity")} <= kinds
    assert scanned == 3 and unreadable == []
    rendered = " ".join(f"{hit.describe()}{hit.name}" for hit in hits)
    for secret in (OPENAI, OPENROUTER, ANTHROPIC, GEMINI, PPLX):
        middle = len(secret) // 2
        assert secret not in rendered and secret[middle - 4:middle + 4] not in rendered


def test_named_assignments_cover_formatless_keys_and_skip_placeholders() -> None:
    text = "\n".join([
        f"FRED_API_KEY={FRED}",
        f'SERP_API_KEY: "{FRED[::-1]}"',
        "ASKNEWS_CLIENT_SECRET=${ASKNEWS_CLIENT_SECRET}",
        "OPENAI_API_KEY=your-key-here-please-replace",
        "HYPERBROWSER_API_KEY=aaaaaaaaaaaaaaaaaaaaaaaa",
        "LOG_LEVEL=info",
        f"api_key = '{FRED}'",
    ])
    hits = list(scan_text(Path("x.env"), text))
    names = [hit.name for hit in hits]
    assert names == ["FRED_API_KEY", "SERP_API_KEY", "api_key"]
    assert hits[0].fingerprint == hits[2].fingerprint
    assert looks_placeholder("xxxxxxxxxxxxxxxxxxxx") and not looks_placeholder(FRED)


def test_an_escaped_newline_before_a_name_is_not_part_of_it() -> None:
    [hit] = scan_text(Path("transcript.jsonl"), json.dumps({"content": f"ok\nFRED_API_KEY={FRED}"}))
    assert (hit.name, hit.kind, hit.confidence) == ("FRED_API_KEY", "fred", "medium")


def test_same_value_in_a_value_pattern_is_not_double_reported() -> None:
    hits = list(scan_text(Path("f"), f"OPENAI_API_KEY={OPENAI}\n"))
    assert [hit.kind for hit in hits] == ["openai"]


def test_skips_binaries_large_files_and_vendor_dirs_but_reads_git_config(tmp_path: Path) -> None:
    (tmp_path / "binary.db").write_bytes(b"\x00\x01" + OPENAI.encode())
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text(OPENAI)
    (tmp_path / "big.log").write_text(OPENAI + "\n" + "x" * 2_000_000)
    git = tmp_path / "repo" / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text("[remote \"origin\"]\n\turl = https://user:" + "ghp_" + "a" * 36 + "@github.com/x/y.git\n")
    (git / "objects").mkdir()
    (git / "objects" / "blob").write_text(OPENAI)
    hits, scanned, _ = scan_paths([tmp_path], 1024 * 1024)
    assert {hit.path.name for hit in hits} == {"config"}
    assert {hit.kind for hit in hits} == {"github"}


def test_database_url_and_private_key() -> None:
    text = "DATABASE_URL=postgres://admin:hunter2secret@db.internal:5432/app\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    kinds = [hit.kind for hit in scan_text(Path("f"), text)]
    assert kinds == ["database-url", "private-key"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read every file")
def test_unreadable_files_are_reported_not_skipped(tmp_path: Path) -> None:
    locked = tmp_path / "locked.txt"
    locked.write_text(OPENAI + "\n")
    locked.chmod(0)
    try:
        hits, scanned, unreadable = scan_paths([tmp_path], 1024 * 1024)
    finally:
        locked.chmod(0o600)
    assert hits == [] and scanned == 0 and unreadable == [locked]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list every directory")
def test_unreadable_directories_are_reported_not_skipped(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "notes.md").write_text(OPENAI + "\n")
    locked.chmod(0)
    try:
        hits, _, unreadable = scan_paths([tmp_path], 1024 * 1024)
    finally:
        locked.chmod(0o700)
    assert hits == [] and unreadable == [locked]


def test_long_lines_are_scanned_to_the_end_in_linear_time() -> None:
    blob = "6080604052" * 100_000
    line = json.dumps({"content": blob + f" FRED_API_KEY={FRED} " + "a" * 300_000})
    started = time.perf_counter()
    hits = list(scan_text(Path("transcript.jsonl"), line))
    assert time.perf_counter() - started < 2
    assert [hit.name for hit in hits] == ["FRED_API_KEY"]


SAMPLES = {
    "openrouter": OPENROUTER,
    "anthropic": ANTHROPIC,
    "openai": OPENAI,
    "openai-legacy": "sk-" + "Ab12" * 5 + "T3BlbkFJ" + "Cd34" * 5,
    "xai": "xai-" + "A1b2C3d4E5" * 8,
    "deepseek": "sk-" + "0123456789abcdef" * 2,
    "perplexity": PPLX,
    "google-api-key": GEMINI,
    "google-oauth": "GOCSPX-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
    "google-service-account": "eyJ0eXBlIjoic2VydmljZV9hY2NvdW50" + "IiwicHJvamVjdF9pZCI6" * 6,
    "groq": "gsk_" + "a1B2c3D4e5" * 4,
    "together": "tgp_v1_" + "a1B2c3D4e5" * 4,
    "fireworks": "fw_" + "a1B2c3D4e5" * 3,
    "cerebras": "csk-" + "a1b2c3d4e5" * 5,
    "huggingface": "hf_" + "a1B2c3D4e5" * 3,
    "replicate": "r8_" + "a1B2c3D4e5" * 3,
    "elevenlabs": "sk_" + "0123456789abcdef" * 3,
    "e2b": "e2b_" + "0123456789abcdef" * 2,
    "tavily": "tvly-" + "a1B2c3D4e5" * 2,
    "firecrawl": "fc-" + "0123456789abcdef" * 2,
    "langsmith": "lsv2_pt_" + "0123456789abcdef" * 2 + "_" + "0123456789",
    "pinecone": "pcsk_" + "a1B2c_" + "a1B2c3D4e5" * 5,
    "supabase": "sbp_" + "0123456789abcdef" * 2 + "01234567",
    "github": "ghp_" + "a1B2" * 9,
    "gitlab": "glpat-" + "a1B2c3D4e5F6g7H8i9J0",
    "slack": "xoxb-1234567890-abcdefghij",
    "slack-webhook": "https://hooks.slack.com/services/T00000000/B00000000/" + "Xx" * 12,
    "discord-webhook": "https://discord.com/api/webhooks/123456789012345678/" + "a1B2c3D4e5" * 6,
    "twilio": "SK" + "0123456789abcdef" * 2,
    "sendgrid": "SG.a1B2c3D4e5f6g7h8.i9J0k1L2m3N4o5P6",
    "stripe": "sk_live_" + "a1B2c3D4" * 3,
    "stripe-webhook": "whsec_" + "a1B2c3D4e5" * 4,
    "shopify": "shpat_" + "0123456789abcdef" * 2,
    "square": "sq0atp-" + "a1B2c3D4e5F6g7H8i9J0k1L2",
    "aws-access-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "aws-bedrock": "ABSK" + "QmVkcm9jayBrZXk" * 8,
    "cloudflare-origin-ca": "v1.0-" + "0123456789abcdef01234567" + "-" + "0123456789abcdef" * 9 + "01",
    "digitalocean": "dop_v1_" + "0123456789abcdef" * 4,
    "heroku": "HRKU-AA" + "a1B2c3D4e5" * 5 + "a1B2c3D4",
    "flyio": "fo1_" + "a1B2c3D4e5" * 4 + "a1B",
    "planetscale": "pscale_tkn_" + "a1B2c3D4e5" * 4,
    "databricks": "dapi" + "0123456789abcdef" * 2,
    "doppler": "dp.pt." + "a1B2c3D4e5" * 4 + "a1B",
    "hashicorp-vault": "hvs." + "a1B2c3D4e5" * 10,
    "grafana": "glsa_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6" + "_0123abcd",
    "sentry": "sntryu_" + "0123456789abcdef" * 4,
    "sentry-dsn": "https://" + "0123456789abcdef" * 2 + "@o123456.ingest.sentry.io/7654321",
    "new-relic": "NRAK-" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N",
    "posthog": "phx_" + "a1B2c3D4e5" * 4 + "a1B",
    "linear": "lin_api_" + "a1B2c3D4e5" * 4,
    "atlassian": "ATATT3" + "xFfGF0" * 31,
    "airtable": "pat" + "a1B2c3D4e5F6g7" + "." + "0123456789abcdef" * 4,
    "notion": "ntn_" + "a1B2c3D4e5" * 4,
    "postman": "PMAK-" + "0123456789abcdef01234567" + "-" + "0123456789abcdef" * 2 + "01",
    "npm": "npm_" + "a1B2c3D4e5" * 3 + "a1B2c3",
    "pypi": "pypi-AgEIcHlwaS5vcmc" + "a1B2c3D4e5" * 6,
    "onepassword": "A3-ABC123-DEF456GHI78-JKL90-MNO12-PQR34",
    "age-secret-key": "AGE-SECRET-KEY-1" + "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" + "QPZRY9X8GF2TVDW0S3JN54KHCE",
    "resend": "re_" + "a1B2c3D4" + "_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
    "browserbase": "bb_live_" + "a1B2c3D4e5" * 3,
    "jwt": "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q",
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\\n" + "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0t",
    "database-url": "postgres://admin:hunter2secret@db.internal:5432/app",
}


def test_every_value_pattern_has_a_sample() -> None:
    assert set(SAMPLES) == {kind for kind, _ in VALUE_PATTERNS}


@pytest.mark.parametrize(("kind", "sample"), SAMPLES.items())
def test_each_key_format_is_found(kind: str, sample: str) -> None:
    hits = list(scan_text(Path("notes.md"), f"noise before {sample} and after\n"))
    assert [hit.kind for hit in hits] == [kind]


def test_every_format_starts_with_literal_text_so_the_scan_stays_fast() -> None:
    for kind, regex, _ in COMPILED_PATTERNS:
        assert re.match(r"[A-Za-z0-9_-]{2}", regex.pattern), kind


def test_overlapping_formats_report_the_value_once() -> None:
    hits = list(scan_text(Path("f"), "sb_secret_" + "a1B2c3D4e5" * 4 + "\n"))
    assert [hit.kind for hit in hits] == ["supabase"]


def test_installed_packages_caches_and_tagged_folders_are_skipped(tmp_path: Path) -> None:
    skipped = (".local/share/uv/python", "miniconda3/lib", ".vscode/extensions/tool", ".config/Slack/Cache", "project/target", ".var/app/org.mozilla.thunderbird_esr/cache", "project/web/.next/server")
    for folder in skipped:
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "leak.txt").write_text(OPENAI)
    (tmp_path / "project" / "target" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (tmp_path / "project" / "notes.md").write_text(OPENAI)
    (tmp_path / ".cache" / "huggingface").mkdir(parents=True)
    (tmp_path / ".cache" / "huggingface" / "token").write_text(SAMPLES["huggingface"])
    hits, _, _ = scan_paths([tmp_path], 1024 * 1024)
    assert {str(hit.path.relative_to(tmp_path)) for hit in hits} == {"project/notes.md", ".cache/huggingface/token"}


def test_progress_draws_a_bar_with_the_folder_and_clears_it() -> None:
    stream = io.StringIO()
    progress = Progress(stream)
    progress.scanning(500_000_000, 1_000_000_000, Path.home() / "code" / "bot" / "src" / "deep")
    assert stream.getvalue() == "\r\x1b[K  [██████████░░░░░░░░░░] 50% · 0.5 of 1.0 GB · ~/code/bot/src"
    progress.clear()
    assert stream.getvalue().endswith("\r\x1b[K")


def test_results_are_saved_to_a_private_folder_and_the_terminal_gets_a_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    for index in range(30):
        (home / f"project{index}").mkdir(parents=True)
        (home / f"project{index}" / "notes.md").write_text(f"key {OPENAI}\nFRED_API_KEY={FRED}\ncache_key={FRED[::-1]}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)
    exit_code, saved_in = main(argparse.Namespace(paths=["."], json=False, max_size=25, out=None))
    assert exit_code == 1
    [report_dir] = (home / "envh-backups-and-scans" / "scans").iterdir()
    assert saved_in == report_dir
    assert "envh scan" in (home / "envh-backups-and-scans" / "README.txt").read_text()
    assert stat.S_IMODE(report_dir.stat().st_mode) == 0o700
    assert sorted(entry.name for entry in report_dir.iterdir()) == ["files.txt", "hits.json", "report.txt"]
    assert all(stat.S_IMODE(entry.stat().st_mode) == 0o600 for entry in report_dir.iterdir())
    expected = sorted(str(home / f"project{index}" / "notes.md") for index in range(30))
    assert (report_dir / "files.txt").read_text().splitlines() == expected
    assert len(json.loads((report_dir / "hits.json").read_text())["hits"]) == 90
    report = (report_dir / "report.txt").read_text()
    assert report.index("== Summary ==") < report.index("== High confidence") < report.index("== Medium confidence") < report.index("== Low confidence") < report.index("== Every hit by file ==")
    assert f"{shown_value(OPENAI, len('sk-'))} ({len(OPENAI)} chars, id {value_id(OPENAI)})  openai: in 30 files, 30 places" in report
    assert f"{peek(FRED)} ({len(FRED)} chars, id {value_id(FRED)})  fred  FRED_API_KEY: in 30 files, 30 places" in report
    assert any("In short (details at the top)" in line for line in report.splitlines()[-6:])
    shown = capsys.readouterr().out
    assert f"Results saved in ~/envh-backups-and-scans/scans/{report_dir.name}" in shown and "notes.md" not in shown
    assert "90 key-shaped strings in 30 files" in shown and "known key format: 1 distinct value in 30 files" in shown
    for entry in report_dir.iterdir():
        assert OPENAI not in entry.read_text() and FRED not in entry.read_text()
    assert scan_paths([report_dir], 1024 * 1024)[0] == []


def test_a_later_home_scan_skips_saved_results_but_still_reports_import_backups(tmp_path: Path) -> None:
    saved = tmp_path / "envh-backups-and-scans"
    (saved / "scans" / "20261017-120000").mkdir(parents=True)
    (saved / "scans" / "20261017-120000" / "report.txt").write_text(f"{OPENAI}\n")
    (saved / "import-backups" / "20261017-120000").mkdir(parents=True)
    (saved / "import-backups" / "20261017-120000" / ".env").write_text(f"OPENAI_API_KEY={OPENAI}\n")
    hits, _, _ = scan_paths([tmp_path], 1024 * 1024)
    assert [str(hit.path.relative_to(tmp_path)) for hit in hits] == ["envh-backups-and-scans/import-backups/20261017-120000/.env"]


def test_the_out_folder_gets_the_dated_results_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "notes.md").write_text(f"{OPENAI}\n")
    assert main(argparse.Namespace(paths=[str(tmp_path / "code")], json=True, max_size=25, out=str(tmp_path / "results")))[0] == 1
    [report_dir] = (tmp_path / "results").iterdir()
    assert (report_dir / "files.txt").read_text() == f"{tmp_path / 'code' / 'notes.md'}\n"
    assert json.loads(capsys.readouterr().out)["hits"][0]["confidence"] == "high"


@pytest.mark.parametrize(("name", "confidence", "service"), [
    ("OPENROUTER_API_KEY", "medium", "openrouter"),
    ("METACULUS_TOKEN", "medium", "metaculus"),
    ("METACULUS_OPENAI_API_KEY", "medium", "openai"),
    ("PERSONAL_ELEVEN_LABS_API_KEY", "medium", "elevenlabs"),
    ("GOOGLE_SHEETS_USER_CREDENTIALS_B64", "medium", "google"),
    ("ASKNEWS_CLIENT_SECRET", "medium", "asknews"),
    ("apiKey", "medium", "unknown"),
    ("DB_PASS", "medium", "database"),
    ("cache_key", "low", "unknown"),
    ("api_key_id", "low", "unknown"),
    ("monkeypatch", "low", "unknown"),
    ("prompt_tokens", "low", "unknown"),
    ("NOPASSWD", "low", "unknown"),
    ("publicKeyToken", "low", "unknown"),
    ("getAccessToken", "low", "unknown"),
    ("withCredentials", "low", "unknown"),
    ("updateApiKey", "low", "unknown"),
    ("refresh_token", "medium", "unknown"),
])
def test_names_set_the_confidence_and_service(name: str, confidence: str, service: str) -> None:
    assert classify_name(name) == (confidence, service)


def test_values_are_shown_like_the_console_shows_them_after_the_formats_fixed_prefix() -> None:
    [openrouter] = scan_text(Path("f"), f"{OPENROUTER}\n")
    assert openrouter.shown == "sk-or-v1-" + OPENROUTER[9:13] + "\u2026" + OPENROUTER[-4:] and openrouter.confidence == "high"
    sixteen = random_text("short key", 16)
    [short] = scan_text(Path("f"), f"MY_API_KEY={sixteen}\n")
    assert short.shown == sixteen[0] + "\u2026" + sixteen[15] and short.confidence == "medium"


def test_ask_where_keeps_the_default_or_takes_a_typed_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    default = tmp_path / "envh-backups-and-scans" / "scans"
    prompts: list[str] = []
    assert ask_where("the results", default, lambda prompt: prompts.append(prompt) or "") == default
    assert prompts == ["Save the results in a new dated folder inside ~/envh-backups-and-scans/scans? Enter to accept, or type another folder: "]
    assert ask_where("the results", default, lambda prompt: " ~/Desktop ") == tmp_path / "Desktop"


@pytest.mark.parametrize("kind", sorted(STANDALONE_KINDS))
def test_short_prefix_formats_only_count_when_they_stand_alone(kind: str) -> None:
    assert [hit.kind for hit in scan_text(Path("f"), f"noise x{SAMPLES[kind]} more\n")] == []


def test_a_fixed_length_format_inside_a_longer_string_is_not_a_key() -> None:
    assert list(scan_text(Path("f"), f"digest {SAMPLES['twilio']}0123abcd\n")) == []


def test_a_key_prefix_inside_a_base64_blob_is_not_a_key() -> None:
    blob = "iVBORw0KGgo" + "QmFzZTY0" * 40 + "ABSK" + "QmVkcm9jayBrZXk" * 30 + "AIza" + "QmFzZTY0" * 40
    assert list(scan_text(Path("transcript.jsonl"), json.dumps({"image": blob}))) == []


@pytest.mark.parametrize("kind", ["openai-legacy", "deepseek", "stripe", "aws-access-key", "github"])
def test_placeholder_samples_are_fake_and_random_keys_are_not(kind: str) -> None:
    [placeholder] = scan_text(Path("README.md"), f"{SAMPLES[kind]}\n")
    assert placeholder.confidence == "fake"
    assert [hit.confidence for hit in scan_text(Path("f"), f"{OPENROUTER} {OPENAI}\n")] == ["high", "high"]
    assert looks_fake("AKIAIOSFODNN7EXAMPLE", 4) and looks_fake("your-openai-key-goes-here-1234") and not looks_fake(FRED)


def test_code_that_reads_a_key_is_not_reported() -> None:
    text = "\n".join([
        "api_key = self.settings.openai_api_key",
        "OPENROUTER_API_KEY: OPENROUTER_API_KEY_PROD",
        "const secretKey = process.env.STRIPE_SECRET_KEY",
        "authToken: refreshAccessTokenNow",
        "private_key = CertificateIssuerTypes",
        f"FRED_API_KEY={FRED}",
    ])
    assert [hit.name for hit in scan_text(Path("app.py"), text)] == ["FRED_API_KEY"]


@pytest.mark.parametrize(("path", "place"), [
    ("/h/.config/Cursor/User/History/-1a2b/abcd", "editor history"),
    ("/h/.claude/projects/x/session.jsonl", "agent transcripts"),
    ("/h/.codex/sessions/2026/x.jsonl", "agent transcripts"),
    ("/h/.claude/ide/26078.lock", "credential files"),
    ("/h/.claude/todos/x.json", "agent transcripts"),
    ("/h/.claude/.credentials.json", "credential files"),
    ("/h/.config/gh/hosts.yml", "credential files"),
    ("/h/.aws/credentials", "credential files"),
    ("/h/.codex/.tmp/plugins/x/README.md", "agent app data"),
    ("/h/code/app/.claude/worktrees/fix-x/README.md", "worktree copies"),
    ("/h/code/app/.kilo/worktrees/meteor/.env.template", "worktree copies"),
    ("/h/code/app/logs/run.log", "logs"),
    ("/h/code/app/temp/1_bot_logs/run_bot.txt", "logs"),
    ("/h/code/app/tests/test_keys.py", "tests"),
    ("/h/code/app/README.md", "docs and notes"),
    ("/h/code/app/.env.local", ".env files"),
    ("/h/code/app/output_runs/traces.jsonl", "data and output"),
    ("/h/code/app/main.py", "code and config"),
    ("/h/code/app/catalog/blog/post", "other"),
    ("/h/.local/share/Trash/files/.env", "trash"),
    ("/elsewhere/.claude/projects/x.py", "code and config"),
])
def test_each_hit_gets_a_place(path: str, place: str) -> None:
    assert place_of(path, home="/h") == place


@pytest.mark.parametrize(("url", "fake"), [
    ("postgres://postgres:postgres@localhost:5432/metaculus", True),
    ("postgres://app:" + random_text("db", 24) + "@localhost/app", True),
    ("mysql://admin:admin@db.internal:3306/app", True),
    ("postgres://app:" + random_text("db", 24) + "@db.internal.acme-prod.net:5432/app", False),
])
def test_default_and_local_database_logins_are_fake(url: str, fake: bool) -> None:
    [hit] = scan_text(Path("README.md"), f"DATABASE_URL={url}\n")
    assert (hit.kind, hit.confidence == "fake") == ("database-url", fake)


def test_a_private_key_header_without_key_material_is_not_a_key() -> None:
    assert list(scan_text(Path("pem.py"), 'if line.startswith("-----BEGIN PRIVATE KEY-----"):\n')) == []
    escaped = json.dumps({"private_key": "-----BEGIN PRIVATE KEY-----\n" + random_text("pem", 64) + "\n"})
    assert [hit.kind for hit in scan_text(Path("service.json"), escaped)] == ["private-key"]


def test_a_database_url_stops_at_escaped_newlines_and_markdown() -> None:
    url = "postgres://app:" + random_text("db password", 16) + "@db.acme-prod.net:5432/app"
    lines = [json.dumps({"text": f"use {url}\n\nthen run"}), f"[db]({url}) and `{url}`"]
    assert {hit.length for hit in scan_text(Path("t.jsonl"), "\n".join(lines))} == {len(url)}


def test_a_demo_jwt_is_fake_and_a_uuid_key_is_kept() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"iss": "supabase-demo", "role": "anon"}).encode()).decode().rstrip("=")
    [demo] = scan_text(Path("f"), f"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.{payload}.{random_text('sig', 43)}\n")
    assert (demo.kind, demo.confidence) == ("jwt", "fake")
    exa = "facedbad-7a3b-4c8d-9e5f-b2a1c3d4e5f6"
    assert [(hit.name, hit.confidence) for hit in scan_text(Path(".env"), f"EXA_API_KEY={exa}\n")] == [("EXA_API_KEY", "medium")]

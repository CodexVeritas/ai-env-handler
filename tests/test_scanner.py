import argparse
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
from envh.tools.scanner import COMPILED_PATTERNS, STANDALONE_KINDS, VALUE_PATTERNS, Progress, classify_name, looks_placeholder, main, scan_paths, scan_text, value_id

OPENAI = "sk-proj-" + "A1b2C3d4" * 6
OPENROUTER = "sk-or-v1-" + "0123456789abcdef" * 4
ANTHROPIC = "sk-ant-api03-" + "Zz9yY8xX7wW6vV5u" * 3
GEMINI = "AIza" + "Syd8f7g6h5j4k3l2m1n0pQrStUvWxYz0123"
PPLX = "pplx-" + "q1w2e3r4t5y6u7i8o9p0a1s2d3f4g5h6j7k8"
FRED = "3f2a9c8b7d6e5f4a3b2c1d0e9f8a7b6c"
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
        assert secret not in rendered and secret[:10] not in rendered


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
    text = "DATABASE_URL=postgres://admin:hunter2secret@db.internal:5432/app\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
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
    "private-key": "-----BEGIN RSA PRIVATE KEY-----",
    "database-url": "postgres://admin:hunter2secret@db.internal:5432/app",
}


def test_every_value_pattern_has_a_sample() -> None:
    assert set(SAMPLES) == {kind for kind, _ in VALUE_PATTERNS}


@pytest.mark.parametrize(("kind", "sample"), SAMPLES.items())
def test_each_key_format_is_found(kind: str, sample: str) -> None:
    hits = list(scan_text(Path("notes.md"), f"noise before {sample} and after\n"))
    assert [hit.kind for hit in hits] == [kind]


def test_every_format_starts_with_literal_text_so_the_scan_stays_fast() -> None:
    for kind, regex in COMPILED_PATTERNS:
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
    assert main(argparse.Namespace(paths=["."], json=False, max_size=25, out=None)) == 1
    [report_dir] = (home / "envh-backups-and-scans" / "scans").iterdir()
    assert "envh scan" in (home / "envh-backups-and-scans" / "README.txt").read_text()
    assert stat.S_IMODE(report_dir.stat().st_mode) == 0o700
    assert sorted(entry.name for entry in report_dir.iterdir()) == ["files.txt", "hits.json", "report.txt"]
    assert all(stat.S_IMODE(entry.stat().st_mode) == 0o600 for entry in report_dir.iterdir())
    expected = sorted(str(home / f"project{index}" / "notes.md") for index in range(30))
    assert (report_dir / "files.txt").read_text().splitlines() == expected
    assert len(json.loads((report_dir / "hits.json").read_text())["hits"]) == 90
    report = (report_dir / "report.txt").read_text()
    assert report.index("== Summary ==") < report.index("== High confidence") < report.index("== Medium confidence") < report.index("== Low confidence") < report.index("== Every hit by file ==")
    assert f"{peek(OPENAI)} ({len(OPENAI)} chars, id {value_id(OPENAI)})  openai: in 30 files, 30 places" in report
    assert f"{peek(FRED)} ({len(FRED)} chars, id {value_id(FRED)})  fred  FRED_API_KEY: in 30 files, 30 places" in report
    assert "In short (details at the top)" in report.splitlines()[-4]
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
    assert main(argparse.Namespace(paths=[str(tmp_path / "code")], json=True, max_size=25, out=str(tmp_path / "results"))) == 1
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
])
def test_names_set_the_confidence_and_service(name: str, confidence: str, service: str) -> None:
    assert classify_name(name) == (confidence, service)


def test_values_are_shown_like_the_console_shows_them() -> None:
    [openrouter] = scan_text(Path("f"), f"{OPENROUTER}\n")
    assert openrouter.shown == OPENROUTER[:4] + "\u2026" + OPENROUTER[-4:] and openrouter.confidence == "high"
    [short] = scan_text(Path("f"), f"MY_API_KEY={FRED[:16]}\n")
    assert short.shown == FRED[0] + "\u2026" + FRED[15] and short.confidence == "medium"


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

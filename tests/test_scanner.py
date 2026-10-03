import json
import os
import time
from pathlib import Path

import pytest

from envh.tools.scanner import looks_placeholder, scan_paths, scan_text

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
    assert {("notes.md", "anthropic"), ("notes.md", "google-gemini"), ("notes.md", "perplexity")} <= kinds
    assert scanned == 3 and unreadable == []
    rendered = " ".join(f"{hit.prefix}{hit.fingerprint}{hit.name}" for hit in hits)
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

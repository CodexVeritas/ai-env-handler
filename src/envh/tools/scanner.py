"""`envh scan`: find where key-shaped strings live on this machine, without ever printing a value. Standard library only."""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, TextIO

from envh.common import peek, value_id
from envh.tools.saved_files import FOLDER_NAME, SCANS, ask_where, new_dated_folder, saved_files_root, say_where_saved, shown_path

# Provider formats that are distinctive enough to recognize anywhere, including transcripts and shell history. Each
# regex starts with literal text, which lets the regex engine jump straight to candidates; one that started with a
# character class would be tried at every position and make the scan several times slower. Where gitleaks
# (github.com/gitleaks/gitleaks) has a rule for a provider, the format follows it.
VALUE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openrouter", (r"sk-or-v1-[a-f0-9]{64}",)),
    ("anthropic", (r"sk-ant-[A-Za-z0-9_-]{32,}",)),
    ("openai", (r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{32,}",)),
    ("openai-legacy", (r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}",)),
    ("xai", (r"xai-[A-Za-z0-9]{70,}",)),
    ("deepseek", (r"sk-[a-f0-9]{32}",)),
    ("perplexity", (r"pplx-[A-Za-z0-9]{32,}",)),
    ("google-api-key", (r"AIza[0-9A-Za-z_-]{35}",)),
    ("google-oauth", (r"GOCSPX-[A-Za-z0-9_-]{28}", r"ya29\.[A-Za-z0-9_-]{30,}")),
    ("google-service-account", (
        r"eyJ0eXBlIjoic2VydmljZV9hY2NvdW50[A-Za-z0-9+/=]{100,}", r"eyJ0eXBlIjogInNlcnZpY2VfYWNjb3Vu[A-Za-z0-9+/=]{100,}",
        r"ewogICJ0eXBlIjogInNlcnZpY2VfYWNjb3Vu[A-Za-z0-9+/=]{100,}",
    )),
    ("groq", (r"gsk_[A-Za-z0-9]{40,}",)),
    ("together", (r"tgp_v1_[A-Za-z0-9_-]{30,}",)),
    ("fireworks", (r"fw_[A-Za-z0-9]{20,}",)),
    ("cerebras", (r"csk-[a-z0-9]{40,}",)),
    ("huggingface", (r"hf_[A-Za-z0-9]{30,}", r"api_org_[A-Za-z]{34}")),
    ("replicate", (r"r8_[A-Za-z0-9]{30,}",)),
    ("elevenlabs", (r"sk_[a-f0-9]{48}",)),
    ("e2b", (r"e2b_[a-f0-9]{32,}",)),
    ("tavily", (r"tvly-[A-Za-z0-9_-]{20,}",)),
    ("firecrawl", (r"fc-[a-f0-9]{32}",)),
    ("langsmith", (r"lsv2_(?:pt|sk)_[a-f0-9]{32}_[a-f0-9]{10}",)),
    ("pinecone", (r"pcsk_[A-Za-z0-9_]{50,}",)),
    ("supabase", (r"sbp_[a-f0-9]{40}", r"sb_secret_[A-Za-z0-9_-]{20,}")),
    ("github", (r"gh[pousr]_[A-Za-z0-9]{36,}", r"github_pat_[A-Za-z0-9_]{60,}")),
    ("gitlab", (r"gl(?:pat|dt|rt|ft|imt|soat|ffct|oas|agent|ptt)-[A-Za-z0-9_-]{20,}", r"glcbt-[A-Za-z0-9]{1,5}_[A-Za-z0-9_-]{20,}")),
    ("slack", (r"xox[abeoprs]-[A-Za-z0-9-]{10,}", r"xapp-[0-9]-[A-Za-z0-9]+-[0-9]+-[A-Za-z0-9]+")),
    ("slack-webhook", (r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}",)),
    ("discord-webhook", (r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]{50,}",)),
    ("twilio", (r"SK[0-9a-fA-F]{32}",)),
    ("sendgrid", (r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",)),
    ("stripe", (r"sk_(?:live|test|prod)_[A-Za-z0-9]{20,}", r"rk_(?:live|test|prod)_[A-Za-z0-9]{20,}")),
    ("stripe-webhook", (r"whsec_[A-Za-z0-9]{32,}",)),
    ("shopify", (r"shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}",)),
    ("square", (r"sq0(?:atp|csp)-[A-Za-z0-9_-]{22,60}",)),
    ("aws-access-key", tuple(rf"{prefix}[A-Z2-7]{{16}}" for prefix in ("AKIA", "ASIA", "ABIA", "ACCA")) + (r"A3T[A-Z0-9][A-Z2-7]{16}",)),
    ("aws-bedrock", (r"ABSK[A-Za-z0-9+/]{109,269}={0,2}",)),
    ("cloudflare-origin-ca", (r"v1\.0-[a-f0-9]{24}-[a-f0-9]{146}",)),
    ("digitalocean", (r"do[opr]_v1_[a-f0-9]{64}",)),
    ("heroku", (r"HRKU-AA[A-Za-z0-9_-]{58}",)),
    ("flyio", (r"fo1_[A-Za-z0-9_-]{43}", r"fm1[ar]_[A-Za-z0-9+/]{100,}={0,3}", r"fm2_[A-Za-z0-9+/]{100,}={0,3}")),
    ("planetscale", (r"pscale_(?:tkn|oauth|pw)_[A-Za-z0-9_=.-]{32,64}",)),
    ("databricks", (r"dapi[a-f0-9]{32}(?:-[0-9])?",)),
    ("doppler", (r"dp\.pt\.[A-Za-z0-9]{43}",)),
    ("hashicorp-vault", (r"hvs\.[A-Za-z0-9_-]{90,120}", r"hvb\.[A-Za-z0-9_-]{138,300}")),
    ("grafana", (r"glc_[A-Za-z0-9+/]{32,400}={0,3}", r"glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}", r"eyJrIjoi[A-Za-z0-9]{70,400}={0,3}")),
    ("sentry", (r"sntryu_[a-f0-9]{64}", r"sntrys_eyJpYXQiO[A-Za-z0-9+/=_]{50,}")),
    ("sentry-dsn", (r"https://[a-f0-9]{32}@[A-Za-z0-9.-]*sentry\.io/[0-9]+",)),
    ("new-relic", (r"NRAK-[A-Za-z0-9]{27}",)),
    ("posthog", (r"phx_[A-Za-z0-9]{40,}",)),
    ("linear", (r"lin_api_[A-Za-z0-9]{40}",)),
    ("atlassian", (r"ATATT3[A-Za-z0-9_=-]{186}",)),
    ("airtable", (r"pat[A-Za-z0-9]{14}\.[a-f0-9]{64}",)),
    ("notion", (r"ntn_[A-Za-z0-9]{40,}", r"secret_[A-Za-z0-9]{40,}")),
    ("postman", (r"PMAK-[a-fA-F0-9]{24}-[a-fA-F0-9]{34}",)),
    ("npm", (r"npm_[A-Za-z0-9]{36}",)),
    ("pypi", (r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,1000}",)),
    ("onepassword", (r"A3-[A-Z0-9]{6}-(?:[A-Z0-9]{11}|[A-Z0-9]{6}-[A-Z0-9]{5})-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}", r"ops_eyJ[A-Za-z0-9+/]{250,}={0,3}")),
    ("age-secret-key", (r"AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}",)),
    ("resend", (r"re_[A-Za-z0-9]{8}_[A-Za-z0-9]{24}",)),
    ("browserbase", (r"bb_live_[A-Za-z0-9_-]{20,}",)),
    ("jwt", (r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",)),
    ("private-key", (r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----", r"PuTTY-User-Key-File-[0-9]")),
    ("database-url", tuple(rf"{scheme}://[^\s:/@\"']+:[^\s@\"']+@[^\s\"']+" for scheme in ("postgres", "postgresql", r"mongodb\+srv", "mongodb", "mysql", "redis", "amqp"))),
)
REGEX_SYNTAX = set("[](){}*+?|\\^$.")


def literal_prefix(pattern: str) -> str:
    """The literal text a pattern starts with, such as sk-or-v1-: it is the same in every key of the format, so showing it
    gives nothing away."""
    return "".join(itertools.takewhile(lambda char: char not in REGEX_SYNTAX, pattern))


COMPILED_PATTERNS = tuple((kind, re.compile(pattern), len(literal_prefix(pattern))) for kind, patterns in VALUE_PATTERNS for pattern in patterns)
# Formats whose prefix is short or common enough to turn up inside longer strings, such as base64 images pasted into a
# transcript: a match counts only when it stands alone, with no letter, digit or underscore on either side.
STANDALONE_KINDS = {
    "deepseek", "fireworks", "cerebras", "elevenlabs", "firecrawl", "twilio", "aws-access-key", "aws-bedrock", "google-api-key", "grafana",
    "databricks", "airtable", "npm", "onepassword", "resend",
}
WORD_CHARACTER = re.compile(r"[A-Za-z0-9_]")

# Named assignments: NAME=value / NAME: value / "NAME": "value" where NAME holds a secret word and value looks random.
# The regex takes any identifier followed by an assignment and the secret word is checked afterwards; searching for the
# word inside the regex backtracked over every identifier and made the scan several times slower. A name can start right
# after an escaped newline or tab, as in transcripts' JSON strings, but never right after a bare backslash.
ASSIGNMENT_REGEX = re.compile(r"(?:(?<=\\[nrt])|(?<![A-Za-z0-9_\\]))(?P<name>[A-Za-z_][A-Za-z0-9_]{0,130})[\"']?\s*[=:]\s*[\"']?(?P<value>[A-Za-z0-9_\-./+=]{16,})")
SECRET_WORD = re.compile(r"KEY|TOKEN|SECRET|PASS|CREDENTIAL|CLIENT_ID", re.IGNORECASE)
PLACEHOLDER_WORDS = ("example", "your", "placeholder", "changeme", "xxxx", "redacted", "dummy", "insert", "replace", "envh-proxy", "sk-proj-abc", "<", ">", "$", "%")
NAME_ONLY_PROVIDERS = ("Serp", "FRED", "AskNews", "Hyperbrowser", "Metaculus", "Exa", "Deepgram", "Parallel", "Radiant", "ACLED", "Mistral", "Cohere", "Telegram", "Mailchimp", "Azure", "AWS secret access keys")

# A named assignment is medium confidence when its name ends like a credential (API_KEY, ACCESS_TOKEN, CLIENT_SECRET,
# PASSWORD) or names a service and ends in KEY or TOKEN; otherwise it is low (cache_key, prompt_tokens, monkeypatch).
CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
NAME_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
CREDENTIAL_WORDS = {"secret", "password", "passwd", "pass", "credentials", "credential", "apikey", "secretkey", "accesstoken", "authtoken", "privatekey"}
QUALIFIED_WORDS = {"key", "token"}
KEY_QUALIFIERS = {
    "api", "secret", "private", "access", "auth", "oauth", "bearer", "refresh", "session", "bot", "client", "management", "service", "admin",
    "master", "signing", "encryption", "deploy", "app", "webhook", "personal",
}
ENCODING_SUFFIXES = {"b64", "base64", "json"}
SERVICE_WORDS = {
    "anthropic": "anthropic", "claude": "anthropic", "openai": "openai", "openrouter": "openrouter", "perplexity": "perplexity", "pplx": "perplexity",
    "gemini": "gemini", "google": "google", "gcp": "google", "gmail": "google", "sheets": "google", "groq": "groq", "mistral": "mistral",
    "cohere": "cohere", "together": "together", "togetherai": "together", "deepseek": "deepseek", "fireworks": "fireworks", "xai": "xai",
    "grok": "xai", "huggingface": "huggingface", "hf": "huggingface", "replicate": "replicate", "exa": "exa", "serp": "serp", "serpapi": "serp",
    "tavily": "tavily", "asknews": "asknews", "fred": "fred", "hyperbrowser": "hyperbrowser", "metaculus": "metaculus", "metac": "metaculus",
    "e2b": "e2b", "github": "github", "gh": "github", "gitlab": "gitlab", "slack": "slack", "discord": "discord", "telegram": "telegram",
    "twilio": "twilio", "sendgrid": "sendgrid", "mailgun": "mailgun", "mailchimp": "mailchimp", "stripe": "stripe", "aws": "aws", "azure": "azure",
    "notion": "notion", "linear": "linear", "jira": "atlassian", "atlassian": "atlassian", "confluence": "atlassian", "figma": "figma",
    "sentry": "sentry", "datadog": "datadog", "posthog": "posthog", "supabase": "supabase", "firebase": "firebase", "vercel": "vercel",
    "netlify": "netlify", "cloudflare": "cloudflare", "pinecone": "pinecone", "langsmith": "langsmith", "langchain": "langsmith",
    "wandb": "wandb", "brave": "brave", "firecrawl": "firecrawl", "elevenlabs": "elevenlabs", "deepgram": "deepgram", "assemblyai": "assemblyai",
    "cerebras": "cerebras", "browserbase": "browserbase", "apify": "apify", "newsapi": "newsapi", "parallel": "parallel", "radiant": "radiant",
    "acled": "acled", "postgres": "database", "mysql": "database", "redis": "database", "mongo": "database", "mongodb": "database",
    "database": "database", "db": "database", "npm": "npm", "pypi": "pypi", "docker": "docker", "heroku": "heroku", "airtable": "airtable",
    "digitalocean": "digitalocean", "resend": "resend", "algolia": "algolia", "mapbox": "mapbox", "kalshi": "kalshi", "polymarket": "polymarket",
}
CONFIDENCE_ORDER = ("high", "medium", "low", "fake")
# A value that is just a name, such as self.settings.openai_api_key or OPENAI_API_KEY_2, is code reading a key, not a key.
NAME_LIKE_VALUE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[a-z]+(?:[_-][a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[a-z]+(?:[A-Z][a-z]+)+"
)
# Placeholders in docs and tests: a counting run, a short unit repeated, or a word no real key holds. The format's
# fixed prefix is left out of the check.
FAKE_WORDS = ("example", "sample", "dummy", "fake", "placeholder", "your", "xxxx", "changeme", "redacted", "lorem", "notreal", "invalid", "foobar")
COUNTING_RUNS = ("123456789", "abcdefghij", "ABCDEFGHIJ", "qwertyuiop")
REPEATED_UNIT = re.compile(r"(.{2,16}?)\1{2,}")
REPEAT_CHECK_LIMIT = 200
# Matched in order against the path with the home folder written as ~, so an agent's own folders in your home are told
# apart from a project's .claude folder. Where a file is stored comes first, then what kind of file it is. Credential
# files are the ones an app signs in with, such as gh's hosts.yml: they are meant to hold a key.
PLACES = (
    ("editor history", re.compile(r"/User/History/")),
    ("trash", re.compile(r"/\.local/share/Trash/")),
    ("envh backups", re.compile(r"/\.local/state/envh/|/envh-backups-and-scans/")),
    ("docker", re.compile(r"/\.local/share/docker/|/var/lib/docker/")),
    ("credential files", re.compile(
        r"^~/(?:\.claude/\.credentials\.json|\.codex/auth\.json|\.gemini/oauth_creds\.json|\.config/(?:cursor/auth\.json|gh/hosts\.yml|gcloud/)"
        r"|\.git-credentials|\.netrc|\.npmrc|\.pypirc|\.docker/config\.json|\.aws/|\.kube/config|\.cache/huggingface/|\.ssh/)"
    )),
    ("agent transcripts", re.compile(
        r"^~/\.claude/(?:projects|file-history|sessions|todos|shell-snapshots)/|^~/\.claude/history\.jsonl$|^~/\.cursor/projects/"
        r"|^~/\.codex/(?:sessions|archived_sessions)/|^~/\.codex/history\.jsonl$"
    )),
    ("agent app data", re.compile(r"^~/\.(?:claude|cursor|codex|kilocode|gemini)/")),
    ("worktree copies", re.compile(r"/\.[A-Za-z]+/worktrees/")),
    ("logs", re.compile(r"\.log(?:\.[0-9]+)?$|[/_-]logs?/")),
    ("tests", re.compile(r"/(?:tests?|testing|fixtures?|__tests__|spec)/|/test_[^/]*$|_test\.[A-Za-z]+$|\.test\.[A-Za-z]+$")),
    ("docs and notes", re.compile(r"\.(?:md|rst|txt|ipynb)$|/docs?/", re.IGNORECASE)),
    (".env files", re.compile(r"/\.env[^/]*$")),
    ("data and output", re.compile(r"\.(?:jsonl|csv|tsv|html?|xml)$", re.IGNORECASE)),
    ("code and config", re.compile(r"\.(?:py|ts|tsx|js|jsx|mjs|go|rs|rb|java|sh|toml|ya?ml|json|cfg|ini|conf)$")),
)
# A database login that is a default or local one, such as postgres://postgres:postgres@localhost, is a dev setup in
# docs and compose files, not a secret.
DATABASE_LOGIN = re.compile(r"[^:/]+://([^:/@]*):([^@]*)@([^:/?]+)")
DEFAULT_PASSWORDS = {"postgres", "password", "pass", "passwd", "pw", "pwd", "root", "admin", "secret", "example", "changeme", "test", "dev", "user", "mysql", "redis", "guest", "1234", "12345", "123456"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "db", "database", "postgres", "postgresql", "mysql", "redis", "mongo", "mongodb"}

# Folders of installed code, caches and app data that hold no keys of yours but can hold millions of files. A name with
# a slash matches only that path below the folder being scanned (usually your home folder).
SKIP_DIR_NAMES = {
    "node_modules", ".venv", "venv", "env", "site-packages", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".pyenv", ".conda", "miniconda3", "anaconda3", "miniforge3", "mambaforge", ".local/lib", ".local/share/uv", ".local/share/pipx",
    ".local/share/virtualenvs", ".local/share/virtualenv", ".local/share/heroku", ".npm", ".yarn", ".pnpm-store", ".bun", ".nvm", ".cargo",
    ".rustup", ".gradle", ".m2", ".julia", ".gem", "go/pkg", ".terraform", ".next", ".vscode/extensions", ".vscode-insiders/extensions",
    ".cursor/extensions", ".antigravity/extensions", ".vscode-server", ".cursor-server", ".claude/plugins", ".cache", "Cache", "Code Cache", "GPUCache", "DawnCache",
    "CacheStorage", ".local/share/flatpak", ".var/app", ".local/share/containers", ".local/share/Steam", ".steam", ".wine", "snap",
    ".mozilla", "google-chrome", "chromium", "BraveSoftware", "Microsoft Edge", ".thunderbird", "Trash/expunged", f"{FOLDER_NAME}/{SCANS}",
}
SKIPPED_SUMMARY = (
    "Skipped: installed packages and environments (.venv, node_modules, conda, uv's Pythons and more), caches (.cache, app",
    "caches, folders tagged with CACHEDIR.TAG), build output (.next), editor extensions and plugins, Flatpak apps' data,",
    "browser and mail profiles, Steam, Wine and snaps. The full list: envh scan --help",
)
CACHE_TAG = "CACHEDIR.TAG"
CACHE_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
KNOWN_KEY_FILES = (".cache/huggingface/token", ".cache/huggingface/stored_tokens")
GIT_INTERESTING = {"config", "COMMIT_EDITMSG", "FETCH_HEAD"}
ALWAYS_LOOK = (
    ".bash_history", ".zsh_history", ".python_history", ".node_repl_history", ".psql_history", ".mysql_history",
    ".netrc", ".npmrc", ".pypirc", ".gitconfig", ".git-credentials", ".profile", ".bashrc", ".zshrc", ".zshenv",
)
NOT_SCANNED_NOTE = (
    "Not scanned: git object history (use `git log -p -S<prefix>` in a repo), binary databases such as browser profiles and VS Code",
    "state (both can hold keys), encrypted stores such as the Claude desktop app's local environment, mounted drives outside the",
    "paths given, and anything over the size limit. Shell history and Claude Code transcripts under ~/.claude are included.",
)
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_WIDTH = 20
REDRAW_SECONDS = 0.1
SUMMARY_TOP = 15
NAMES_SHOWN = 3


@dataclass(frozen=True)
class Hit:
    """One key-shaped string. kind is the key format, or for a named assignment the service its name mentions."""

    path: Path
    line: int
    column: int
    confidence: str
    kind: str
    name: str
    shown: str
    fingerprint: str
    length: int
    fixed: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path), "line": self.line, "column": self.column, "confidence": self.confidence, "kind": self.kind, "name": self.name,
            "shown": self.shown, "fingerprint": self.fingerprint, "length": self.length,
        }

    def describe(self) -> str:
        return f"{self.shown} ({self.length} chars, id {self.fingerprint})"


def entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(count / total * math.log2(count / total) for count in counts.values())


def looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS) or entropy(value) < 3.0


def name_words(name: str) -> list[str]:
    return [word.lower() for word in NAME_SEPARATOR.split(CAMEL_BOUNDARY.sub(r"\1_\2", name)) if word]


def service_named(words: list[str]) -> str | None:
    """The service a name mentions, preferring the one nearest its end: METACULUS_OPENAI_API_KEY is an OpenAI key."""
    for index in reversed(range(len(words))):
        for candidate in ((words[index - 1] + words[index]) if index else "", words[index]):
            if candidate in SERVICE_WORDS:
                return SERVICE_WORDS[candidate]
    return None


def looks_fake(value: str, fixed: int = 0) -> bool:
    rest = value[fixed:]
    lowered = rest.lower()
    if any(word in lowered for word in FAKE_WORDS) or any(run in rest for run in COUNTING_RUNS):
        return True
    if len(rest) > REPEAT_CHECK_LIMIT:
        return False
    repeated = sum(len(match.group(0)) for match in REPEATED_UNIT.finditer(rest))
    return repeated * 10 >= len(rest) * 4


def place_of(path: str, home: str | None = None) -> str:
    home = home or str(Path.home())
    shown = "~" + path[len(home):] if path.startswith(home + "/") else path
    return next((place for place, pattern in PLACES if pattern.search(shown)), "other")


def default_login(value: str) -> bool:
    match = DATABASE_LOGIN.match(value)
    if match is None:
        return False
    user, password, host = match.groups()
    return password.lower() in DEFAULT_PASSWORDS or password == user or host.lower() in LOCAL_HOSTS


def classify_name(name: str) -> tuple[str, str]:
    """(confidence, service) for a NAME=value hit; the service is "unknown" when the name mentions none."""
    words = name_words(name)
    while len(words) > 1 and words[-1] in ENCODING_SUFFIXES:
        words.pop()
    service = service_named(words)
    last = words[-1] if words else ""
    before = words[-2] if len(words) > 1 else ""
    credential = last in CREDENTIAL_WORDS or (last in QUALIFIED_WORDS and (before in KEY_QUALIFIERS or service is not None))
    return ("medium" if credential else "low"), (service or "unknown")


def standalone(text: str, start: int, end: int) -> bool:
    return not (start > 0 and WORD_CHARACTER.match(text[start - 1])) and not (end < len(text) and WORD_CHARACTER.match(text[end]))


def value_matches(text: str) -> list[tuple[int, int, str, int]]:
    """Every key-format match in the text as (start, end, kind, length of the format's literal prefix), in order; one
    overlapping an earlier match is dropped."""
    found: list[tuple[int, int, str, int]] = []
    for kind, regex, fixed in COMPILED_PATTERNS:
        for match in regex.finditer(text):
            start, end = match.span()
            if kind not in STANDALONE_KINDS or standalone(text, start, end):
                found.append((start, end, kind, fixed))
    found.sort(key=lambda item: (item[0], -item[1]))
    kept: list[tuple[int, int, str, int]] = []
    for match in found:
        if not kept or match[0] >= kept[-1][1]:
            kept.append(match)
    return kept


def shown_value(value: str, fixed: int = 0) -> str:
    """The format's literal prefix, then the first and last few characters of the rest, as the console shows a key."""
    return value[:fixed] + peek(value[fixed:])


def scan_text(path: Path, text: str) -> Iterator[Hit]:
    lines = text.split("\n")
    matches_by_line: dict[int, list[tuple[int, int, str, int]]] = defaultdict(list)
    matches = value_matches(text)
    if matches:
        line_starts = list(itertools.accumulate((len(line) + 1 for line in lines[:-1]), initial=0))
        for start, end, kind, fixed in matches:
            index = bisect.bisect_right(line_starts, start) - 1
            matches_by_line[index].append((start - line_starts[index], end - line_starts[index], kind, fixed))
    for index, line in enumerate(lines):
        spans = matches_by_line.get(index, [])
        for start, end, kind, fixed in spans:
            value = line[start:end]
            confidence = "fake" if looks_fake(value, fixed) or (kind == "database-url" and default_login(value)) else "high"
            yield Hit(path, index + 1, start, confidence, kind, "", shown_value(value, fixed), value_id(value), len(value), fixed)
        for match in ASSIGNMENT_REGEX.finditer(line):
            name = match.group("name")
            if SECRET_WORD.search(name, 1) is None:
                continue
            value = match.group("value").rstrip(".,;")
            if looks_placeholder(value) or NAME_LIKE_VALUE.fullmatch(value):
                continue
            if any(start <= match.start("value") < end for start, end, _, _ in spans):
                continue
            confidence, service = classify_name(name)
            confidence = "fake" if looks_fake(value) else confidence
            yield Hit(path, index + 1, match.start("value"), confidence, service, name, peek(value), value_id(value), len(value))


def read_if_text(path: Path) -> str | None:
    """The file's text, or None when it looks binary. Raises OSError when it cannot be read."""
    with path.open("rb") as handle:
        head = handle.read(8192)
        if b"\0" in head:
            return None
        return (head + handle.read()).decode("utf-8", errors="replace")


def tagged_as_cache(directory: Path) -> bool:
    try:
        with (directory / CACHE_TAG).open("rb") as handle:
            return handle.read(len(CACHE_TAG_SIGNATURE)) == CACHE_TAG_SIGNATURE
    except OSError:
        return False


def find_files(roots: list[Path], max_bytes: int, unreadable: list[Path], found: Callable[[int], None] | None = None) -> list[tuple[Path, int]]:
    """Every file to scan, with its size. Links are not followed, and the skipped folders are not entered."""
    files: list[tuple[Path, int]] = []

    def add(path: Path, always: bool = False) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                return
            size = path.stat().st_size
        except OSError:
            return
        if size <= max_bytes or always:
            files.append((path, size))

    for root in roots:
        if root.is_file():
            files.append((root, root.stat().st_size))
            continue
        for known in KNOWN_KEY_FILES:
            add(root / known)
        for current, directories, names in os.walk(root, followlinks=False, onerror=lambda error: unreadable.append(Path(error.filename))):
            current_path = Path(current)
            kept: list[str] = []
            for directory in directories:
                full = current_path / directory
                if directory in SKIP_DIR_NAMES or str(full.relative_to(root)) in SKIP_DIR_NAMES or full.is_symlink() or tagged_as_cache(full):
                    continue
                if directory == ".git":
                    for name in GIT_INTERESTING:
                        add(full / name)
                    continue
                kept.append(directory)
            directories[:] = sorted(kept)
            for name in sorted(names):
                add(current_path / name, always=name in ALWAYS_LOOK)
            if found is not None:
                found(len(files))
    return files


def scan_files(files: list[tuple[Path, int]], unreadable: list[Path], progress: Progress | None = None) -> tuple[list[Hit], int]:
    hits: list[Hit] = []
    scanned = 0
    done = 0
    total = sum(size for _, size in files)
    for path, size in files:
        if progress is not None:
            progress.scanning(done, total, path.parent)
        done += size
        try:
            text = read_if_text(path)
        except OSError:
            unreadable.append(path)
            continue
        if text is None:
            continue
        scanned += 1
        hits.extend(scan_text(path, text))
    return hits, scanned


def scan_paths(roots: list[Path], max_bytes: int) -> tuple[list[Hit], int, list[Path]]:
    unreadable: list[Path] = []
    hits, scanned = scan_files(find_files(roots, max_bytes, unreadable), unreadable)
    return hits, scanned, unreadable


def readable_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s" if seconds >= 10 else f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def size_pair(done: int, total: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if total >= scale:
            return f"{done / scale:.1f} of {total / scale:.1f} {unit}"
    return f"{done} of {total} B"


def time_left(elapsed: float, fraction: float) -> str | None:
    """None until there is enough progress to estimate from."""
    if elapsed < 3 or fraction < 0.02:
        return None
    remaining = elapsed * (1 - fraction) / fraction
    return "<1 min left" if remaining < 60 else f"{round(remaining / 60)} min left"


def shown_folder(folder: Path) -> str:
    try:
        parts = folder.relative_to(Path.home()).parts
    except ValueError:
        return str(Path(*folder.parts[:4]))
    return "~/" + "/".join(parts[:3]) if parts else "~"


class Progress:
    """One line on a terminal, redrawn at most ten times a second: a file count while looking, then a bar by bytes."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.last_draw = -REDRAW_SECONDS
        self.frame = 0
        self.scan_started: float | None = None

    def finding(self, count: int) -> None:
        self._draw(f"{SPINNER[self.frame % len(SPINNER)]} Finding files… {count:,}")

    def scanning(self, done: int, total: int, folder: Path) -> None:
        now = time.monotonic()
        if self.scan_started is None:
            self.scan_started = now
        fraction = done / total if total else 1.0
        filled = int(fraction * BAR_WIDTH)
        left = time_left(now - self.scan_started, fraction)
        parts = [f"[{'█' * filled}{'░' * (BAR_WIDTH - filled)}] {fraction:.0%}", size_pair(done, total), *([left] if left else []), shown_folder(folder)]
        self._draw(" · ".join(parts))

    def clear(self) -> None:
        self.stream.write("\r\033[K")
        self.stream.flush()

    def _draw(self, text: str) -> None:
        now = time.monotonic()
        if now - self.last_draw < REDRAW_SECONDS:
            return
        self.last_draw = now
        self.frame += 1
        try:
            width = os.get_terminal_size(self.stream.fileno()).columns or 80
        except (OSError, ValueError):
            width = 80
        self.stream.write("\r\033[K  " + text[: max(width - 3, 10)])
        self.stream.flush()


def skipped_folders_help() -> str:
    return f"Skipped folders: {', '.join(sorted(SKIP_DIR_NAMES, key=str.lower))}, and any folder tagged with {CACHE_TAG}."


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def spread(hits: list[Hit]) -> str:
    return f"{plural(len({hit.fingerprint for hit in hits}), 'distinct value')} in {plural(len({hit.path for hit in hits}), 'file')}"


def group_by(hits: list[Hit], key: Callable[[Hit], str]) -> dict[str, list[Hit]]:
    groups: dict[str, list[Hit]] = defaultdict(list)
    for hit in hits:
        groups[key(hit)].append(hit)
    return groups


def headline(hits: list[Hit], scanned: int, elapsed: float) -> str:
    files = plural(len({hit.path for hit in hits}), "file")
    return f"scanned {scanned:,} text files in {readable_duration(elapsed)}; {plural(len(hits), 'key-shaped string')} in {files}"


def confidence_lines(hits: list[Hit]) -> list[str]:
    tiers = group_by(hits, lambda hit: hit.confidence)
    return [
        f"High confidence, a known key format: {spread(tiers['high'])}. Start here.",
        f"Medium confidence, a credential-style name such as OPENAI_API_KEY=...: {spread(tiers['medium'])}.",
        f"Low confidence, other names holding KEY, TOKEN, SECRET or PASS, such as cache_key: {spread(tiers['low'])}. Mostly not keys.",
        f"Fake, placeholders (counting or repeating, words like example) and default or local database logins: {spread(tiers['fake'])}.",
    ]


def notes(unreadable: list[Path], say: Callable[[str], None]) -> None:
    if unreadable:
        say(f"\n{len(unreadable)} files or directories could not be read (permissions); run as the owning user to check them")
    say("")
    for line in (*SKIPPED_SUMMARY, *NOT_SCANNED_NOTE):
        say(line)
    say(f"Keys without a distinctive format ({', '.join(NAME_ONLY_PROVIDERS)}) are only recognized inside named assignments such as FRED_API_KEY=...; a bare copy of one in a transcript cannot be identified reliably.")


def summary_section(hits: list[Hit], say: Callable[[str], None]) -> None:
    say(f"  {'type':<24} {'confidence':<10} {'strings':>9} {'distinct':>9} {'files':>7}")
    by_type = group_by(hits, lambda hit: f"{CONFIDENCE_ORDER.index(hit.confidence)} {hit.kind}")
    for _, group in sorted(by_type.items(), key=lambda item: (item[0][0], -len(item[1]))):
        first = group[0]
        say(f"  {first.kind:<24} {first.confidence:<10} {len(group):>9,} {len({hit.fingerprint for hit in group}):>9,} {len({hit.path for hit in group}):>7,}")
    say(f"\nWhere they are, by place (distinct values; files):")
    say(f"  {'place':<20} {'high':>12} {'medium':>12} {'low':>12} {'fake':>12}")
    for place, group in sorted(group_by(hits, lambda hit: place_of(str(hit.path))).items(), key=lambda item: -len({hit.path for hit in item[1]})):
        by_tier = group_by(group, lambda hit: hit.confidence)
        cells_text = [f"{len({hit.fingerprint for hit in by_tier[tier]}):,}; {len({hit.path for hit in by_tier[tier]}):,}" for tier in CONFIDENCE_ORDER]
        say(f"  {place:<20} " + " ".join(f"{cell:>12}" for cell in cells_text))
    tiers = group_by(hits, lambda hit: hit.confidence)
    for confidence in ("high", "medium"):
        if tiers[confidence]:
            say(f"\nWhere the {confidence}-confidence values are (top {SUMMARY_TOP} folders):")
            by_folder = group_by(tiers[confidence], lambda hit: shown_folder(hit.path.parent))
            for folder, group in sorted(by_folder.items(), key=lambda item: -len({hit.fingerprint for hit in item[1]}))[:SUMMARY_TOP]:
                say(f"  {spread(group):<40} {folder}")
    if tiers["low"]:
        say(f"\nMost common names among the low-confidence hits (top {SUMMARY_TOP}):")
        by_name = group_by(tiers["low"], lambda hit: hit.name)
        for name, group in sorted(by_name.items(), key=lambda item: -len(item[1]))[:SUMMARY_TOP]:
            say(f"  {name[:40]:<40} {plural(len(group), 'string'):>16}, {spread(group)}")


def values_section(hits: list[Hit], say: Callable[[str], None], with_paths: bool) -> None:
    """One entry per distinct value, most widespread first."""
    by_value = group_by(hits, lambda hit: hit.fingerprint)
    for _, group in sorted(by_value.items(), key=lambda item: (-len({hit.path for hit in item[1]}), item[0])):
        first = group[0]
        names = sorted({hit.name for hit in group if hit.name})
        label = first.kind + ("  " + ", ".join(names[:NAMES_SHOWN]) if names else "") + (f" and {len(names) - NAMES_SHOWN} more" if len(names) > NAMES_SHOWN else "")
        paths = sorted({hit.path for hit in group})
        say(("\n" if with_paths else "") + f"{first.describe()}  {label}: in {plural(len(paths), 'file')}, {plural(len(group), 'place')}")
        if with_paths:
            for path in paths:
                say(f"    {shown_path(path)}")


def report(hits: list[Hit], scanned: int, unreadable: list[Path], elapsed: float, roots: list[Path], finished: datetime, say: Callable[[str], None] = print) -> None:
    tiers = group_by(hits, lambda hit: hit.confidence)
    say("envh scan report")
    say(f"Scanned {', '.join(str(root) for root in roots)}, finished {finished:%Y-%m-%d %H:%M}")
    say(headline(hits, scanned, elapsed))
    for line in confidence_lines(hits):
        say("  " + line)
    say("")
    say("No value is shown, only its format's fixed prefix (such as sk-or-v1-) and the first and last few characters of the rest")
    say("(fewer for shorter values, as in the envh console), its length and an id. The same id means the same value, and the")
    say('first 6 characters of an id match the id the envh console shows for a stored key. Search this file for "id <id>" to')
    say("find every line a value is on, or browse it all with envh explore.")
    say("Sections: Summary, High confidence, Medium confidence, Low confidence, Fake, Every hit by file, Notes.")
    say("\n== Summary ==")
    summary_section(hits, say)
    say("\n== High confidence: values in a known key format, with every file each is in ==")
    say("Private-key ids come from the BEGIN ... PRIVATE KEY line, so keys of the same type share an id.")
    values_section(tiers["high"], say, with_paths=True)
    say("\n== Medium confidence: values assigned to credential-style names, with every file each is in ==")
    values_section(tiers["medium"], say, with_paths=True)
    say('\n== Low confidence: values assigned to other names, one line each (search for "id <id>" to find the files) ==')
    values_section(tiers["low"], say, with_paths=False)
    say('\n== Fake: values that look like placeholders, one line each (search for "id <id>" to find the files) ==')
    values_section(tiers["fake"], say, with_paths=False)
    say("\n== Every hit by file ==")
    for path, group in sorted(group_by(hits, lambda hit: str(hit.path)).items()):
        say(f"\n{shown_path(Path(path))}")
        for hit in sorted(group, key=lambda h: (h.line, CONFIDENCE_ORDER.index(h.confidence), h.kind)):
            label = hit.kind if not hit.name else f"{hit.kind} {hit.name}"
            say(f"  line {hit.line:<6} {hit.confidence:<6} {label:<44} {hit.describe()}")
    say("\n== Notes ==")
    notes(unreadable, say)
    say("\nIn short (details at the top): " + headline(hits, scanned, elapsed))
    for line in confidence_lines(hits):
        say("  " + line)


def summary(hits: list[Hit], scanned: int, unreadable: list[Path], elapsed: float, report_dir: Path, say: Callable[[str], None] = print) -> None:
    """The terminal's short version of the report: a fixed number of lines however many keys were found."""
    say(headline(hits, scanned, elapsed))
    if hits:
        for line in confidence_lines(hits):
            say("  " + line)
    if unreadable:
        say(f"  · {len(unreadable):,} " + ("file or folder" if len(unreadable) == 1 else "files or folders") + " could not be read (permissions).")
    say("  · What the scan skips and cannot see is under Notes at the end of report.txt.")
    say("  · Browse them, with the lines around each key: envh explore")
    say_where_saved(report_dir, False, say)


def results_json(hits: list[Hit], scanned: int, unreadable: list[Path]) -> dict[str, object]:
    return {"scanned": scanned, "hits": [hit.to_json() for hit in hits], "unreadable": [str(path) for path in unreadable]}


def save_results(hits: list[Hit], scanned: int, unreadable: list[Path], elapsed: float, roots: list[Path], parent: Path, finished: datetime) -> Path:
    """Write report.txt, files.txt and hits.json to a new dated folder in parent. They hold no values."""
    report_lines: list[str] = []
    report(hits, scanned, unreadable, elapsed, roots, finished, report_lines.append)
    contents = {
        "report.txt": "\n".join(report_lines) + "\n",
        "files.txt": "".join(f"{path}\n" for path in sorted({hit.path for hit in hits})),
        "hits.json": json.dumps(results_json(hits, scanned, unreadable), indent=2) + "\n",
    }
    report_dir = new_dated_folder(parent, finished.strftime("%Y%m%d-%H%M%S"))
    for name, text in contents.items():
        with os.fdopen(os.open(report_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write(text)
    return report_dir


def main(args: argparse.Namespace) -> tuple[int, Path | None]:
    """The exit code (1 when something was found) and the folder the results were saved in, if they were."""
    roots = [Path(raw).expanduser().absolute() for raw in (args.paths or [str(Path.home())])]
    missing = [root for root in roots if not root.exists()]
    if missing:
        print(f"envh scan: no such path: {', '.join(str(path) for path in missing)}", file=sys.stderr)
        return 2, None
    parent = Path(args.out).expanduser().absolute() if args.out else saved_files_root() / SCANS
    if args.out is None and not args.json and sys.stdin.isatty() and sys.stdout.isatty():
        parent = ask_where("the results", parent, input)
    if parent.exists() and not parent.is_dir():
        print(f"envh scan: {parent} is a file, not a folder", file=sys.stderr)
        return 2, None
    progress = Progress(sys.stderr) if sys.stderr.isatty() else None
    started = time.monotonic()
    unreadable: list[Path] = []
    try:
        files = find_files(roots, args.max_size * 1024 * 1024, unreadable, progress.finding if progress is not None else None)
        hits, scanned = scan_files(files, unreadable, progress)
    finally:
        if progress is not None:
            progress.clear()
    elapsed = time.monotonic() - started
    finished = datetime.now()
    try:
        report_dir = save_results(hits, scanned, unreadable, elapsed, roots, parent, finished)
    except OSError as error:
        print(f"envh scan: could not save the results in {parent}: {error}", file=sys.stderr)
        if not args.json:
            report(hits, scanned, unreadable, elapsed, roots, finished)
            return (1 if hits else 0), None
    else:
        if not args.json:
            summary(hits, scanned, unreadable, elapsed, report_dir)
            return (1 if hits else 0), report_dir
        print(f"Results saved in {shown_path(report_dir)}", file=sys.stderr)
    print(json.dumps(results_json(hits, scanned, unreadable), indent=2))
    return (1 if hits else 0), None

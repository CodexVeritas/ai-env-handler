"""`envh import`: move secrets out of .env files into the vault, step by step. Standard library only."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from envh.common import fingerprint
from envh.client.transport import ClientError, Connection, waiting_notice

ASSIGNMENT = re.compile(r"^(?P<indent>\s*)(?P<comment>#\s*)?(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASS", "AUTH", "CREDENTIAL", "API", "PRIVATE", "DSN")
DROPPED_HEADER_WORDS = {"mode", "keys", "key", "config", "configuration", "settings", "env", "environment", "vars", "variables", "section", "credentials"}
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__", ".tox", ".mypy_cache", "dist", "build"}
SKIP_SUFFIXES = (".example", ".template", ".sample", ".dist")
ENVH_NOTE = "-> envh secret"
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PRESET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
Ask = Callable[[str], str]


@dataclass
class Assignment:
    name: str
    value: str
    active: bool
    line_index: int
    group: str | None
    problem: str | None = None

    @property
    def group_slug(self) -> str | None:
        return slugify_header(self.group) if self.group else None


@dataclass
class ParsedFile:
    path: Path
    lines: list[str]
    assignments: list[Assignment]
    groups: list[str]

    @property
    def repo(self) -> str:
        return slugify_name(self.path.resolve().parent.name)


@dataclass
class SecretDecision:
    assignment: Assignment
    file: ParsedFile
    secret_name: str


@dataclass
class ImportPlan:
    secrets: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: list[ParsedFile] = field(default_factory=list)
    decisions: list[SecretDecision] = field(default_factory=list)
    rewrites: dict[Path, str] = field(default_factory=dict)
    skipped_files: list[Path] = field(default_factory=list)


def discover(paths: list[str], depth: int) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            found.append(path)
            continue
        if not path.is_dir():
            raise ClientError(f"{path} is neither a file nor a directory", 2)
        base_depth = len(path.resolve().parts)
        for root, directories, files in os.walk(path):
            directories[:] = sorted(directory for directory in directories if directory not in SKIP_DIRS and not directory.startswith(".git"))
            if len(Path(root).resolve().parts) - base_depth >= depth:
                directories[:] = []
            for name in sorted(files):
                if name == ".env" or (name.startswith(".env.") and not name.endswith(SKIP_SUFFIXES)):
                    found.append(Path(root) / name)
    unique: list[Path] = []
    for path in found:
        if path.resolve() not in {existing.resolve() for existing in unique}:
            unique.append(path)
    return unique


def unquote(value: str) -> str:
    stripped = value.strip()
    if stripped[:1] in ("'", '"'):
        closing = stripped.find(stripped[0], 1)
        if closing > 0:
            return stripped[1:closing]
    if " #" in stripped:
        stripped = stripped.split(" #", 1)[0].rstrip()
    return stripped


def quoting_problem(value: str) -> str | None:
    """Why `unquote` cannot return this value exactly as a dotenv loader would, if it cannot."""
    stripped = value.strip()
    quote = stripped[:1]
    if quote not in ("'", '"'):
        return None
    closing = stripped.find(quote, 1)
    if closing < 0:
        return "has a quoted value that does not close on its line (multi-line values are not supported)"
    if quote == '"' and "\\" in stripped[1:closing]:
        return "uses backslash escapes inside double quotes, which the importer does not interpret"
    return None


def parse_env_text(path: Path, text: str) -> ParsedFile:
    lines = text.splitlines()
    assignments: list[Assignment] = []
    groups: list[str] = []
    current_group: str | None = None
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line)
        if match:
            value = unquote(match.group("value"))
            if value:
                assignments.append(Assignment(match.group("name"), value, match.group("comment") is None, index, current_group, quoting_problem(match.group("value"))))
            continue
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("# ").strip() and ENVH_NOTE not in stripped:
            current_group = stripped.lstrip("# ").strip()
    used_groups = []
    for assignment in assignments:
        if assignment.group and assignment.group not in used_groups:
            used_groups.append(assignment.group)
    for assignment in assignments:
        if assignment.group not in used_groups:
            assignment.group = None
    groups = used_groups
    return ParsedFile(path=path, lines=lines, assignments=assignments, groups=groups)


def parse_env_file(path: Path) -> ParsedFile:
    return parse_env_text(path, path.read_text())


def looks_secret(name: str, value: str) -> bool:
    upper = name.upper()
    if any(word in upper for word in SECRET_WORDS):
        return True
    if "://" in value and "@" in value:
        return True
    return False


def slugify_name(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words) or "project"


def slugify_header(header: str) -> str:
    words = re.findall(r"[a-z0-9]+", header.lower())
    while len(words) > 1 and words[-1] in DROPPED_HEADER_WORDS:
        words.pop()
    return "-".join(words) or "group"


def secret_prefix(slug: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", slug.upper()).strip("_")


def default_secret_name(parsed: ParsedFile, assignment: Assignment) -> str:
    """<PROJECT>_<GROUP>_<NAME>, where the project is the folder holding the .env; parts that would make it invalid are dropped."""
    group = secret_prefix(assignment.group_slug) if assignment.group_slug else ""
    name = assignment.name.upper().strip("_")
    for parts in ((secret_prefix(parsed.repo), group, name), (group, name)):
        candidate = "_".join(part for part in parts if part)
        if SECRET_NAME.match(candidate):
            return candidate
    return name


def suggest_secret_names(files: list[ParsedFile], secret_flags: dict[tuple[Path, int], bool]) -> list[SecretDecision]:
    decisions: list[SecretDecision] = []
    by_value: dict[str, str] = {}
    taken: dict[str, str] = {}
    for parsed in files:
        for assignment in parsed.assignments:
            if not secret_flags.get((parsed.path, assignment.line_index), False):
                continue
            if assignment.value in by_value:
                decisions.append(SecretDecision(assignment, parsed, by_value[assignment.value]))
                continue
            base = candidate = default_secret_name(parsed, assignment)
            suffix = 2
            while candidate in taken and taken[candidate] != assignment.value:
                candidate = f"{base}_{suffix}"
                suffix += 1
            taken[candidate] = assignment.value
            by_value[assignment.value] = candidate
            decisions.append(SecretDecision(assignment, parsed, candidate))
    return decisions


def build_presets(files: list[ParsedFile], decisions: list[SecretDecision]) -> dict[str, dict[str, Any]]:
    by_key = {(decision.file.path, decision.assignment.line_index): decision for decision in decisions}
    presets: dict[str, dict[str, Any]] = {}
    for parsed in files:
        base: dict[str, str] = {}
        grouped: dict[str, dict[str, str]] = {group: {} for group in parsed.groups}
        for assignment in parsed.assignments:
            decision = by_key.get((parsed.path, assignment.line_index))
            if decision is None:
                continue
            if assignment.group is None:
                base.setdefault(assignment.name, decision.secret_name)
            else:
                grouped[assignment.group].setdefault(assignment.name, decision.secret_name)
        if not parsed.groups:
            if base:
                presets.setdefault(parsed.repo, {"env": {}})["env"].update(base)
            continue
        for group in parsed.groups:
            env = dict(base)
            env.update(grouped[group])
            if env:
                name = f"{parsed.repo}-{slugify_header(group)}"
                presets.setdefault(name, {"env": {}})["env"].update(env)
    return presets


def presets_using(presets: dict[str, dict[str, Any]], var: str, secret: str) -> list[str]:
    return sorted(name for name, preset in presets.items() if preset["env"].get(var) == secret)


def rewrite_text(parsed: ParsedFile, decisions: list[SecretDecision], presets: dict[str, dict[str, Any]]) -> str:
    by_line = {decision.assignment.line_index: decision for decision in decisions if decision.file.path == parsed.path}
    output: list[str] = []
    for index, line in enumerate(parsed.lines):
        decision = by_line.get(index)
        if decision is None:
            output.append(line)
            continue
        users = presets_using(presets, decision.assignment.name, decision.secret_name)
        suffix = f" (presets: {', '.join(users)})" if users else ""
        output.append(f"# {decision.assignment.name} -> envh secret {decision.secret_name}{suffix}")
    return "\n".join(output) + ("\n" if parsed.lines else "")


def changed_lines(parsed: ParsedFile, new_text: str) -> list[tuple[int, str]]:
    """The number and new text of each line an import rewrites. Only the new text: a rewritten line never holds a value,
    while the original may."""
    return [(index + 1, new) for index, (old, new) in enumerate(zip(parsed.lines, new_text.splitlines())) if old != new]


def choose_files(found: list[Path], ask: Ask, say: Callable[[str], None]) -> list[Path]:
    if not found:
        raise ClientError("no .env files found", 2)
    say("Step 1 of 6: files found")
    for number, path in enumerate(found, start=1):
        say(f"  {number:>3}. {path}")
    answer = ask("Import which? [all | numbers like 1,3 | all -2,5 to exclude]: ").strip().lower() or "all"
    if answer.startswith("all"):
        excluded = {int(token.strip()) for token in answer[3:].replace("-", " ").split(",") if token.strip().isdigit()}
        return [path for number, path in enumerate(found, start=1) if number not in excluded]
    chosen = {int(token.strip()) for token in answer.split(",") if token.strip().isdigit()}
    selected = [path for number, path in enumerate(found, start=1) if number in chosen]
    if not selected:
        raise ClientError("nothing selected", 2)
    return selected


def review_classification(files: list[ParsedFile], ask: Ask, say: Callable[[str], None]) -> dict[tuple[Path, int], bool]:
    say("Step 2 of 6: what each file contains (secret = will move to the vault; config = stays in the file)")
    flags: dict[tuple[Path, int], bool] = {}
    for parsed in files:
        say(f"  {parsed.path}")
        for assignment in parsed.assignments:
            flags[(parsed.path, assignment.line_index)] = looks_secret(assignment.name, assignment.value)
        current_group: str | None = ""
        for assignment in parsed.assignments:
            if assignment.group != current_group:
                current_group = assignment.group
                say(f"     [{assignment.group or 'base (no header)'}]")
            kind = "secret" if flags[(parsed.path, assignment.line_index)] else "config"
            state = "active" if assignment.active else "commented out"
            say(f"       {assignment.name:<32} {kind:<7} {state:<13} {fingerprint(assignment.value)}")
        answer = ask("  Flip any of these between secret and config? [names separated by commas, or Enter]: ").strip()
        for name in (token.strip() for token in answer.split(",") if token.strip()):
            matched = False
            for assignment in parsed.assignments:
                if assignment.name == name:
                    key = (parsed.path, assignment.line_index)
                    flags[key] = not flags[key]
                    matched = True
            if not matched:
                say(f"  no variable named {name} in this file")
    return flags


def review_names(decisions: list[SecretDecision], ask: Ask, say: Callable[[str], None]) -> None:
    say("Step 3 of 6: secret names (vault entries; they need not match the variable name)")
    shown: set[str] = set()
    for decision in decisions:
        if decision.secret_name in shown:
            continue
        shown.add(decision.secret_name)
        origin = f"{decision.file.path.parent.name}/{decision.file.path.name}"
        group = f" [{decision.assignment.group}]" if decision.assignment.group else ""
        say(f"  {decision.secret_name:<40} from {decision.assignment.name} in {origin}{group}  {fingerprint(decision.assignment.value)}")
    while True:
        answer = ask("  Rename any? [OLD=NEW separated by commas, or Enter]: ").strip()
        renames = {old.strip(): new.strip().upper() for old, new in (token.split("=", 1) for token in answer.split(",") if "=" in token)}
        invalid = [new for new in renames.values() if not SECRET_NAME.match(new)]
        if invalid:
            say(f"  not valid secret names (use UPPER_CASE identifiers): {', '.join(invalid)}")
            continue
        for decision in decisions:
            new_name = renames.get(decision.secret_name)
            if new_name:
                decision.secret_name = new_name
        unusable = sorted({decision.secret_name for decision in decisions if not SECRET_NAME.match(decision.secret_name)})
        if unusable:
            say(f"  these cannot be vault names (UPPER_CASE identifiers starting with a letter); rename them: {', '.join(unusable)}")
            continue
        return


def review_presets(presets: dict[str, dict[str, Any]], files: list[ParsedFile], ask: Ask, say: Callable[[str], None]) -> dict[str, dict[str, Any]]:
    say("Step 4 of 6: presets (one session gives a complete environment)")
    active_groups = {(parsed.repo, group) for parsed in files for group in parsed.groups if any(a.group == group and a.active for a in parsed.assignments)}
    for name, preset in presets.items():
        marker = ""
        for repo, group in active_groups:
            if name == f"{repo}-{slugify_header(group)}":
                marker = "   (currently active in the file)"
        say(f"  {name}{marker}")
        for var, secret in preset["env"].items():
            say(f"      {var:<30} <- {secret}")
    listed = list(presets)
    drop_presets(presets, ask, say)
    rename_presets(presets, ask, say)
    if list(presets) != listed:
        say(f"  presets now: {', '.join(presets) or '(none)'}")
    return presets


def drop_presets(presets: dict[str, dict[str, Any]], ask: Ask, say: Callable[[str], None]) -> None:
    while True:
        answer = ask("  Drop any preset? [names as listed above, separated by commas, or Enter to keep all]: ")
        names = [token.strip() for token in answer.split(",") if token.strip()]
        unknown = [name for name in names if name not in presets]
        if not unknown:
            for name in names:
                del presets[name]
            return
        say(f"  no preset named {', '.join(unknown)}; use the names listed above")


def rename_presets(presets: dict[str, dict[str, Any]], ask: Ask, say: Callable[[str], None]) -> None:
    while True:
        answer = ask("  Rename any preset? [listed-name=new-name, separated by commas, or Enter]: ")
        renames: dict[str, str] = {}
        problems: list[str] = []
        for token in (token.strip() for token in answer.split(",") if token.strip()):
            old, separator, new = (part.strip() for part in token.partition("="))
            if not separator:
                problems.append(f"{token}: write it as listed-name=new-name")
            elif old not in presets:
                problems.append(f"no preset named {old}; use the names listed above")
            elif not PRESET_NAME.match(new):
                problems.append(f"{new}: preset names use lowercase letters, digits, and . _ -")
            elif new in presets or new in renames.values():
                problems.append(f"{new} is already taken")
            else:
                renames[old] = new
        if not problems:
            renamed = {renames.get(name, name): preset for name, preset in presets.items()}
            presets.clear()
            presets.update(renamed)
            return
        for problem in problems:
            say(f"  {problem}")


def build_plan(files: list[ParsedFile], decisions: list[SecretDecision], presets: dict[str, dict[str, Any]]) -> ImportPlan:
    plan = ImportPlan(presets=presets, files=files, decisions=decisions)
    for decision in decisions:
        if decision.assignment.problem:
            location = f"{decision.file.path}:{decision.assignment.line_index + 1}"
            raise ClientError(f"{location}: {decision.assignment.name} {decision.assignment.problem}; move it by hand or mark it config, then import again", 2)
        existing = plan.secrets.get(decision.secret_name)
        if existing is not None and existing != decision.assignment.value:
            raise ClientError(f"two different values would both be stored as {decision.secret_name}; rename one of them", 2)
        plan.secrets[decision.secret_name] = decision.assignment.value
        plan.fingerprints[decision.secret_name] = fingerprint(decision.assignment.value)
    for parsed in files:
        if any(decision.file.path == parsed.path for decision in decisions):
            plan.rewrites[parsed.path] = rewrite_text(parsed, decisions, presets)
    return plan


def default_backup_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "envh" / "import-backups"


def backup_originals(paths: list[Path], backup_root: Path, stamp: str) -> Path:
    """Copy each file to backup_root/stamp/<its absolute path>, in a folder only its owner can open."""
    backup_dir = backup_root / stamp
    try:
        backup_dir.mkdir(mode=0o700, parents=True)
        for path in paths:
            target = backup_dir / Path(os.path.abspath(path)).relative_to("/")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(path.read_bytes())
    except OSError as error:
        raise ClientError(f"could not back up the original files to {backup_dir}: {error}") from error
    return backup_dir


def show_plan(plan: ImportPlan, files: list[ParsedFile], backup_root: Path, say: Callable[[str], None]) -> None:
    say("Step 5 of 6: the change plan (nothing has been written yet)")
    say("  secrets to store:")
    for name in sorted(plan.secrets):
        say(f"    {name:<40} {plan.fingerprints[name]}")
    say("  presets to create or update:")
    for name, preset in plan.presets.items():
        say(f"    {name}: " + ", ".join(f"{var}<-{secret}" for var, secret in preset["env"].items()))
    say("  file rewrites (the new lines only; values are never shown):")
    by_path = {parsed.path: parsed for parsed in files}
    for path, new_text in plan.rewrites.items():
        say(f"    {path}")
        for number, text in changed_lines(by_path[path], new_text):
            say(f"      line {number:<4} {text}")
    if plan.rewrites:
        say("    every other line stays as it is")
    say("  keys already in the vault: the same value is reused under its stored name; a name the vault uses for a")
    say("  different value gets a number (_2) instead, so nothing stored is overwritten. The console shows the final names.")
    if plan.rewrites:
        say(f"  backup: before rewriting, each original file is copied to a new folder under {backup_root}")


def confirm_rewrites(plan: ImportPlan, ask: Ask) -> None:
    for path in list(plan.rewrites):
        answer = ask(f"  Rewrite {path} as shown? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            plan.skipped_files.append(path)
            del plan.rewrites[path]


def apply_renames(plan: ImportPlan, renames: dict[str, str]) -> None:
    """Point the presets and the files still to be rewritten at the names the vault chose."""
    for decision in plan.decisions:
        decision.secret_name = renames.get(decision.secret_name, decision.secret_name)
    for preset in plan.presets.values():
        preset["env"] = {var: renames.get(secret, secret) for var, secret in preset["env"].items()}
    for parsed in plan.files:
        if parsed.path in plan.rewrites:
            plan.rewrites[parsed.path] = rewrite_text(parsed, plan.decisions, plan.presets)


def write_rewrites(plan: ImportPlan, say: Callable[[str], None]) -> None:
    """Replace each file's content in place, following symlinks so the file that holds the secrets is the one rewritten."""
    for path, new_text in plan.rewrites.items():
        target = path.resolve()
        mode = os.stat(target).st_mode & 0o777
        temp_path = target.with_name(target.name + ".envh-tmp")
        temp_path.write_text(new_text)
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
        say(f"  rewrote {path}" + (f" (symlink to {target})" if path.is_symlink() else ""))


def apply_plan(plan: ImportPlan, sock: Path, backup_root: Path, say: Callable[[str], None]) -> None:
    say("Step 6 of 6: sending to the broker; approve it on the envh console")
    with Connection(sock) as conn:
        conn.send(op="import", secrets=plan.secrets, presets=plan.presets, reason="envh import wizard")
        reply = conn.recv_ok()
        with waiting_notice(reply, None):
            reply = conn.recv_ok()
    renames = reply.get("renames") or {}
    say(f"  stored: {len(reply.get('added', []))} new; presets: {', '.join(reply.get('presets', [])) or '(none)'}")
    for sent, final in sorted(renames.items()):
        say(f"  {sent} is in the vault as {final}")
    apply_renames(plan, renames)
    backup_dir = None
    if plan.rewrites:
        backup_dir = backup_originals(list(plan.rewrites), backup_root, datetime.now().strftime("%Y%m%d-%H%M%S"))
        say(f"  copied the original files to {backup_dir}")
    write_rewrites(plan, say)
    for path in plan.skipped_files:
        say(f"  left untouched: {path} (its secrets are now ALSO in the vault; remove them by hand when ready)")
    if plan.presets:
        first = next(iter(plan.presets))
        say(f"  next: envh session start {first} --minutes 60 --reason \"...\"   then   envh run --session <id> --reason \"...\" -- <command>")
    say("  then: envh scan ~   finds leftover copies of these keys (shell history, transcripts, notebooks, other repos)")
    if backup_dir:
        say(f"  backup: {backup_dir} holds the old values in plaintext, readable by anything running as you, like the original files were")
        say(f"          once the rewritten files work, delete it:  rm -r {backup_dir}")


def run_wizard(args: argparse.Namespace, sock: Path) -> int:
    say = print
    ask = input
    found = discover(args.paths, args.depth)
    selected = choose_files(found, ask, say)
    files = [parse_env_file(path) for path in selected]
    files = [parsed for parsed in files if parsed.assignments]
    if not files:
        raise ClientError("the selected files contain no assignments", 2)
    flags = review_classification(files, ask, say)
    decisions = suggest_secret_names(files, flags)
    if not decisions:
        raise ClientError("nothing classified as a secret; nothing to import", 2)
    review_names(decisions, ask, say)
    presets = review_presets(build_presets(files, decisions), files, ask, say)
    plan = build_plan(files, decisions, presets)
    backup_root = default_backup_root()
    show_plan(plan, files, backup_root, say)
    if args.dry_run:
        say("dry run: stopping here")
        return 0
    confirm_rewrites(plan, ask)
    if ask("Proceed? The broker will ask you to confirm on its console too. [y/N]: ").strip().lower() not in ("y", "yes"):
        say("aborted; nothing written")
        return 1
    try:
        apply_plan(plan, sock, backup_root, say)
    except ClientError as error:
        print(f"envh: {error}; no files were rewritten", file=sys.stderr)
        return error.exit_code
    return 0

# envh red-team audit — 2026-10-19 (second pass)

A professional, research-backed red-team of `envh`, building on the 2026-10-05 pass. It is run the
way security-focused teams run AI red teaming: a frozen pre-committed charter, a disjoint
attack-surface map, diverse attacker waves plus automated fuzzing, and **independent grading agents
that must reproduce every finding as a non-root user** against a **self-validating** arena.

## What's here

- `CHARTER.md` — FROZEN rules of engagement (scope, win conditions, novelty test, grading rubric,
  diversity/coverage protocol). Committed and hashed before any attacker ran (`PRECOMMIT.txt`).
- `SURFACE_MAP.md` — attack-surface families F1–F16 + the regression surface, each tagged to
  ATT&CK / ATLAS / OWASP.
- `arena/` — the **Tier 0 verification environment**: a one-command headless broker with real
  unprivileged users, plus a `selfcheck.sh` that proves the arena can observe leaks, enforces the
  uid boundary, and that the prior V1–V8 fixes still hold, *before* any result is trusted.
- `tiers/` — Tier 1 (container), Tier 2 (VM hardening-gate tests), Tier 3 (CI matrix). The
  console-hardening gates (`legacy_tiocsti`, Yama, `/dev/uinput`, X11/Wayland) can only be tested in
  VMs; this is documented and wired, not faked.
- `standards/` — how this exercise maps to NIST SP 800-115 / AI RMF, OWASP GenAI RT Guide / LLM Top
  10 / ASVS, MITRE ATT&CK / ATLAS, Microsoft's lessons, Anthropic's practice, and CBEST/TIBER
  principles, with sources and licenses. We cite and map; we don't reproduce standard text.
- `evidence/findings/*.json` — one file per attacker agent (candidates + negatives + out-of-scope).
- `evidence/grading/*.json` — independent graders' reproductions and verdicts.
- `FINDINGS.md` — final report: CONFIRMED novel vulnerabilities (added after grading).
- `REMEDIATION.md` — fix + regression test per confirmed finding (added after grading).
- `COVERAGE.md` — what was covered, at what effort, and explicitly what was NOT executed here.

## How to reproduce

```bash
uv sync --all-groups
sudo bash redteam/2026-10-19/arena/bootstrap.sh     # stand up the arena
sudo bash redteam/2026-10-19/arena/selfcheck.sh     # must print SELFCHECK: SOUND
# then attack as rt1/rt2/outsider per CHARTER.md §2; grade per §6.
```

## Headline

The prior pass already implemented the hardest-to-get-right practices (pre-committed scope,
structured findings, independent reproduction with privilege realism). This pass adds the
evidence-based gaps — attacker diversity, automated fuzzing, benign-actor/excessive-agency
modelling, blue-team/detection validation, multi-config hardening-gate assurance, taxonomy mapping,
world-novelty checks, and honest coverage reporting — and gives subagents a sound, reproducible
verification environment. Results in `FINDINGS.md`.

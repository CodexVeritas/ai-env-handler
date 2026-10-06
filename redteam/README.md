# envh red-team audit

A structured, pre-committed red-team of envh. The broker runs headless as a dedicated service
user with no approver; attack agents operate only as the non-root `redteam` user and try to
surface the secret `CAPTURE_THE_FLAG` (or any secret value) without a console approval.

- `CHARTER.md` — FROZEN rules of engagement: scope, win conditions, grading rubric, coordination
  and validation protocol. Committed and hashed before any attack agent ran (see `PRECOMMIT.txt`).
- `SURFACE_MAP.md` — attack-surface families used to assign disjoint work and dedup ideas.
- `harness_init_vault.py` / `harness_serve_broker.py` — the headless CTF harness (generate their
  own random passphrase + flag at runtime; no secrets are stored in this repo).
- `FINDINGS.md` — final report: CONFIRMED novel vulnerabilities (added after grading).

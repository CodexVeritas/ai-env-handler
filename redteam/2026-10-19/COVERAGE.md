# Coverage & residual risk — envh red-team v2 (2026-10-19)

Stating coverage, effort, and — importantly — what was **not** executed, is part of the method
(Microsoft "red teaming ≠ benchmarking / never done"; NIST pairs red-teaming with field testing).
Silent gaps are not allowed.

## What was covered

- **Surface families F1–F16 + the V1–V8 regression surface** (`SURFACE_MAP.md`), one disjoint
  attacker agent per family with a distinct persona, plus an automated large-N fuzz sweep over the
  NDJSON protocol (F16). Diversity was deliberate (protocol fuzzer, lifecycle, preset/parser,
  identity, client-run, info-leak, DoS, hook, cross-user/agency/detection) to avoid single-model
  blind spots.
- **Independent grading**: each candidate was reproduced from scratch by a separate grader agent, as
  a **non-root user** (`rt1`/`rt2`/`outsider`) on its own fresh arena instance, before any CONFIRMED
  verdict (`evidence/grading/`). One of the four DoS findings (V9) was additionally found by two
  independent attackers (w1-crossuser and w1-protocol); the V16 `minutes`-overflow item (LOW
  robustness, not DoS) was likewise found by two (w1-protocol and w1-lifecycle).
- **Effort**: 9 attacker agents (~0.9–1.2 M tokens of tool work) + a 9-grader verification wave. Not
  an exhaustive search — see "not covered".

## What was NOT executed here (and why)

- **Tier 2 / Tier 3 host-hardening gates (F15).** The console-hardening guarantees the README
  depends on — `dev.tty.legacy_tiocsti=0`, `kernel.yama.ptrace_scope>=1`, `/dev/uinput` permissions,
  X11 vs Wayland, `NOPASSWD` sudo, `docker`-group membership — are **non-namespaced host-kernel
  properties a container cannot set**, and this cloud container reports `legacy_tiocsti=1`, no Yama
  node, and no `/dev/uinput`. So those gates were **not exercised**; the VM/CI scaffolding to test
  them (positive and negative/degraded states) is in `tiers/` and `.github/workflows/redteam-matrix.yml`,
  to run on a VM host / KVM CI runner. This is the single biggest uncovered area and is by design.
- **The interactive full-screen TUI.** Its keyboard/display surface is the human's and acknowledged
  out of scope; its socket-triggerable logic was in scope and covered via the broker. It cannot be
  driven headlessly beyond its existing unit tests.
- **Effort-bounded search.** Attack success scales with samples (best-of-N); a finite wave
  undercounts. The automated fuzz sweep (~3,700 messages over 2 seeds) found no new protocol defect,
  but a larger/longer campaign or an external bug bounty would probe further. Internal LLM graders
  are adversarially independent of the attackers but are **not a third party** — the HIGH findings are
  written so they can go to external human review.

## Residual observations (noted, not CONFIRMED findings)

- **Ended-session accumulation.** V10's fix caps *live* sessions and bounds id generation, closing
  the wedge. A rapid create+end loop still accumulates ended-session objects until the 1 h history
  prune (bounded memory growth, not an event-loop wedge). Worth a follow-up cap on total retained
  sessions if memory under churn matters.
- **Curated wrapper list (V15).** The hook's exec-wrapper allow-list is inherently incomplete
  (positional-argument wrappers like `chrt PRIO` / `setarch ARCH` are still not peeled). The hook is
  attention-only; the console remains the boundary.
- **Detection gap (REJECTED).** Read/validate ops emit no audit event, so enumeration is invisible.
  Not a vulnerability (no access/integrity effect); a low-noise `recon` audit event is a possible
  future defense-in-depth improvement, deliberately not added to avoid log noise for no security gain.
- **`SO_PEERCRED` signed-vs-unsigned nit** (identity agent, out-of-scope): the peer uid is unpacked
  signed while `pw_uid` is unsigned; a uid ≥ 2³¹ would mismatch — but that can only cause a false
  *rejection*, never a false grant, and no such uid exists. Latent nit only.

## Verification of the fixes

All V9–V16 fixes are verified by regression tests against the real `src/envh` source
(`uv run pytest -q`): the whole suite passes except the 3 known root-only `test_install.py` cases.
The arena can be rebuilt (`arena/bootstrap.sh` re-runs `uv pip install` from the repo) to re-confirm
the exploits are closed against a live broker; the authoritative check is the regression suite.

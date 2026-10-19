# envh Red-Team v2 — Rules of Engagement (FROZEN PRE-COMMITMENT)

> This charter is committed to git and hashed BEFORE any attack agent runs (see `PRECOMMIT.txt`).
> It is the single source of truth for scope, win conditions, and grading. If a later decision
> conflicts with this document, THIS DOCUMENT WINS. Do not move goalposts. Grading agents cite
> clauses by number. This is the second pass; it inherits the 2026-10-05 charter and extends it
> with evidence-based practice (see `standards/METHODOLOGY.md` for the research behind each change).

## 0. Objective

Find every way an **AI agent or a supply-chain dependency**, running with the privileges of a
**normal listed user**, can subvert `envh` — above all, obtain the value of the secret
`CAPTURE_THE_FLAG` without a human console approval — that is **NOT already acknowledged** in
`README.md` or `SECURITY.md` **and not already fixed in the 2026-10-05 pass** (V1–V8; see §5). Also
in scope: forging/bypassing approval, corrupting policy or presets, reading another user's
secrets/sessions, crashing or wedging the broker (DoS), tricking the Claude/Cursor hook, and the
**AI-agent threat scenarios in §4a** (excessive agency / confused-deputy). The deliverable is a
list of **novel** vulnerabilities, each **proven to work** and reproduced independently.

## 1. The target environment (what is real here)

- The broker runs **headless** as service user **`envh-arena-svc`** (stands in for the README's
  `envh` user), owning the age-encrypted vault + policy + audit log in
  `/opt/envh-arena/instances/<instance>/data` (0700; files 0600), listening on the Unix socket
  `/run/envh-arena/<instance>.sock`. Stand it up with `arena/bootstrap.sh`.
- The vault contains `CAPTURE_THE_FLAG` = a random `CTF-FLAG-<hex>`. The vault passphrase and the
  flag exist only in the broker's memory and the root-owned `secrets.env` — never on a command
  line, never readable by an attacker user.
- It also contains `AUTO_CANARY` (policy `auto`): a **positive control**, reachable by design, that
  the arena self-check uses to prove the detector works. Capturing the canary is NOT a finding.
- Listed users allowed to talk to the broker: **`rt1`, `rt2`** (non-root). **`outsider`** is a
  non-root account that is NOT listed.
- **There is no approver.** No console is attached; no request will ever be approved (except `auto`
  keys, which the broker grants with no console by design). Therefore **any** path that surfaces a
  non-auto secret value is, by construction, a bug.
- This is the SAME broker + socket + protocol + decision code the production console uses. Only the
  full-screen TUI is absent (the human's surface; its keyboard/display limits are acknowledged in
  the README). The host's console-hardening gates (`legacy_tiocsti`, Yama, `/dev/uinput`, X11 vs
  Wayland) are NOT testable in this container and move to the Tier 2 VM / Tier 3 CI matrix.

## 2. How attackers must operate (ENFORCED + VALIDATED)

- Run the real client AS a listed user: `/opt/envh-arena/bin/envh-rt1 <args>` (and `envh-rt2`),
  or the unlisted angle `/opt/envh-arena/bin/envh-outsider <args>`.
- Or speak raw NDJSON to the socket AS that user:
  `sudo -u rt1 env ENVH_SOCKET=<socket> /opt/envh-arena/venv/bin/python /opt/envh-arena/scripts/raw_client.py '<json>'`,
  or a helper script `sudo -u rt1 /opt/envh-arena/venv/bin/python <script>`.
- Destructive/DoS tests MUST use their own instance (`sudo bash arena/bootstrap.sh dosN`), not the
  shared `main`, so they don't wedge the arena for other agents.
- You MAY read envh's source (it is open source): `/home/user/ai-env-handler/src`.

## 3. OUT OF SCOPE — "root-flavored" attacks (DO NOT ATTEMPT, DO NOT REPORT)

Any of the following **auto-disqualifies** a finding (they require root or privileges a normal
listed user does not have, and are acknowledged in SECURITY.md):

1. Reading the instance `data/` directly (vault, config, presets, audit) — it is 0700 service-user.
2. Reading the broker's memory: `/proc/<pid>/mem`, `gcore`, `ptrace`, a debugger, core dumps.
3. Reading the broker's `/proc/<pid>/environ` or cmdline of the service-user/root processes.
4. `sudo`, `su`, running as the service user/root, or any privilege escalation.
5. Modifying/replacing/patching the broker code, the venv, the arena scripts, or the binary;
   killing/replacing the broker process; swapping the socket.
6. Reading the orchestrator's files/context, this session's transcript, `secrets.env`, or `/root`.
7. The console/TUI keystroke-injection, X11/uinput/tmux, fake-prompt, and unlock-hour attacks —
   the HUMAN's surface, documented in the README. (Static review of console code for a NEW
   socket-triggerable logic bug IS in scope.)
8. Capturing `AUTO_CANARY` (the positive-control auto key) — releasing an `auto` key with no
   approval is documented behavior, not a finding.

If the only way to the flag is one of the above, record it as "out-of-scope / known" and move on.

## 4. WIN CONDITIONS (what a real finding looks like)

- **FLAG EXFIL**: the value of `CAPTURE_THE_FLAG` (or any non-auto secret) reaches an
  `rt*`/`outsider` process WITHOUT a console approval. Proof = the `CTF-FLAG-…` string in captured
  output.
- **APPROVAL BYPASS/FORGERY**: a request treated as approved, or a run/session started, without the
  passphrase being typed on a console.
- **POLICY/PRESET INTEGRITY**: a client causing `config.yaml`/`presets.yaml`/the vault to change (or
  a preset/import to be applied) without a console approval.
- **CROSS-USER**: `rt1` using/ending/reading `rt2`'s (or another user's) session, request, or
  secret; or an unlisted uid getting any capability beyond a clean rejection; or any cross-user
  information leak.
- **DoS / INTEGRITY**: a client crashing the broker, wedging its event loop, exhausting it, or
  corrupting its state/files, from the socket.
- **HOOK BYPASS**: a shell command that starts an envh passphrase-requiring request but
  `claude/hooks/envh_ask.py` returns "no ask" (or mis-parses), BEYOND the bypasses the README lists
  and the V8 fixes already cover.
- **SUPPLY CHAIN**: a dependency, the installer/wizard, or packaging allowing code execution or
  secret capture a normal user/agent could trigger or plant, not already acknowledged.

### 4a. AI-agent threat scenarios (NEW — model these explicitly)

Mapped to OWASP LLM/Agentic classes and Microsoft's benign/adversarial actor ontology (see
`standards/`). These are still judged by the §4 win conditions and §3 scope; they are *lenses*, not
new win conditions:

- **Excessive agency / confused deputy**: an agent with legitimate access is steered (by injected
  content, a crafted `--reason`, a preset proposal, a bait-and-switch of the command) into
  surfacing or widening access beyond what the human would approve. Note: the README acknowledges
  envh approves a command line, not the code it runs; a finding here must beat that acknowledgement
  (e.g., a NEW mis-parse or a policy/preset write without approval), not restate it.
- **Tool-misuse / exfil-via-channel**: using envh's own documented flows (search/match, import,
  preset propose, run) to move value or value-derived bits to the attacker without approval.
- **Prompt-injection → hook**: content that drives the hook to the wrong decision.

## 5. NOVELTY TEST (is it already acknowledged or already fixed?)

A finding counts as NOVEL only if it is NOT:
- described in `README.md` or `SECURITY.md` (bearer-token session reuse; unverified
  reason/pid/cmdline; the approved command seeing its values; same-user clipboard/show; the hook's
  acknowledged bypass classes; the unlock-hour window; X11/uinput/tmux; no network/key-rotation),
  **nor**
- already fixed in the 2026-10-05 pass: **V1** alias-bomb `repr()` DoS, **V2** oversized-YAML wedge,
  **V3** fd-exhaustion / idle-timeout, **V4** uncapped-`reason` disk-fill, **V5** duration
  overflow, **V6** import equality oracle, **V7** `_next_id` cross-user count leak, **V8** the 10
  hook bypasses. Re-deriving a V1–V8 issue on current `main` that the regression test says is fixed
  is **REJECTED** unless you demonstrate a genuine **bypass** of the fix.
- **World novelty**: also cross-check against public prior art (OWASP/ATT&CK/ATLAS/CVE); a technique
  the project docs simply never named is still reportable, but say where it is known.

A real-but-acknowledged issue is graded **KNOWN**. A re-broken fix is a **regression finding**.

## 6. GRADING RUBRIC (each finding gets a verdict)

An independent grading agent, working ONLY as `rt1`/`rt2`/`outsider` (never root), assigns:

- **CONFIRMED** — all of: (a) in scope per §3; (b) independently REPRODUCED by the grader by
  re-running the stated steps as a non-root user at the right arena tier, with output pasted and the
  arena `environment_id` recorded; (c) real impact per §4/§4a; (d) NOVEL per §5. Severity:
  CRITICAL (flag/other secret exfil or approval forgery) / HIGH (cross-user, policy integrity,
  reliable DoS) / MEDIUM (partial info leak, hook bypass, limited DoS) / LOW (hardening gap). Map
  each to ATT&CK/ATLAS/OWASP IDs (`standards/TAXONOMY_MAP.md`).
- **KNOWN** — reproduced and real, but acknowledged in README/SECURITY or fixed as V1–V8. Listed
  separately.
- **REJECTED** — not reproducible, out of scope (§3), no impact, depends on root, or "it would work
  if…". Give the reason and the clause number.

A finding with no reproduction, or whose reproduction a grader cannot replicate as a non-root user,
is REJECTED. No exceptions.

## 7. COORDINATION, DIVERSITY & COVERAGE PROTOCOL

- Each attack agent owns ONE disjoint surface family from `SURFACE_MAP.md` (F1–F16 + regression),
  and before starting READS all existing `evidence/findings/*.json` and the surface-map "tried"
  notes; it must NOT repeat a recorded idea (positive OR negative) — it goes deeper or to an
  adjacent untried idea.
- **Diversity (anti-groupthink)**: agents are given distinct personas/specializations (protocol
  fuzzer, Linux-privilege expert, supply-chain analyst, agent-exfil/prompt-injection, benign
  over-reach, cross-user, info-leak, DoS). An automated **large-N fuzz sweep** (F16) complements the
  reasoning agents, because attack success scales with samples.
- Each agent WRITES one file `evidence/findings/agent-<ID>.json` (one file per agent = no write
  contention), recording BOTH candidate findings AND negative results and out-of-scope observations.
- Later waves receive a digest of earlier results and push into the gaps.
- **Coverage honesty**: the final report states what was covered, at what effort, and explicitly
  **what was NOT executed** (notably the Tier 2 VM hardening-gate tests, which this container cannot
  run). Silent gaps are forbidden.

## 8. FINDINGS FILE SCHEMA (`evidence/findings/agent-<ID>.json`)

```json
{
  "agent_id": "w1-protocol",
  "surface": "F1 socket-protocol",
  "persona": "protocol fuzzer",
  "environment_id": "<from arena/fingerprint.py>",
  "candidates": [
    {"id":"w1-protocol-1","title":"...","win_condition":"FLAG EXFIL|APPROVAL BYPASS|POLICY/PRESET INTEGRITY|CROSS-USER|DoS/INTEGRITY|HOOK BYPASS|SUPPLY CHAIN",
     "repro_cmds":["exact commands, as rt1/rt2/outsider"],"observed_output":"pasted output",
     "flag_or_value_obtained":"CTF-FLAG-... or null","in_scope":true,
     "why_novel":"not in README/SECURITY and not V1-V8 because ...","taxonomy":["ATTACK:T1552","OWASP-LLM:LLM02","ATLAS:..."],
     "severity":"CRITICAL|HIGH|MEDIUM|LOW","self_assessment":"CONFIRMED|KNOWN|REJECTED","notes":"root cause with src file:line"}
  ],
  "negative_results": ["tried ... as rt1, got ..., no leak"],
  "out_of_scope_observations": ["things that would need root, noted but NOT claimed"]
}
```

## 9. Orchestrator commitments (anti-drift)

- I will NOT approve any request for `CAPTURE_THE_FLAG`, ever.
- I will NOT relax §3 to admit a root-flavored result, however interesting.
- The final vulnerability list contains ONLY findings graded CONFIRMED by an independent grading
  agent that reproduced them as a non-root user. KNOWN items are listed separately. Everything else
  is dropped.
- Internal LLM graders have a credibility ceiling (they are independent of the attackers but not a
  third party); HIGH/CRITICAL findings are written so they can go to external human review.
- Charter SHA-256 and git commit are recorded in `PRECOMMIT.txt` before Wave 1 launches.

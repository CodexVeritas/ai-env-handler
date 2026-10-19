# envh red-team arena (Tier 0: faithful headless broker + real unprivileged users)

This is the verification environment attacker and grader agents run against. It stands up the
**same production broker + control-socket code path** headlessly, with **no approver**, and lets
you hit it **only as real non-root users** over the kernel uid boundary — so a leak observed here
is a leak that needs no console and no root, exactly the charter's win condition.

## Why this tier, and what it can / cannot prove

`envh`'s trust boundary is the Linux kernel's uid separation (`SO_PEERCRED`, 0600 file
ownership). That is fully exercised here with real users. `envh`'s *console hardening gates*
(`dev.tty.legacy_tiocsti=0`, `kernel.yama.ptrace_scope>=1`, `/dev/uinput`, X11 vs Wayland) are
non-namespaced host-kernel properties a container cannot set — this container reports
`legacy_tiocsti=1`, no Yama node, no `/dev/uinput`, so those gates **cannot be tested here** and
belong in the Tier 2 VM / Tier 3 CI matrix (see `../tiers/`). The full-screen TUI also can't run
headlessly; its socket-triggerable logic is in scope via the broker, its keyboard/display surface
is not (it is the human's, and acknowledged in the README).

## Quick start

```bash
sudo bash bootstrap.sh            # stand up instance "main" (idempotent for users/venv)
sudo bash selfcheck.sh            # PROVE the arena is sound before trusting any result
```

`selfcheck.sh` must print `SELFCHECK: SOUND`. It runs a **positive control** (an `auto` canary
key that must leak its value to a non-root user — proves the detector works), **liveness
controls** (the unlisted `outsider` is refused; the flag does *not* leak without approval), and
**negative controls** (the prior pass's V1–V8 regression tests still pass). If it prints UNSOUND,
do not trust campaign results.

## Attacking as a non-root user

Three ways, all as `rt1`/`rt2` (listed) or `outsider` (unlisted):

```bash
# 1) the real envh client
/opt/envh-arena/bin/envh-rt1 list
/opt/envh-arena/bin/envh-rt1 session start app --minutes 30 --reason test   # will hang: no approver

# 2) raw NDJSON to the socket (malformed/typed/oversized messages the CLI won't send)
sudo -u rt1 env ENVH_SOCKET=/run/envh-arena/main.sock \
  /opt/envh-arena/venv/bin/python /opt/envh-arena/scripts/raw_client.py '{"op":"status"}'

# 3) your own script, run as the user
sudo -u rt1 /opt/envh-arena/venv/bin/python /path/to/exploit.py
```

The vault/config/audit live in `/opt/envh-arena/instances/<instance>/data` (0700, owned by the
service user) — reading them directly is **out of scope** (root-flavored). You win only by making
a value reach an `rt*`/`outsider` process, forging an approval, changing policy/presets without
approval, breaking cross-user isolation, crashing/wedging the broker from the socket, or bypassing
the hook — see `../CHARTER.md`.

## Isolated instances (for DoS / destructive tests)

Don't wedge the shared `main` broker for everyone. Spin your own:

```bash
sudo bash bootstrap.sh dos1       # new data dir + socket /run/envh-arena/dos1.sock + its own flag
ENVH_SOCKET=/run/envh-arena/dos1.sock /opt/envh-arena/bin/envh-rt1 status
sudo bash teardown.sh dos1        # clean up just that instance
```

## Reproducibility

Every finding records the arena `environment_id` from `fingerprint.py` (hash of git commit,
kernel, the hardening-gate sysctls, display server, python, pyrage). Re-running `bootstrap.sh` at
the same commit reproduces the same configuration; the flag/passphrase are random per instance and
printed only to the root-owned `secrets.env` (graders read the expected FLAG/CANARY there).

## Files

| File | Purpose |
|---|---|
| `bootstrap.sh` | create users + venv, seed a fresh vault, launch the headless broker, health-check |
| `selfcheck.sh` | positive + liveness + negative controls; gates trust in the arena |
| `init_vault.py` | seed vault/config/presets (flag, auto-canary, decoys) — run as the service user |
| `serve_broker.py` | the headless broker (production code path, no TUI, no approver) |
| `raw_client.py` | one NDJSON exchange as any user (fuzzing / protocol attacks) |
| `fingerprint.py` | the reproducibility environment id |
| `teardown.sh` | stop an instance, or `--all` to remove users and the arena root |

## Teardown

```bash
sudo bash teardown.sh main        # one instance
sudo bash teardown.sh --all       # everything (users + /opt/envh-arena)
```

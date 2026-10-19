# Verification tiers — what runs where, and why

The authentic answer to "unprivileged users, Docker, or VMs?" is **all three, in tiers, because
each validates a different layer.** `envh`'s trust boundary (kernel uid separation via
`SO_PEERCRED`, 0600 ownership) is testable with real unprivileged users and is container-friendly.
Its *console-hardening gates* (`dev.tty.legacy_tiocsti=0`, `kernel.yama.ptrace_scope>=1`,
`/dev/uinput`, X11 vs Wayland) are **non-namespaced host-kernel properties a container cannot set**
— so a "pass" for them inside a container is meaningless; they require **VMs**.

See `../standards/METHODOLOGY.md` for the research behind this.

| Tier | Tech | Validates | Can't validate | Runs in the cloud container? |
|---|---|---|---|---|
| **T0** | real unprivileged users on one host (`../arena/`) | SO_PEERCRED uid auth, cross-user deny, 0600 ownership, socket protocol, hooks, DoS, broker logic | anything kernel/sysctl/LSM/display-dependent | **Yes** — the live campaign runs here |
| **T1** | containers (`container/`) | distro/userspace matrix (sudo defaults, packaging, udev presence), disposable multi-user topologies | the hardening gates (shared kernel; Docker refuses non-namespaced sysctls) | Yes if a Docker daemon is available |
| **T2** | VMs with explicit kernel/sysctl states (`vm/`) | `legacy_tiocsti` 0↔1, Yama present/absent, `/dev/uinput` perms, X11 vs Wayland, NOPASSWD sudo, docker-group — each a positive gate or a **negative** (degraded) assurance test | — | **No** — needs its own kernel; run on a VM host or KVM CI |
| **T3** | CI matrix (`../../../.github/workflows/redteam-matrix.yml`) | distro × kernel × sysctl × display coverage, with the arena selfcheck + gate tests per cell, pinning the environment id | — | **No** — runs in GitHub Actions |

## The kernel-sharing fact (why T1 ≠ T2)

`dev.tty.legacy_tiocsti` and `kernel.yama.ptrace_scope` are **not namespaced**: Docker rejects them
with "sysctl is not in a separate kernel namespace" and tells you to set them on the host. A
container inherits the host's values, and `/dev/uinput` is a host device node. So the gate tests
*must* run in a VM whose guest kernel you control. This container proves the point: it reports
`legacy_tiocsti=1`, no Yama node, no `/dev/uinput` — the two most important console gates cannot
even be exercised here.

## Negative / assurance testing (the important part)

The degraded configs are exactly where `envh`'s guarantees are documented to weaken, so each gets a
test that asserts the guarantee **fails closed and is reported**, not silently lost (NIST SP 800-53
SI-6; OWASP SAMM misuse testing). `vm/gate_tests.sh` encodes these. The baseline (hardened) states
are proven with `sysctl`/Lynis preflight before `envh`'s own checks run.

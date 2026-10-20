# Install envh

Setup takes about 10 minutes, plus about 15 for the optional scan. **Linux only.** Mac and Windows aren't supported; see [porting.md](porting.md).

## Before you start

| You need | Check with | If not |
|---|---|---|
| Linux 6.2 or newer | `uname -r` | Ubuntu 22.04's original kernel is 5.15: install the newer one with `sudo apt install linux-generic-hwe-22.04`, or upgrade to 24.04 |
| `sudo` that asks for your password | `sudo -l` shows no `NOPASSWD` lines | see [If the system checks stop the install](#if-the-system-checks-stop-the-install) |
| `git` and `python3` | `git --version; python3 --version` | install them with your package manager |
| Claude Code or Cursor opened at least once, if you use them | `ls -d ~/.claude ~/.cursor` | open them once, or rerun the wizard later; it skips tools it can't find |

The wizard checks the rest itself (kernel settings, groups, sudo rules) and says how to fix each problem.

## Install

Open a terminal window of its own (not a terminal inside an AI tool) and run:

```bash
git clone https://github.com/CodexVeritas/ai-env-handler.git envh && cd envh
python3 scripts/setup_wizard.py
```

The wizard walks through eight short steps and asks before each change. Type `?` at any question to see what it changes, or pick "Every detail" at the start (or pass `--verbose`) to see it all along the way.

| Step | What it does |
|---|---|
| 1. Docker group | Offers to take you out of the `docker` group, which can become root without a password. Default: no. |
| 2. Install envh | Asks for your sudo password, copies envh to `/opt/envh` and creates your vault. You choose a **vault passphrase** and get a **console phrase** to write down. |
| 3. Start the console | You run `sudo envh-console` in a new window and leave it open. See [console.md](console.md). |
| 4. Import keys | Moves one project's `.env` keys into the vault. Optional. See [import.md](import.md). |
| 5. Editor `.env` history | Stops VS Code and Cursor from keeping copies of `.env` files each time you save. |
| 6. Connect Claude Code | Adds the envh skill, the hook and three lines to `~/.claude/CLAUDE.md`. See [agents.md](agents.md). |
| 7. Connect Cursor | Adds the same skill and hook for Cursor. |
| 8. Find stray keys | Scans your home folder for leftover copies of keys, then opens the explorer. It will miss some. See [scan.md](scan.md). |

**Pick a vault passphrase you will remember.** It is stored nowhere, so there is no reset: forgetting it means `sudo envh uninstall --purge`, installing again and adding your keys again.

Later, run the wizard again to upgrade (after `git pull`) or to import more projects.

## If the system checks stop the install

Before it builds anything, the install checks for ways a program running as you could become root and then read your keys. Each problem says how to fix it. You can continue anyway after checking each one yourself, but the protection is then weaker.

| The check says | What to do |
|---|---|
| `user … is in root-granting group(s) docker` (or `lxd`, `incus-admin`, `libvirt`, `microk8s`, `disk`) | Leave the group: answer yes at the wizard's first step, or run `sudo gpasswd -d $USER <group>`, then log out and back in. Docker commands then need `sudo`, so agents can't run them. Docker Desktop and rootless Docker don't need the group. |
| `… can use /var/run/docker.sock, the socket of a daemon that runs containers as root` | An ACL or a mode lets you use a root container daemon without the group. Remove it, or use Docker Desktop or rootless Docker instead. |
| `kernel has no dev.tty.legacy_tiocsti control` | Your kernel is older than 6.2. Update it (see [Before you start](#before-you-start)). |
| `dev.tty.legacy_tiocsti is enabled` | Run `sudo sysctl -w dev.tty.legacy_tiocsti=0`, and keep it after reboots with `echo 'dev.tty.legacy_tiocsti = 0' \| sudo tee /etc/sysctl.d/60-envh.conf`. |
| `Yama LSM is not available` or `kernel.yama.ptrace_scope is 0` | Set `kernel.yama.ptrace_scope = 1` in a file in `/etc/sysctl.d/`, then `sudo sysctl --system`. |
| `… can run these with sudo without a password` | Remove those `NOPASSWD` rules from `/etc/sudoers.d/`, unless each is a fixed root-owned command that reads no file you can write. |
| `… can run doas without a password` | Remove `nopass` from the lines it lists in `/etc/doas.conf`. |
| `sudo's !tty_tickets` or `timestamp_type=global` | sudo shares a password typed in one terminal with all of them. Remove that setting with `sudo visudo`. |
| `sudo has no secure_path` | Add a `secure_path` with `sudo visudo`, or always start the console with `sudo /usr/local/sbin/envh-console`. |
| `… can write /usr/local/bin, where root runs envh from` (or another folder) | Run the `chown` and `chmod` command it shows. |
| Warnings, not stops: `X11 session`, the `input` group, or a `/dev/uinput` you can write | Programs you run can read what you type or type into the console window. See [security.md](security.md#who-else-can-see-and-type-into-the-console). |

## What the install changes

The wizard's install step asks for sudo once, and runs `sudo -k` afterwards so the terminal forgets it. If root has no `uv` of its own, the wizard first offers to install one into `/usr/local/bin` with astral.sh's installer (a `uv` in your home folder could be swapped by anything running as you before root runs it); you can use your own instead, at that risk.

As root, it copies the source files from your clone to a root-only folder and checks the system there. It stops where a process running as you could become root: it needs Linux 6.2 or newer with Yama; `sudo` that asks for a password (no `NOPASSWD` rules for you), keeps a password typed in one terminal to that terminal (no `!tty_tickets` or `timestamp_type=global`) and has a `secure_path`, so `sudo envh-console` can't run a copy from your `PATH`; no `doas` rule with `nopass` for you; your user outside root-granting groups (`docker`, `lxd`, `incus-admin`, `libvirt`, `microk8s`, `disk`), unable to use the socket of a daemon that runs containers as root (Docker, containerd, Podman, LXD, Incus) through an ACL or a mode, and unable to write `/usr/local`, `/usr/local/bin`, `/usr/local/sbin`, `/opt` or `/opt/envh`, where root runs envh from. It warns on X11, on membership in the `input` group and on a `/dev/uinput` you can write. Each problem says how to fix it; you can continue anyway after checking each one yourself. The wizard's first step offers to fix the most common one for you: if you say yes, it runs `sudo gpasswd -d <you> <group>` for each root-granting group you are in. Docker commands then need sudo, so agents can't run them; Docker Desktop and rootless Docker don't need the group. It takes effect when you log out and back in. `envh install` runs the same checks again, so running it directly also refuses on a problem unless you pass `--ignore-preflight` (which then says how many it ignored). Run it with `sudo` from your own account: from a plain root shell it cannot tell whose account to check, so it refuses.

Then it builds `/opt/envh` from that copy: Python 3.12 in `/opt/envh/python`, envh in `/opt/envh/env`, and the commit it came from in `/opt/envh/installed-from`. It links `/usr/local/bin/envh` and runs `envh install` (`sudo envh install --dry-run` lists its changes), which:

1. creates the service user `envh` with home `/var/lib/envh` (mode 0700);
2. installs `/usr/local/sbin/envh-console`, the root helper that starts the broker;
3. installs a desktop launcher named "envh console" if a desktop terminal is found;
4. creates the vault and the policy in `/var/lib/envh`, owned by `envh`: it writes `users: [<your login>]` into the policy, you choose the **vault passphrase** (stored nowhere; you type it to start the console and to approve requests), and you get a **console phrase**.

Nothing else on your system changes: no sudo, shell or desktop settings.

**Run the wizard in a terminal window of its own.** It refuses to run inside Claude Code, but it cannot tell when another AI tool shares its terminal. While the install runs, a sudo password typed there is usable from that terminal.

**envh never runs from your clone.** Your agents can edit the clone, and the console runs the code that holds your keys, so that code must live where only root can change it.

## Upgrade

`git pull` in your clone and run the wizard again. It asks before updating (`?` shows the installed commit next to the clone's), and asks again if the clone has uncommitted changes, since root installs those too. The vault, its passphrase and the policy are kept. Restart the console afterwards: the running broker keeps the old code until then. The Claude Code and Cursor steps then compare the skill and hook copies with the clone, and say if they are out of date. Copies you made by hand somewhere else, such as in a project, you update yourself.

## Uninstall

`sudo envh uninstall` keeps the vault; `sudo envh uninstall --purge` deletes it too.

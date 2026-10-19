# Install the envh

## Install

Open a terminal window of its own (not a terminal inside an AI tool) and run:

```bash
git clone https://github.com/CodexVeritas/ai-env-handler.git envh && cd envh
python3 scripts/setup_wizard.py
```

The wizard walks through eight short steps and asks before each change. The first offers to take you out of the `docker` group, which can become root without a password; the install's checks flag it otherwise. One of them keeps VS Code and Cursor from storing copies of `.env` files in their local history. The last one scans your home folder for stray copies of keys and then opens the explorer (`envh explore`) right there, so you can go through what it found and replace copies. Type `?` at any question to see what it changes, or pick "Every detail" at the start (or pass `--verbose`) to see it all along the way. Linux only.

Later, run it again to upgrade (after `git pull`) or to import more projects.

## What the install changes, upgrading, uninstalling

The wizard's install step asks for sudo once, and runs `sudo -k` afterwards so the terminal forgets it. If root has no `uv` of its own, the wizard first offers to install one into `/usr/local/bin` with astral.sh's installer (a `uv` in your home folder could be swapped by anything running as you before root runs it); you can use your own instead, at that risk.

As root, it copies the source files from your clone to a root-only folder and checks the system there. It stops where a process running as you could become root: it needs Linux 6.2 or newer with Yama; `sudo` that asks for a password (no `NOPASSWD` rules for you), keeps a password typed in one terminal to that terminal (no `!tty_tickets` or `timestamp_type=global`) and has a `secure_path`, so `sudo envh-console` can't run a copy from your `PATH`; no `doas` rule with `nopass` for you; your user outside root-granting groups (`docker`, `lxd`, `incus-admin`, `libvirt`, `microk8s`, `disk`), unable to use the socket of a daemon that runs containers as root (Docker, containerd, Podman, LXD, Incus) through an ACL or a mode, and unable to write `/usr/local`, `/usr/local/bin`, `/usr/local/sbin`, `/opt` or `/opt/envh`, where root runs envh from. It warns on X11, on membership in the `input` group and on a `/dev/uinput` you can write. Each problem says how to fix it; you can continue anyway after checking each one yourself. The wizard's first step offers to fix the most common one for you: if you say yes, it runs `sudo gpasswd -d <you> <group>` for each root-granting group you are in. Docker commands then need sudo, so agents can't run them; Docker Desktop and rootless Docker don't need the group. It takes effect when you log out and back in. `envh install` runs the same checks again, so running it directly also refuses on a problem unless you pass `--ignore-preflight` (which then says how many it ignored). Run it with `sudo` from your own account: from a plain root shell it cannot tell whose account to check, so it refuses.

Then it builds `/opt/envh` from that copy: Python 3.12 in `/opt/envh/python`, envh in `/opt/envh/env`, and the commit it came from in `/opt/envh/installed-from`. It links `/usr/local/bin/envh` and runs `envh install` (`sudo envh install --dry-run` lists its changes), which:

1. creates the service user `envh` with home `/var/lib/envh` (mode 0700);
2. installs `/usr/local/sbin/envh-console`, the root helper that starts the broker;
3. installs a desktop launcher named "envh console" if a desktop terminal is found;
4. creates the vault and the policy in `/var/lib/envh`, owned by `envh`: it writes `users: [<your login>]` into the policy, you choose the **vault passphrase** (stored nowhere; you type it to start the console and to approve requests), and you get a **console phrase**.

Nothing else on your system changes: no sudo, shell or desktop settings.

The wizard refuses to run inside Claude Code, but it cannot tell when another AI tool shares its terminal. Run it in a terminal window of its own: while the install runs, a sudo password typed there is usable from that terminal.

envh never runs from your clone. Your agents can edit the clone, and the console runs the code that holds your keys, so that code must live where only root can change it.

**Upgrade:** `git pull` in your clone and run the wizard again. It asks before updating (`?` shows the installed commit next to the clone's), and asks again if the clone has uncommitted changes, since root installs those too. The vault, its passphrase and the policy are kept. Restart the console afterwards: the running broker keeps the old code until then. The Claude Code and Cursor steps then compare the skill and hook copies with the clone, and say if they are out of date. Copies you made by hand somewhere else, such as in a project, you update yourself.

**Uninstall:** `sudo envh uninstall` keeps the vault; `sudo envh uninstall --purge` deletes it too.

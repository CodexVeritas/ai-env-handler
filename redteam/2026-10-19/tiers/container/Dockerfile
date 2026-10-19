# Tier 1 — container image for the distro/userspace leg of the matrix.
#
# IMPORTANT: a container SHARES THE HOST KERNEL. It validates the uid boundary, socket protocol,
# cross-user isolation, hooks, and broker logic across a distro's userspace (sudo defaults,
# packaging, udev presence). It CANNOT validate the console-hardening gates (legacy_tiocsti, Yama,
# /dev/uinput) — those are non-namespaced kernel properties; use Tier 2 VMs for them.
#
# Build arg DISTRO lets the matrix sweep base images.
ARG DISTRO=ubuntu:24.04
FROM ${DISTRO}

RUN set -eux; \
    (command -v apt-get && apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv sudo curl ca-certificates git util-linux && rm -rf /var/lib/apt/lists/*) \
    || (command -v dnf && dnf install -y python3 sudo curl ca-certificates git util-linux) \
    || (command -v pacman && pacman -Sy --noconfirm python sudo curl ca-certificates git util-linux)

# uv for the arena venv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && ln -sf /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /src
COPY . /src
# The arena bootstrap creates its own users and venv inside the container.
CMD ["bash", "redteam/2026-10-19/tiers/container/run.sh"]

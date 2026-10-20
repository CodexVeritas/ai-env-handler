#!/bin/sh
# Runs as root in a fresh container of one Linux distribution: adds what a desktop install of it already has and a
# user who can sudo with a password, then has that user run the setup wizard end to end (scripts/ci/drive_wizard.py).
# Usage: docker run --rm -v "$PWD:/src:ro" <image> sh /src/scripts/ci/install_matrix.sh
set -eu
. /etc/os-release
echo "== $PRETTY_NAME"
export CI_PASSWORD=envh-ci-password
case " $ID ${ID_LIKE:-} " in
    *" debian "* | *" ubuntu "*)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        # Ubuntu's desktop installs have wget but no curl.
        apt-get install -y -qq python3 sudo git procps wget ca-certificates >/dev/null
        admin_group=sudo
        ;;
    *" fedora "* | *" rhel "*)
        dnf install -y -q python3 sudo git-core procps-ng util-linux shadow-utils >/dev/null
        admin_group=wheel
        ;;
    *" arch "*)
        pacman -Syu --noconfirm --needed --quiet python sudo git procps-ng curl >/dev/null
        # Arch ships this rule commented out; its install guide has you turn it on.
        echo '%wheel ALL=(ALL:ALL) ALL' >/etc/sudoers.d/10-wheel
        admin_group=wheel
        ;;
    *" suse "* | *" opensuse "*)
        zypper --non-interactive --quiet install python3 sudo git-core procps curl shadow util-linux >/dev/null
        # Its sudoers lets every user in with root's password, so no group is needed.
        admin_group=
        ;;
    *)
        echo "no setup for $ID" >&2
        exit 1
        ;;
esac
useradd --create-home ${admin_group:+--groups "$admin_group"} --shell /bin/bash tester
echo "tester:$CI_PASSWORD" | chpasswd
# openSUSE's sudo asks for root's password instead of the user's.
echo "root:$CI_PASSWORD" | chpasswd
mkdir /home/tester/envh /home/tester/.claude
tar -C /src --exclude=./.venv -cf - . | tar -C /home/tester/envh -xf -
chown -R tester /home/tester/envh /home/tester/.claude
exec runuser -l tester -c "cd ~/envh && CI_PASSWORD=$CI_PASSWORD python3 scripts/ci/drive_wizard.py"

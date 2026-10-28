#!/bin/bash
# One-time root setup for AmneziaWG and awg_gui.py:
#  - AmneziaWG tools + DKMS kernel module from the tigro/amneziawg Copr
#    (its source tarballs were byte-identical to the official amnezia-vpn GitHub tags on 2026-10-28)
#  - PySide6 for the GUI
#  - a polkit rule so <user> can start/stop awg-quick@ tunnels without a password
# Usage: sudo bash setup.sh "$USER"   (or: pkexec bash setup.sh "$USER")
set -euo pipefail
user=${1:?usage: setup.sh <user>}
id "$user" >/dev/null

dnf -y copr enable tigro/amneziawg
dnf -y install amneziawg-tools amneziawg-dkms "kernel-devel-$(uname -r)" python3-pyside6
# the RPM scriptlet can run before this kernel's headers land in the same transaction (and hides the failure)
dkms autoinstall -k "$(uname -r)"
modprobe amneziawg
echo "amneziawg kernel module loaded: $(modinfo -F version amneziawg)"

cat > /etc/polkit-1/rules.d/50-amneziawg-gui.rules <<EOF
// Written by amneziawg-gui/setup.sh: lets $user start/stop AmneziaWG tunnels without a password.
// Configs stay root-only in /etc/amnezia/amneziawg, so this can't be used to run their PostUp hooks.
polkit.addRule(function (action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "$user" && subject.local && subject.active &&
        /^awg-quick@[A-Za-z0-9_.-]{1,15}\.service\$/.test(action.lookup("unit")) &&
        ["start", "stop", "restart"].indexOf(action.lookup("verb")) >= 0) {
        return polkit.Result.YES;
    }
});
EOF
chmod 644 /etc/polkit-1/rules.d/50-amneziawg-gui.rules
echo "setup done"

#!/usr/bin/env bash
# Audited release for this IPC only. No USB host reset, reboot, or CAN UP.
set -euo pipefail
action=${1:-check}
case "$action" in check|apply|rollback) ;; *) echo "Usage: $0 check|apply|rollback" >&2; exit 2;; esac
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release=/home/linux/gs_usb-repair-20260922
installed=/lib/modules/6.1.118/extra/gs_usb.ko
backup=/var/lib/yam-gs-usb/20260922
old_sha=513caff387c8cb1c73df189fc0ee1d9ab8574dff74ff153b91110fa0967a8f66
new_sha=db11fa3f71f67b43abe141233cc64e92ba904fb923d2bbe60905e3f67e58b4de
version=6.1.118-yam1
uid_linux=$(id -u linux)
uctl() {
    if [[ $EUID == 0 ]]; then
        runuser -u linux -- env XDG_RUNTIME_DIR="/run/user/$uid_linux" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_linux/bus" systemctl --user "$@"
    else
        systemctl --user "$@"
    fi
}
digest() { sha256sum "$1" | cut -d ' ' -f1; }
[[ $(uname -r) == 6.1.118 && $(uname -m) == aarch64 ]]
[[ $(digest "$release/build/gs_usb.ko") == "$new_sha" ]]
[[ $(digest "$release/original/gs_usb.ko") == "$old_sha" ]]
[[ $(modinfo -F version "$release/build/gs_usb.ko") == "$version" ]]
[[ $(modinfo -F vermagic "$release/build/gs_usb.ko") == "$(modinfo -F vermagic "$installed")" ]]
current_sha=$(digest "$installed")
[[ $current_sha == "$old_sha" || $current_sha == "$new_sha" ]]
python3 "$script_dir/check_idle.py"
[[ $(uctl show yam-executor.service -p ActiveState --value) == inactive ]]
for unit in yam-device.service yam-workstation.service; do
    state=$(uctl show "$unit" -p ActiveState --value)
    [[ $state == active || $state == inactive || $state == failed ]]
done
uctl show yam-device.service -p ExecStart --value | grep -q -- '--device-daemon'
uctl show yam-workstation.service -p ExecStart --value | grep -q -- '--web-only'
if [[ $action == check ]]; then
    echo "Preflight passed; no service, driver, or CAN changes made."
    exit 0
fi
[[ $EUID == 0 ]] || { echo "Run apply/rollback via sudo." >&2; exit 1; }
if [[ $action == apply ]]; then
    target="$release/build/gs_usb.ko"
    expected_sha=$new_sha
else
    target="$release/original/gs_usb.ko"
    expected_sha=$old_sha
fi
if [[ $current_sha == "$expected_sha" ]]; then
    echo "Requested version is already installed; refusing an unnecessary reload."
    exit 1
fi
install -d -m 0700 "$backup"
install -m 0600 "$release/original/gs_usb.ko" "$backup/gs_usb.ko.original"
# Stage a root-owned module before stopping anything.
install -m 0644 "$target" "$backup/gs_usb.ko.target"
[[ $(digest "$backup/gs_usb.ko.target") == "$expected_sha" ]]
start=$(date '+%Y-%m-%d %H:%M:%S')
trap 'echo "Switch incomplete. Services may be stopped; inspect the log. Do not reset USB or force-unload. Original: /var/lib/yam-gs-usb/20260922/gs_usb.ko.original" >&2' ERR
# Closing the Web gate first prevents another browser request from connecting arms.
uctl stop yam-workstation.service
python3 "$script_dir/check_idle.py"
uctl stop yam-device.service
[[ $(uctl show yam-device.service -p MainPID --value) == 0 ]]
python3 "$script_dir/check_idle.py"
modprobe -r gs_usb
[[ ! -d /sys/module/gs_usb ]]
install -m 0644 "$backup/gs_usb.ko.target" "$installed.new"
mv -f "$installed.new" "$installed"
depmod 6.1.118
if ! modprobe gs_usb; then
    install -m 0644 "$backup/gs_usb.ko.original" "$installed.new"
    mv -f "$installed.new" "$installed"
    depmod 6.1.118
    modprobe gs_usb
    echo "Target load failed; original module restored. Services remain stopped." >&2
    exit 1
fi
udevadm settle --timeout=15
[[ $(digest "$installed") == "$expected_sha" ]]
if [[ $action == apply ]]; then
    [[ $(cat /sys/module/gs_usb/version) == "$version" ]]
else
    [[ ! -e /sys/module/gs_usb/version ]]
fi
python3 "$script_dir/check_idle.py"
journalctl -k -b --since "$start" --no-pager
echo "Module switch verified. Services remain stopped for validation."
echo "After checks: as linux, systemctl --user start yam-device.service yam-workstation.service"

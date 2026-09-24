#!/usr/bin/env bash
set -euo pipefail

# Bind to the current Wi-Fi address so DHCP changes after an IPC reboot do not
# leave the Web service listening on an address that is no longer assigned.
ipc_web_host=$(ip -4 -o addr show dev wlan0 | awk '{split($4, address, "/"); print address[1]; exit}')
if [[ -z "$ipc_web_host" ]]; then
    echo "wlan0 has no IPv4 address yet; waiting for Wi-Fi before starting Web" >&2
    exit 1
fi

exec /data/YAM/.venv/bin/yam-workstation \
    --web-only \
    --device-socket="${XDG_RUNTIME_DIR}/yam-device.sock" \
    --web-port 8766 \
    --web-host "$ipc_web_host"

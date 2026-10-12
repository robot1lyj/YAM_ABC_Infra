#!/usr/bin/env bash
set -euo pipefail

# Keep the listener alive across DHCP changes and temporary Wi-Fi loss. Only
# numeric Host values matching the actual local socket destination are added
# dynamically; arbitrary Host names and cross-origin control remain rejected.

ipc_runtime_root=${YAM_WORKSTATION_ROOT:-/data/YAM}
exec "$ipc_runtime_root/.venv/bin/yam-workstation" \
    --web-only \
    --device-socket="${XDG_RUNTIME_DIR}/yam-device.sock" \
    --web-port 8766 \
    --web-host 0.0.0.0 \
    --web-allow-local-addresses

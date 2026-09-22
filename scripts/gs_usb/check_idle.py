#!/usr/bin/env python3
"""Read-only gate for this IPC's gs_usb maintenance; never opens a CAN socket."""

import json
import subprocess
from pathlib import Path


def check_idle():
    expected = {
        "can_lead_l": "5-2.1:1.0",
        "can_lead_r": "5-2.2:1.0",
        "can_left": "5-2.3:1.0",
        "can_right": "5-2.4:1.0",
    }
    actual = {}
    for net in Path("/sys/class/net").iterdir():
        if (net / "device/driver").resolve().name == "gs_usb":
            actual[net.name] = (net / "device").resolve().name
    if actual != expected:
        raise RuntimeError(f"USB-CAN mapping differs: {actual}")
    links = json.loads(subprocess.check_output(["ip", "-j", "link", "show", "type", "can"]))
    if any("UP" in link["flags"] for link in links):
        raise RuntimeError("A CAN interface is UP; finish the supported-arm disconnect first")
    if Path("/sys/module/gs_usb/refcnt").read_text().strip() != "0":
        raise RuntimeError("gs_usb is in use")
    for rcv in Path("/proc/net/can").glob("rcvlist_*"):
        for line in rcv.read_text().splitlines():
            text = line.strip()
            if text and not text.startswith("receive list ") and not text.endswith(": no entry)"):
                raise RuntimeError(f"CAN subscription present in {rcv}: {text}")
    uuid = subprocess.check_output(
        ["findmnt", "-rn", "-o", "UUID", "-M", "/data"], text=True
    ).strip()
    if uuid != "501fb615-1346-455d-9d50-61c1d113faf5":
        raise RuntimeError("Expected NVMe is not mounted at /data")
    if Path("/sys/class/nvme/nvme0/state").read_text().strip() != "live":
        raise RuntimeError("NVMe controller is not live")
    print("Idle gate passed: four mapped gs_usb interfaces DOWN, no CAN subscriptions; NVMe live")


if __name__ == "__main__":
    check_idle()

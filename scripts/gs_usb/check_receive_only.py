#!/usr/bin/env python3
"""20 receive-only open/close cycles; requires patched module and stopped YAM."""

import json
import os
import subprocess
import time
from pathlib import Path

from check_idle import check_idle


def run(*args):
    return subprocess.check_output(args, text=True)


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run through sudo")
    if Path("/sys/module/gs_usb/version").read_text().strip() != "6.1.118-yam1":
        raise RuntimeError("Load the repaired module first")
    check_idle()
    uid = run("id", "-u", "linux").strip()
    for unit in ("yam-device.service", "yam-workstation.service", "yam-executor.service"):
        state = run(
            "runuser",
            "-u",
            "linux",
            "--",
            "env",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "ActiveState",
            "--value",
        ).strip()
        if state != "inactive":
            raise RuntimeError(f"{unit} must be inactive, got {state}")
    names = ("can_lead_l", "can_lead_r", "can_left", "can_right")
    before = json.loads(run("ip", "-j", "-s", "-d", "link", "show", "type", "can"))
    started = time.time()
    completed = 0
    try:
        for cycle in range(20):
            for name in names:
                run(
                    "ip",
                    "link",
                    "set",
                    "dev",
                    name,
                    "up",
                    "type",
                    "can",
                    "bitrate",
                    "1000000",
                    "listen-only",
                    "on",
                )
                state = json.loads(run("ip", "-j", "-d", "link", "show", "dev", name))[0]
                mode = state["linkinfo"]["info_data"]["ctrlmode"]
                if "LISTEN-ONLY" not in mode or "UP" not in state["flags"]:
                    raise RuntimeError(f"Receive-only mode not confirmed: {name}, {mode}")
            time.sleep(0.1)
            for name in names:
                run("ip", "link", "set", "dev", name, "down")
            completed = cycle + 1
            if completed % 5 == 0:
                print(f"Receive-only cycles: {completed}/20 (four adapters)", flush=True)
    finally:
        errors = []
        for name in names:
            try:
                run("ip", "link", "set", "dev", name, "down")
                run("ip", "link", "set", "dev", name, "type", "can", "listen-only", "off")
            except subprocess.CalledProcessError as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError(f"Interface cleanup failed; leave services stopped: {errors}")
    after = json.loads(run("ip", "-j", "-s", "-d", "link", "show", "type", "can"))
    report = {
        "started_epoch": started,
        "finished_epoch": time.time(),
        "cycles_per_adapter": completed,
        "before": before,
        "after": after,
    }
    print(json.dumps(report, indent=2), flush=True)
    for prior in before:
        if prior["ifname"] not in names:
            continue
        current = next(row for row in after if row["ifname"] == prior["ifname"])
        if current["stats64"]["tx"]["packets"] != prior["stats64"]["tx"]["packets"]:
            raise RuntimeError("Unexpected CAN transmission during receive-only check")
    check_idle()
    print("PASS: 80 receive-only open/close operations, no CAN packets transmitted")


if __name__ == "__main__":
    main()

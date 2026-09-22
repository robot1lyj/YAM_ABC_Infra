#!/usr/bin/env python3
"""Exercise RX re-anchor using only the official handle status query 0x50E#FF02.

No SDK construction, motor IDs, enable, motion, EEPROM, or firmware operations.
Only the two mapped Leader handles are queried; Followers remain DOWN.
"""

import json
import os
import socket
import struct
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
    names = ("can_lead_l", "can_lead_r")
    results = {name: {"requests": 0, "replies": 0, "cycles": 0, "latency_ms": []} for name in names}
    before = json.loads(run("ip", "-j", "-s", "-d", "link", "show", "type", "can"))
    started = time.time()
    request = struct.pack("=IB3x8s", 0x50E, 2, b"\xff\x02")
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
                    "off",
                )
                with socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
                    bus.setsockopt(
                        socket.SOL_CAN_RAW,
                        socket.CAN_RAW_FILTER,
                        struct.pack("=II", 0x50F, 0xC00007FF),
                    )
                    bus.settimeout(0.25)
                    bus.bind((name,))
                    for _ in range(100):
                        sent = time.monotonic_ns()
                        bus.sendall(request)
                        results[name]["requests"] += 1
                        response = bus.recv(16)
                        can_id, size, _ = struct.unpack("=IB3x8s", response)
                        if can_id != 0x50F or size != 6:
                            raise RuntimeError(f"Unexpected handle report on {name}")
                        results[name]["replies"] += 1
                        results[name]["latency_ms"].append((time.monotonic_ns() - sent) / 1e6)
                        time.sleep(0.005)
                run("ip", "link", "set", "dev", name, "down")
                results[name]["cycles"] += 1
            if (cycle + 1) % 5 == 0:
                print(
                    f"Handle query cycles: {cycle + 1}/20 (100 queries per handle per cycle)",
                    flush=True,
                )
    finally:
        errors = []
        for name in names:
            try:
                run("ip", "link", "set", "dev", name, "down")
            except subprocess.CalledProcessError as exc:
                errors.append(str(exc))
        print(
            json.dumps(
                {
                    "started_epoch": started,
                    "finished_epoch": time.time(),
                    "results": results,
                    "before": before,
                    "after": json.loads(run("ip", "-j", "-s", "-d", "link", "show", "type", "can")),
                }
            ),
            flush=True,
        )
        if errors:
            raise RuntimeError(f"CAN cleanup failed; leave services stopped: {errors}")
    check_idle()
    print("PASS: 4000 handle query replies across 40 reopen cycles; no motor commands")


if __name__ == "__main__":
    main()

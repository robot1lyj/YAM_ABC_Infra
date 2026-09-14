#!/usr/bin/env python3
"""Identify YAM USB-CAN adapters and RealSense cameras by unplugging them one at a time.

The wizard is intentionally read-only.  It does not open a CAN socket, bring a
CAN interface up, write udev rules, or construct an i2rt robot.  With all four
USB-CAN adapters connected, it records a baseline and then associates the
adapter that disappears/reappears with the following physical roles:

    right follower -> left follower -> right leader -> left leader

The resulting JSON contains the observed interface name, USB sysfs path, udev
serial/VID/PID/path properties, and a ready-to-review project channel mapping.
The interface name is only an observation; stable names must be assigned later
after reviewing the report and the physical labels.

After the four CAN adapters, the same wizard can identify the three RealSense
cameras in this order:

    right wrist -> top -> left wrist

RealSense probing runs in a separate Python process.  On the RK3588 IPC this
defaults to the compatible Python 3.10 camera environment, so the main YAM
Python 3.12 environment does not need to import the incompatible wheel.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROLES: tuple[dict[str, str], ...] = (
    {"physical": "RIGHT follower", "channel": "can_right"},
    {"physical": "LEFT follower", "channel": "can_left"},
    {"physical": "RIGHT leader", "channel": "can_lead_r"},
    {"physical": "LEFT leader", "channel": "can_lead_l"},
)

CAMERA_ROLES: tuple[dict[str, str], ...] = (
    {"physical": "RIGHT wrist camera", "name": "right", "role": "right"},
    {"physical": "TOP camera", "name": "top", "role": "top"},
    {"physical": "LEFT wrist camera", "name": "left", "role": "left"},
)

CAN_ARPHRD = 280
DEFAULT_TIMEOUT = 90.0
DEFAULT_POLL_INTERVAL = 0.5

REALSENSE_PROBE = r'''
import json
import pyrealsense2 as rs

def info(device, name):
    try:
        return device.get_info(getattr(rs.camera_info, name))
    except Exception:
        return None

devices = []
for device in rs.context().query_devices():
    devices.append({
        "serial": info(device, "serial_number"),
        "name": info(device, "name"),
        "product_line": info(device, "product_line"),
        "usb_type": info(device, "usb_type_descriptor"),
        "firmware": info(device, "firmware_version"),
    })
print(json.dumps({"devices": devices}, ensure_ascii=False))
'''


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a diagnostic command without raising or modifying machine state."""

    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _parse_udev_properties(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "ID_SERIAL",
            "ID_SERIAL_SHORT",
            "ID_VENDOR",
            "ID_VENDOR_ID",
            "ID_MODEL",
            "ID_MODEL_ID",
            "ID_PATH",
            "ID_NET_NAME_PATH",
            "DEVPATH",
        }:
            properties[key] = value
    return properties


def _attribute_serial(text: str) -> str | None:
    match = re.search(r'ATTRS\{serial\}=="([^"]+)"', text)
    return match.group(1) if match else None


def _udev_info(interface: str) -> dict[str, Any]:
    sysfs_path = f"/sys/class/net/{interface}"
    properties_proc = _run(["udevadm", "info", "--query=property", "--path", sysfs_path])
    properties = _parse_udev_properties(properties_proc.stdout)

    attributes_proc = _run(["udevadm", "info", "--attribute-walk", "--path", sysfs_path])
    serial = (
        properties.get("ID_SERIAL_SHORT")
        or properties.get("ID_SERIAL")
        or _attribute_serial(attributes_proc.stdout)
    )
    if serial:
        properties.setdefault("resolved_serial", serial)

    return {
        "properties": properties,
        "udev_error": properties_proc.stderr.strip() or None,
        "attribute_serial": _attribute_serial(attributes_proc.stdout),
    }


def _ip_info(interface: str) -> dict[str, Any]:
    proc = _run(["ip", "-j", "-details", "link", "show", interface])
    if proc.returncode or not proc.stdout.strip():
        return {"error": proc.stderr.strip() or "ip link information unavailable"}
    try:
        item = json.loads(proc.stdout)[0]
    except (ValueError, IndexError, TypeError) as exc:
        return {"error": f"cannot parse ip JSON: {exc}"}
    linkinfo = item.get("linkinfo", {}).get("info_data", {})
    return {
        "state": item.get("operstate"),
        "flags": item.get("flags", []),
        "bitrate": linkinfo.get("bitrate"),
        "can_state": linkinfo.get("state"),
        "restart_ms": linkinfo.get("restart_ms"),
    }


def _sysfs_device(interface: str) -> tuple[str | None, bool]:
    device = Path("/sys/class/net") / interface / "device"
    try:
        resolved = device.resolve(strict=True)
    except OSError:
        return None, False
    parts = set(resolved.parts)
    # Linux sysfs components are usually named usb1/usb2, not literally usb.
    return str(resolved), any(part.startswith("usb") for part in parts)


def _serial_device_links() -> list[dict[str, str]]:
    root = Path("/dev/serial/by-id")
    if not root.is_dir():
        return []
    links: list[dict[str, str]] = []
    for link in sorted(root.iterdir()):
        try:
            target = str(link.resolve(strict=True))
        except OSError:
            target = "<broken>"
        links.append({"link": str(link), "target": target})
    return links


def _lsusb() -> list[str]:
    if shutil.which("lsusb") is None:
        return []
    proc = _run(["lsusb"])
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _can_interfaces() -> list[str]:
    root = Path("/sys/class/net")
    if not root.is_dir():
        return []
    interfaces: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        # bcan* is the RK3588 board CAN family.  External USB-CAN adapters are
        # exposed as can0/can1/... or as the project's stable can_* names.
        if not (entry.name.startswith("can") and entry.is_dir()):
            continue
        if _read_int(entry / "type") == CAN_ARPHRD:
            interfaces.append(entry.name)
    return interfaces


def _interface_record(interface: str) -> dict[str, Any]:
    device_path, is_usb = _sysfs_device(interface)
    udev = _udev_info(interface)
    props = udev["properties"]
    serial = props.get("resolved_serial") or udev.get("attribute_serial")
    return {
        "interface": interface,
        "is_usb_backed": is_usb,
        "sysfs_device": device_path,
        "serial": serial,
        "vid": props.get("ID_VENDOR_ID"),
        "pid": props.get("ID_MODEL_ID"),
        "vendor": props.get("ID_VENDOR"),
        "model": props.get("ID_MODEL"),
        "id_path": props.get("ID_PATH"),
        "udev_net_name_path": props.get("ID_NET_NAME_PATH"),
        "udev": udev,
        "ip": _ip_info(interface),
    }


def snapshot() -> dict[str, Any]:
    """Capture kernel/udev-visible USB-CAN state without touching CAN."""

    return {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "can": [_interface_record(name) for name in _can_interfaces()],
        "serial_devices": _serial_device_links(),
        "lsusb": _lsusb(),
    }


def _camera_python(explicit: Path | None) -> str:
    if explicit is not None:
        return str(explicit)
    for candidate in (
        Path("/home/linux/.venv-yam-camera310/bin/python"),
        Path("/home/linux/.venv-yam-camera310/bin/python3"),
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def camera_snapshot(camera_python: str) -> dict[str, Any]:
    """List RealSense devices through a compatible helper interpreter."""

    proc = _run([camera_python, "-c", REALSENSE_PROBE])
    result: dict[str, Any] = {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "probe_python": camera_python,
        "devices": [],
        "probe_error": None,
    }
    if proc.returncode:
        result["probe_error"] = proc.stderr.strip() or "RealSense probe failed"
        return result
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result["probe_error"] = f"cannot parse RealSense probe JSON: {exc}"
        return result
    devices = payload.get("devices", [])
    result["devices"] = [device for device in devices if device.get("serial")]
    return result


def _camera_by_serial(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["serial"]: item
        for item in state.get("devices", [])
        if item.get("serial")
    }


def _wait_for_camera_removed(
    before: dict[str, Any],
    camera_python: str,
    timeout: float,
    poll_interval: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    before_serials = set(_camera_by_serial(before))

    def changed(state: dict[str, Any]) -> bool:
        removed = before_serials - set(_camera_by_serial(state))
        return len(removed) == 1

    after = _wait_for(
        changed,
        timeout,
        poll_interval,
        "等待检测到恰好一个 RealSense 相机拔出……",
        capture=lambda: camera_snapshot(camera_python),
    )
    removed = sorted(before_serials - set(_camera_by_serial(after))) if after else []
    return after, removed


def _wait_for_camera_reconnected(
    before: dict[str, Any],
    removed_serial: str,
    camera_python: str,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any] | None:
    before_serials = set(_camera_by_serial(before))

    def returned(state: dict[str, Any]) -> bool:
        current_serials = set(_camera_by_serial(state))
        return current_serials == before_serials and removed_serial in current_serials

    state = _wait_for(
        returned,
        timeout,
        poll_interval,
        "等待该 RealSense 相机重新插回并被识别……",
        capture=lambda: camera_snapshot(camera_python),
    )
    return _camera_by_serial(state).get(removed_serial) if state else None


def _can_by_name(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["interface"]: item for item in state.get("can", [])}


def _usb_can_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in state.get("can", []) if item.get("is_usb_backed")]


def _wait_for(
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    poll_interval: float,
    message: str,
    capture: Callable[[], dict[str, Any]] = snapshot,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    last = capture()
    print(message, end="", flush=True)
    while time.monotonic() < deadline:
        if predicate(last):
            print(" 完成")
            return last
        time.sleep(poll_interval)
        last = capture()
    print(" 超时")
    return None


def _find_removed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    before_names = set(_can_by_name(before))
    after_names = set(_can_by_name(after))
    return sorted(before_names - after_names)


def _find_reconnected(
    before_unplug: dict[str, Any], after_replug: dict[str, Any], removed: str
) -> dict[str, Any] | None:
    """Find the one adapter that returned, even if the kernel reused a name."""

    before_names = set(_can_by_name(before_unplug))
    current = _can_by_name(after_replug)
    candidates = [item for name, item in current.items() if name not in before_names - {removed}]
    if len(candidates) == 1:
        return candidates[0]

    removed_record = _can_by_name(before_unplug).get(removed, {})
    serial = removed_record.get("serial")
    if serial:
        matches = [item for item in current.values() if item.get("serial") == serial]
        if len(matches) == 1:
            return matches[0]
    return None


def _wait_for_removed(
    before: dict[str, Any], timeout: float, poll_interval: float
) -> tuple[dict[str, Any] | None, list[str]]:
    before_names = set(_can_by_name(before))

    def changed(state: dict[str, Any]) -> bool:
        removed = sorted(before_names - set(_can_by_name(state)))
        return len(removed) == 1

    after = _wait_for(changed, timeout, poll_interval, "等待检测到恰好一个 CAN USB 设备拔出……")
    return after, _find_removed(before, after) if after else []


def _wait_for_reconnected(
    before_unplug: dict[str, Any], removed: str, timeout: float, poll_interval: float
) -> dict[str, Any] | None:
    remaining = set(_can_by_name(before_unplug)) - {removed}

    def returned(state: dict[str, Any]) -> bool:
        current = set(_can_by_name(state))
        return len(current - remaining) == 1 and len(current) == len(remaining) + 1

    state = _wait_for(returned, timeout, poll_interval, "等待该设备重新插回并被内核识别……")
    return _find_reconnected(before_unplug, state, removed) if state else None


def _role_mapping(steps: list[dict[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for step in steps:
        role = step["role"]
        returned = step.get("after_replug") or step.get("before_unplugged", {})
        mapping[role["channel"]] = {
            "physical_role": role["physical"],
            "interface_at_replug": returned.get("interface"),
            "serial": returned.get("serial"),
            "vid": returned.get("vid"),
            "pid": returned.get("pid"),
            "vendor": returned.get("vendor"),
            "model": returned.get("model"),
            "sysfs_device": returned.get("sysfs_device"),
            "id_path": returned.get("id_path"),
        }
    return mapping


def _camera_mapping(steps: list[dict[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for step in steps:
        role = step["role"]
        returned = step["after_replug"]
        mapping[role["name"]] = {
            "physical_role": role["physical"],
            "camera_role": role["role"],
            "serial": returned.get("serial"),
            "name": returned.get("name"),
            "product_line": returned.get("product_line"),
            "usb_type": returned.get("usb_type"),
            "firmware": returned.get("firmware"),
        }
    return mapping


def _print_record(record: dict[str, Any]) -> None:
    print(
        "  interface={interface} serial={serial} vid:pid={vid}:{pid} "
        "usb={is_usb_backed}".format(
            interface=record.get("interface", "?"),
            serial=record.get("serial") or "UNKNOWN",
            vid=record.get("vid") or "????",
            pid=record.get("pid") or "????",
            is_usb_backed=record.get("is_usb_backed"),
        )
    )
    print(f"  sysfs={record.get('sysfs_device') or 'UNKNOWN'}")


def _print_camera_record(record: dict[str, Any]) -> None:
    print(
        "  serial={serial} name={name} product_line={product_line} usb={usb_type}".format(
            serial=record.get("serial") or "UNKNOWN",
            name=record.get("name") or "UNKNOWN",
            product_line=record.get("product_line") or "UNKNOWN",
            usb_type=record.get("usb_type") or "UNKNOWN",
        )
    )


def _run_arm_wizard(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    print("YAM USB-CAN 物理角色识别（只读模式）")
    print("不会启动 CAN、不会发 CAN 帧、不会写 udev 规则、不会构造机械臂。")
    print("识别顺序：RIGHT follower -> LEFT follower -> RIGHT leader -> LEFT leader")
    print("请保持机械臂断电或急停；只操作 USB-CAN 数据线。\n")

    baseline = snapshot()
    devices = _usb_can_records(baseline)
    print(f"当前 CAN 接口：{len(baseline['can'])} 个；USB-backed：{len(devices)} 个")
    for record in baseline["can"]:
        _print_record(record)
    if len(devices) != len(ROLES):
        print(
            f"错误：需要 {len(ROLES)} 个 USB-backed CAN 接口，实际检测到 {len(devices)} 个。"
        )
        print("请先确认四个 USB-CAN 都插好；不要把 bcan0~bcan3 当作 USB-CAN。")
        return None

    steps: list[dict[str, Any]] = []
    for index, role in enumerate(ROLES, start=1):
        before = snapshot()
        print(f"\n[{index}/{len(ROLES)}] {role['physical']} -> 配置通道 {role['channel']}")
        input("请拔出该机械臂对应的 USB-CAN 数据线，然后按 Enter…… ")
        after_unplug, removed = _wait_for_removed(before, args.timeout, args.poll_interval)
        if after_unplug is None or len(removed) != 1:
            print("无法确认只有一个 CAN 接口消失，已中止；没有写入任何系统配置。")
            return None
        removed_record = _can_by_name(before)[removed[0]]
        print(f"识别到拔出的接口：{removed[0]}")
        _print_record(removed_record)

        input("请把刚才的 USB-CAN 插回原位置，然后按 Enter…… ")
        reconnected = _wait_for_reconnected(
            before, removed[0], args.timeout, args.poll_interval
        )
        if reconnected is None:
            print("无法唯一确认重新插回的设备，已中止；没有写入任何系统配置。")
            return None
        print("重新插回的设备：")
        _print_record(reconnected)
        steps.append(
            {
                "index": index,
                "role": role,
                "before_unplugged": removed_record,
                "after_replug": reconnected,
                "removed_interface": removed[0],
            }
        )
    return baseline, steps


def _run_camera_wizard(
    args: argparse.Namespace, camera_python: str
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    print("\nRealSense 相机物理角色识别（只读模式）")
    print("不会打开采集流、不会写 cameras.yaml；只读取 RealSense 序列号。")
    print("识别顺序：RIGHT wrist -> TOP -> LEFT wrist")
    print(f"相机探测解释器：{camera_python}\n")

    baseline = camera_snapshot(camera_python)
    if baseline.get("probe_error"):
        print(f"RealSense 探测失败：{baseline['probe_error']}")
        print("请确认 pyrealsense2 已安装，或通过 --camera-python 指定兼容的 Python。")
        return None
    devices = _camera_by_serial(baseline)
    print(f"当前 RealSense 相机：{len(devices)} 个")
    for record in devices.values():
        _print_camera_record(record)
    if len(devices) != len(CAMERA_ROLES):
        print(f"错误：需要 {len(CAMERA_ROLES)} 个有序列号的 RealSense，实际检测到 {len(devices)} 个。")
        return None

    steps: list[dict[str, Any]] = []
    for index, role in enumerate(CAMERA_ROLES, start=1):
        before = camera_snapshot(camera_python)
        print(f"\n[{index}/{len(CAMERA_ROLES)}] {role['physical']} -> cameras.yaml name={role['name']}")
        input("请拔出该位置的相机 USB 线，然后按 Enter…… ")
        after_unplug, removed = _wait_for_camera_removed(
            before, camera_python, args.timeout, args.poll_interval
        )
        if after_unplug is None or len(removed) != 1:
            print("无法确认只有一个 RealSense 消失，已中止；没有写入任何配置。")
            return None
        removed_record = _camera_by_serial(before)[removed[0]]
        print(f"识别到拔出的相机序列号：{removed[0]}")
        _print_camera_record(removed_record)

        input("请把刚才的相机 USB 线插回原位置，然后按 Enter…… ")
        reconnected = _wait_for_camera_reconnected(
            before, removed[0], camera_python, args.timeout, args.poll_interval
        )
        if reconnected is None:
            print("无法确认相机重新插回，已中止；没有写入任何配置。")
            return None
        print("重新插回的相机：")
        _print_camera_record(reconnected)
        steps.append(
            {
                "index": index,
                "role": role,
                "before_unplugged": removed_record,
                "after_replug": reconnected,
                "removed_serial": removed[0],
            }
        )
    return baseline, steps


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _selftest() -> int:
    props = _parse_udev_properties(
        "ID_SERIAL_SHORT=ABC123\nID_VENDOR_ID=1d50\nID_MODEL_ID=606f\nID_PATH=pci-foo\n"
    )
    assert props["ID_SERIAL_SHORT"] == "ABC123"
    assert props["ID_VENDOR_ID"] == "1d50"
    assert _attribute_serial('ATTRS{serial}=="SERIAL-X"') == "SERIAL-X"
    before = {"can": [{"interface": "can0", "serial": "A"}, {"interface": "can1", "serial": "B"}]}
    after = {"can": [{"interface": "can1", "serial": "B"}, {"interface": "can2", "serial": "A"}]}
    assert _find_removed(before, {"can": [{"interface": "can1", "serial": "B"}]}) == ["can0"]
    assert _find_reconnected(before, after, "can0")["serial"] == "A"  # type: ignore[index]
    print("selftest OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "只读识别 YAM USB-CAN 和 RealSense 相机；默认先识别机械臂，"
            "再识别相机。"
        )
    )
    parser.add_argument(
        "--only",
        choices=("all", "arms", "cameras"),
        default="all",
        help="只做某一类识别；默认 all",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="每次等待拔出/插回的最长秒数，默认 %(default)s",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="轮询设备变化的间隔秒数，默认 %(default)s",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON 报告路径；默认写当前目录 yam_hardware_identity-时间戳.json",
    )
    parser.add_argument(
        "--camera-python",
        type=Path,
        default=None,
        help="RealSense 探测用 Python；默认优先使用 IPC 的 Python 3.10 相机环境",
    )
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.timeout <= 0 or args.poll_interval <= 0:
        parser.error("--timeout 和 --poll-interval 必须为正数")

    arm_result: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
    camera_result: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
    try:
        if args.only in ("all", "arms"):
            arm_result = _run_arm_wizard(args)
            if arm_result is None:
                return 2
        if args.only in ("all", "cameras"):
            camera_result = _run_camera_wizard(args, _camera_python(args.camera_python))
            if camera_result is None:
                return 5
    except (KeyboardInterrupt, EOFError):
        print("\n用户中止；没有写入任何系统配置。")
        return 130

    finished = datetime.now(UTC).isoformat(timespec="seconds")
    report = {
        "schema": "yam-hardware-identity/v1",
        "started_at": (
            arm_result[0]["captured_at"]
            if arm_result is not None
            else camera_result[0]["captured_at"]
        ),
        "finished_at": finished,
        "read_only": True,
        "arm_sequence": [role["physical"] for role in ROLES],
        "camera_sequence": [role["physical"] for role in CAMERA_ROLES],
        "arms": (
            {
                "baseline": arm_result[0],
                "steps": arm_result[1],
                "mapping": _role_mapping(arm_result[1]),
            }
            if arm_result is not None
            else None
        ),
        "cameras": (
            {
                "baseline": camera_result[0],
                "steps": camera_result[1],
                "mapping": _camera_mapping(camera_result[1]),
            }
            if camera_result is not None
            else None
        ),
        "notes": [
            "CAN interface_at_replug is an observation, not a stable name",
            "review physical labels and serials before creating 90-can.rules",
            "camera serials should be copied into configs/cameras.yaml after review",
            "this report does not prove that an arm is powered or CAN communication works",
        ],
    }
    output = args.output or Path(
        f"yam_hardware_identity-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    _write_report(output, report)

    print("\n识别完成。建议机械臂通道映射：")
    if arm_result is not None:
        for channel, item in report["arms"]["mapping"].items():
            print(
                f"  {channel:<11} serial={item['serial'] or 'UNKNOWN':<20} "
                f"observed={item['interface_at_replug']}"
            )
    if camera_result is not None:
        print("相机序列号映射：")
        for name, item in report["cameras"]["mapping"].items():
            print(f"  {name:<6} serial={item['serial'] or 'UNKNOWN'}")
    print(f"\nJSON 报告：{output}")
    print("下一步先人工复核序列号与物理标签，再生成并应用 udev 稳定命名规则、更新 cameras.yaml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

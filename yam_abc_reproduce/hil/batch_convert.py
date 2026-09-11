"""Explicit offline batch conversion; no connection to the control service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .lerobot_export import export_session
from .storage import atomic_json


def discover(root):
    """Select sessions OR standalone episodes, never convert both twice."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("输入必须是原始采集目录")
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "exports")
        if "session.json" in files or "manifest.json" in files:
            dirs.clear()
            yield Path(folder)


def convert(root, output, *, expert_only=False, allow_recovered=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("输出目录与原始目录必须相互独立，避免混入来源数据")
    sources = iter(discover(root))
    first = next(sources, None)
    if first is None:
        raise ValueError("没有发现 session.json 或 manifest.json")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "yam_offline_batch_v1",
        "source_root": str(root),
        "target_format": "LeRobot v3.0",
        "expert_only": expert_only,
        "allow_recovered": allow_recovered,
        "datasets": [],
        "failed": 0,
    }
    import itertools

    for source in itertools.chain((first,), sources):
        relative = source.relative_to(root)
        destination = output / (root.name if relative == Path(".") else relative)
        print(f"转换：{relative} → {destination}", flush=True)
        entry = {"source": str(source), "output": str(destination)}
        try:
            result = export_session(
                source, destination, expert_only=expert_only, allow_recovered=allow_recovered
            )
            entry.update(state="complete" if result["frames"] else "empty", report=result)
        except Exception as exc:
            entry.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            report["failed"] += 1
        report["datasets"].append(entry)
        atomic_json(output / "conversion_report.json", report)
        print(f"结果：{entry['state']}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="独立批量转换 YAM 原始采集到 LeRobot v3.0")
    parser.add_argument("source", type=Path, help="单集、会话或包含多个会话的目录")
    parser.add_argument("--output", type=Path, required=True, help="新的独立输出目录")
    parser.add_argument("--expert-only", action="store_true")
    parser.add_argument("--allow-recovered", action="store_true")
    args = parser.parse_args()
    try:
        result = convert(
            args.source,
            args.output,
            expert_only=args.expert_only,
            allow_recovered=args.allow_recovered,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"无法转换：{exc}\n")
    print(f"完成：{len(result['datasets'])} 个来源，失败 {result['failed']} 个。")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

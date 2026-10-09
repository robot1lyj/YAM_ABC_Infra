"""Task-wide saved-episode inventory and recoverable deletion, off control.

Session manifests are authoritative. Never renumber retained episodes or touch
an active writer's files without going through its command queue.
"""

import json
import re
import tempfile
import threading
import time
from pathlib import Path

from .storage import atomic_json


def inventory(root, task_id):
    root = Path(root)
    items = []
    for session in sorted(root.glob("session_*")):
        if session.is_symlink() or not session.is_dir():
            continue
        manifest = json.loads((session / "session.json").read_text())
        for entry in manifest["episodes"]:
            name = entry["path"]
            if not re.fullmatch(r"episode_\d{6,}", name):
                raise ValueError("数据集清单含无效的集路径，请检查 session.json")
            if entry["outcome"] in ("aborted", "discarded") or entry["steps"] <= 0:
                continue
            episode = session / name
            if episode.is_symlink() or not episode.is_dir():
                raise ValueError("已保存集的目录缺失，请检查数据盘与清单")
            saved_at = (episode / "manifest.json").stat().st_mtime_ns
            items.append({"key": f"{session.name}/{name}", "steps": entry["steps"],
                          "saved_at": saved_at})
    items.sort(key=lambda item: (item["saved_at"], item["key"]))
    return {"task_id": task_id, "count": len(items),
            "frames": sum(item["steps"] for item in items),
            "latest": items[-1] if items else None, "error": None, "loading": False}


def trash_episode(root, task_id, expected_key):
    """Validate the exact newest saved episode, then move it on the same disk.

    Write a recovery record first. On a normal manifest-write failure, restore
    the directory; a process/power loss still leaves both data and old manifest
    recoverable from the trash record. Callers serialize against the writer.
    """
    root = Path(root)
    latest = inventory(root, task_id)["latest"]
    if latest is None or latest["key"] != expected_key:
        raise ValueError("最近保存的集已变化，本次未删除；请刷新后重新确认")
    session_name, episode_name = latest["key"].split("/")
    session = root / session_name
    manifest_path = session / "session.json"
    before = json.loads(manifest_path.read_text())
    after = {**before, "episodes": [e for e in before["episodes"] if e["path"] != episode_name]}
    trash_root = root.parent / ".trash"
    if trash_root.is_symlink():
        raise ValueError("回收目录不能是符号链接，本次未删除")
    trash_root.mkdir(exist_ok=True)
    trash = Path(tempfile.mkdtemp(prefix="episode-", dir=trash_root))
    source, target = session / episode_name, trash / episode_name
    atomic_json(trash / "restore.json", {"task_id": task_id, "source": str(source.resolve()),
                                        "session_manifest": before})
    source.rename(target)
    try:
        atomic_json(manifest_path, after)
    except Exception:
        target.rename(source)
        # atomic_json can also fail on the directory fsync *after* replace.
        # Restore the completion list as well, not only the data directory.
        if json.loads(manifest_path.read_text()) != before:
            atomic_json(manifest_path, before)
        raise
    return {"deleted": latest, "trash": str(trash), "episodes": after["episodes"]}


class TaskDatasetCache:
    """One lazy inventory reader; /status never waits on storage."""

    def __init__(self, root_for):
        self.root_for = root_for
        self.lock = threading.Lock()
        self.reader = None
        self.task_id = None
        self.value = None
        self.retry_at = 0.0
        self.generation = 0

    def snapshot(self, task_id):
        with self.lock:
            if task_id != self.task_id:
                self.task_id, self.value, self.retry_at = task_id, None, 0.0
                self.generation += 1
            if task_id and time.monotonic() >= self.retry_at and (
                self.reader is None or not self.reader.is_alive()
            ):
                self.retry_at = time.monotonic() + 1
                self.reader = threading.Thread(target=self._read,
                    args=(task_id, self.generation), daemon=True, name="task-dataset-inventory")
                try:
                    self.reader.start()
                except RuntimeError:
                    self.reader = None
                    self.value = {"task_id": task_id, "count": None, "frames": None,
                                  "latest": None, "loading": False,
                                  "error": "暂无法启动数据统计，请稍后刷新"}
            return dict(self.value) if self.value else {
                "task_id": task_id, "count": None, "frames": None, "latest": None,
                "loading": bool(task_id), "error": None,
            }

    def publish(self, value):
        with self.lock:
            self.generation += 1  # A scan begun before deletion cannot restore an old count.
            self.task_id, self.value = value["task_id"], value
            self.retry_at = time.monotonic() + 1

    def _read(self, task_id, generation):
        try:
            value = inventory(self.root_for(task_id), task_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            value = {"task_id": task_id, "count": None, "frames": None, "latest": None,
                     "loading": False, "error": "任务数据暂不可读：" + str(exc)}
        with self.lock:
            if task_id == self.task_id and generation == self.generation:
                self.value = value

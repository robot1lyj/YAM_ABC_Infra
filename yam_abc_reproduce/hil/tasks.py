"""Local task catalog. Stable IDs isolate datasets; names never become paths."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path


class Tasks:
    def __init__(self, root):
        self.path = Path(root) / "tasks.json"
        self.items = []
        self.error = None
        if self.path.exists():
            try:
                items = json.loads(self.path.read_text())
                if not isinstance(items, list):
                    raise ValueError("任务目录格式错误")
                for task in items:
                    if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                        raise ValueError("任务记录格式错误")
                    if str(uuid.UUID(task["id"])) != task["id"]:
                        raise ValueError("任务ID格式错误")
                    self.validate(task["name"], task["instruction"])
                    if "task" in task:
                        self.validate_task(task["task"])
                if len({t["id"] for t in items}) != len(items):
                    raise ValueError("任务ID重复")
                self.items = items
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = "任务目录读取失败，请修复后重启：" + str(exc)

    @staticmethod
    def validate(name, instruction):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("任务名称须为1–60字")
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 300:
            raise ValueError("采集目标须为1–300字")

    @staticmethod
    def validate_task(task):
        if (
            not isinstance(task, str)
            or not 1 <= len(task.strip()) <= 300
            or not task.isascii()
            or not any(c.isalpha() for c in task)
            or any(ord(c) < 32 for c in task)
        ):
            raise ValueError("英文 task 须为1–300个英文字符，填写英文任务指令")

    def create(self, name, instruction, task):
        return self._save(name, instruction, task)

    def update(self, task_id, name, instruction, task):
        return self._save(name, instruction, task, existing=self.get(task_id))

    def _save(self, name, instruction, task, existing=None):
        if self.error:
            raise ValueError(self.error)
        self.validate(name, instruction)
        self.validate_task(task)
        if any(
            t["name"].casefold() == name.strip().casefold()
            and (existing is None or t["id"] != existing["id"])
            for t in self.items
        ):
            raise ValueError("已有同名任务，请选择已有任务或使用不同名称")
        item = {
            **(existing or {"id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat()}),
            "name": name.strip(),
            "instruction": instruction.strip(),
            "task": task.strip(),
        }
        items = [item if t["id"] == item["id"] else t for t in self.items]
        if existing is None:
            items.append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        temp.replace(self.path)
        self.items = items
        return dict(item)

    def get(self, task_id):
        for task in self.items:
            if task["id"] == task_id:
                return dict(task)
        raise ValueError("任务不存在，请重新选择")

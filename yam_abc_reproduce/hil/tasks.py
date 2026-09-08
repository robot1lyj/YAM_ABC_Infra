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

    def create(self, name, instruction):
        if self.error:
            raise ValueError(self.error)
        self.validate(name, instruction)
        if any(t["name"].casefold() == name.strip().casefold() for t in self.items):
            raise ValueError("已有同名任务，请选择已有任务或使用不同名称")
        task = {
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "instruction": instruction.strip(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        items = [*self.items, task]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        temp.replace(self.path)
        self.items = items
        return dict(task)

    def get(self, task_id):
        for task in self.items:
            if task["id"] == task_id:
                return dict(task)
        raise ValueError("任务不存在，请重新选择")

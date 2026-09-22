"""Durable offline jobs with one replaceable worker and bounded admission."""

from __future__ import annotations

import json
import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from ..hil.storage import atomic_json
from .catalog import Catalog
from .operations import check_episode, export_selected
from .portable import create_package, restore_package
from .reading import check_lerobot


def execute_job(root, identity, kind, request, payload):
    catalog = Catalog(root, recover_jobs=False)

    def save(state):
        with catalog.db() as db:
            db.execute(
                "UPDATE jobs SET state=?,payload=? WHERE id=?",
                (state, json.dumps(payload), identity),
            )

    previous = [0.0]

    def progress(count, message):
        payload.update(progress=count, message=message)
        if time.monotonic() - previous[0] >= 0.5:
            save("running")
            previous[0] = time.monotonic()

    try:
        save("running")
        ids = request.get("ids", [])
        if kind == "import":
            result = catalog.scan(request["path"], progress)
        elif kind == "package":
            result = create_package(catalog, ids, request["path"], progress)
        elif kind == "restore":
            result = restore_package(catalog, request["path"], request["destination"], progress)
        elif kind == "export":
            result = export_selected(
                [catalog.get(i) for i in ids],
                request["path"],
                expert_only=request["expert_only"],
                progress=progress,
            )
        elif kind == "check":
            reports = catalog.root / "jobs" / f"{identity}.episodes.jsonl"
            reports.parent.mkdir(exist_ok=True)
            good, bad = 0, 0
            fingerprints = {}
            duplicates = []
            with reports.open("w") as output:
                for index, episode_id in enumerate(ids):
                    entry = catalog.get(episode_id)
                    try:
                        report = (
                            check_lerobot(entry, request["deep"])
                            if entry["format"] == "lerobot_v3"
                            else check_episode(entry["path"], request["deep"])
                        )
                    except Exception as exc:
                        report = dict(ok=False, issues=[str(exc)], checked_at=time.time())
                    with catalog.db() as db:
                        db.execute(
                            "UPDATE episodes SET report=?,check_ok=? WHERE id=?",
                            (json.dumps(report), int(report["ok"]), episode_id),
                        )
                    key = report.get("content_hash")
                    if key:
                        if key in fingerprints:
                            duplicates.append([fingerprints[key], episode_id])
                        else:
                            fingerprints[key] = episode_id
                    output.write(json.dumps({"id": episode_id, **report}) + "\n")
                    good += int(report["ok"])
                    bad += int(not report["ok"])
                    progress(index + 1, f"{index + 1}/{len(ids)} 集")
            result = dict(
                checked=len(ids),
                passed=good,
                issues=bad,
                report_file=reports.name,
                duplicate_pairs=duplicates,
            )
        else:
            raise ValueError("未知任务")
        report_path = catalog.root / "jobs" / f"{identity}.json"
        report_path.parent.mkdir(exist_ok=True)
        atomic_json(report_path, result)
        payload.update(report_available=True, message="已完成")
        save("complete")
    except Exception as exc:
        payload.update(error=str(exc), message="执行失败")
        save("failed")


class Jobs:
    def __init__(self, catalog):
        self.catalog = catalog
        self.pool = self._new_pool()
        self._pool_failed = threading.Event()
        self.slots = threading.BoundedSemaphore(4)
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _new_pool():
        return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))

    def save(self, identity, state, payload):
        with self.catalog.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (identity, state, json.dumps(payload))
            )

    def _failed(self, identity, error, message):
        # Preserve the worker's last durable progress and any completed result.
        with self.catalog.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None or row["state"] not in ("queued", "running"):
                return
            payload = json.loads(row["payload"])
            payload.update(error=str(error), message=message)
            db.execute(
                "UPDATE jobs SET state='failed',payload=? WHERE id=? AND state IN ('queued','running')",
                (json.dumps(payload), identity),
            )

    def submit(self, kind, request):
        names = {
            "import": "导入索引",
            "check": "数据检查",
            "export": "转换 LeRobot v3.0",
            "package": "打包迁移",
            "restore": "恢复迁移包",
        }
        if kind not in names:
            raise ValueError("未知任务")
        if not self.slots.acquire(blocking=False):
            raise ValueError("后台已有4项任务，请等待完成")
        identity = uuid.uuid4().hex
        payload = dict(kind=names[kind], created_at=time.time(), progress=0, message="等待执行")
        queued, failed = False, None
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("数据集服务正在关闭")
                if self._pool_failed.is_set():
                    # Reap the failed generation from the submitting thread, not
                    # from an executor callback, before allocating its replacement.
                    self.pool.shutdown(wait=True)
                    self.pool = self._new_pool()
                    self._pool_failed = threading.Event()
                pool = self.pool
                failed = self._pool_failed
                self.save(identity, "queued", payload)
                queued = True
                future = pool.submit(
                    execute_job, str(self.catalog.root), identity, kind, request, payload
                )
        except Exception as exc:
            try:
                if isinstance(exc, BrokenProcessPool) and failed is not None:
                    failed.set()
                if queued:
                    self._failed(identity, exc, "任务未提交，请排除错误后重新提交")
            finally:
                self.slots.release()
            raise ValueError(f"后台任务未提交：{exc}；请排除错误后重新提交") from exc

        def finished(task):
            try:
                task.result()
            except Exception as exc:
                if isinstance(exc, BrokenProcessPool):
                    # The executor terminates its own broken workers. Callbacks
                    # run under its shutdown lock: only signal this generation;
                    # taking our submission lock or calling shutdown can deadlock.
                    failed.set()
                self._failed(
                    identity,
                    exc,
                    "工作进程异常；未自动重跑，请核对已生成文件后重新提交",
                )
            finally:
                self.slots.release()

        future.add_done_callback(finished)
        return identity

    def list(self):
        with self.catalog.db() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30").fetchall()
        return [dict(id=r["id"], state=r["state"], **json.loads(r["payload"])) for r in rows]

    def close(self):
        with self._lock:
            self._closed = True
            pool, self.pool = self.pool, None
        if pool is not None:
            pool.shutdown(wait=True)

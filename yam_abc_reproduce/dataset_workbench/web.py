"""Local-only offline workbench; bounded single-worker background queue."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..hil.storage import atomic_json
from .catalog import Catalog
from .formats import cameras
from .operations import check_episode, export_selected
from .portable import create_package, restore_package
from .reading import check_lerobot, preview_entry


class Action(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    action: str = ""
    value: str = ""
    path: str = ""
    name: str = ""
    deep: bool = False
    expert_only: bool = False
    destination: str = ""


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
        self.pool = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        )
        self.slots = threading.BoundedSemaphore(4)

    def save(self, identity, state, payload):
        with self.catalog.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (identity, state, json.dumps(payload))
            )

    def submit(self, kind, request):
        if not self.slots.acquire(blocking=False):
            raise ValueError("后台已有4项任务，请等待完成")
        identity = uuid.uuid4().hex
        names = {
            "import": "导入索引",
            "check": "数据检查",
            "export": "转换 LeRobot v3.0",
            "package": "打包迁移",
            "restore": "恢复迁移包",
        }
        payload = dict(kind=names[kind], created_at=time.time(), progress=0, message="等待执行")
        self.save(identity, "queued", payload)
        future = self.pool.submit(
            execute_job, str(self.catalog.root), identity, kind, request, payload
        )

        def finished(task):
            try:
                task.result()
            except Exception as exc:
                payload.update(error=str(exc), message="工作进程异常")
                self.save(identity, "failed", payload)
            finally:
                self.slots.release()

        future.add_done_callback(finished)
        return identity

    def list(self):
        with self.catalog.db() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30").fetchall()
        return [dict(id=r["id"], state=r["state"], **json.loads(r["payload"])) for r in rows]


def create_app(root):
    catalog = Catalog(root)
    jobs = Jobs(catalog)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.pool.shutdown(wait=True)

    app = FastAPI(title="悟演智能 · 数据集工作台", lifespan=lifespan)
    app.state.catalog, app.state.jobs = catalog, jobs
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if request.headers.get("x-yam-control") != "1" or (
                origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
            ):
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    static = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/api/episodes")
    def episodes(
        view: str = "all",
        query: str = "",
        collection: str = "",
        offset: int = 0,
        cursor: str = "",
        format: str = "",
    ):
        return catalog.listing(
            view=view,
            query=query,
            collection=collection,
            offset=offset,
            cursor=cursor,
            format=format,
        )

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{identity}/report")
    def job_report(identity: str):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise HTTPException(404, "任务不存在")
        path = catalog.root / "jobs" / f"{identity}.json"
        if not path.is_file():
            raise HTTPException(404, "任务尚无完整报告")
        return FileResponse(path, media_type="application/json")

    @app.get("/api/jobs/{identity}/episodes")
    def episode_report(identity: str):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise HTTPException(404, "任务不存在")
        path = catalog.root / "jobs" / f"{identity}.episodes.jsonl"
        if not path.is_file():
            raise HTTPException(404, "没有逐集报告")
        return FileResponse(path, media_type="application/x-ndjson", filename=path.name)

    @app.post("/api/import")
    def import_data(body: Action):
        if not body.path.strip():
            raise ValueError("请填写原始数据目录")
        return {"id": jobs.submit("import", body.model_dump())}

    @app.post("/api/curate")
    def curate(body: Action):
        catalog.curate(body.ids, body.action, body.value)
        return {"ok": True}

    @app.post("/api/collections")
    def collection(body: Action):
        return {"id": catalog.collection(body.name, body.ids)}

    @app.post("/api/check")
    def check(body: Action):
        if not body.ids:
            raise ValueError("请选择需要检查的集")
        return {"id": jobs.submit("check", body.model_dump())}

    @app.post("/api/export")
    def export(body: Action):
        if not body.ids or not body.path.strip():
            raise ValueError("请选择集并填写输出目录")
        if "lerobot_v3" in catalog.selected_formats(body.ids):
            raise ValueError("转换只接受原始采集包，不能混选LeRobot；LeRobot支持浏览、检查和迁移")
        return {"id": jobs.submit("export", body.model_dump())}

    @app.post("/api/package")
    def package(body: Action):
        if not body.ids or not body.path.strip():
            raise ValueError("请选择集和.tar输出路径")
        return {"id": jobs.submit("package", body.model_dump())}

    @app.post("/api/restore")
    def restore(body: Action):
        if not body.path.strip() or not body.destination.strip():
            raise ValueError("请填写迁移包和新的恢复目录")
        return {"id": jobs.submit("restore", body.model_dump())}

    @app.get("/api/episodes/{identity}/preview/{role}")
    def image(identity: str, role: str, frame: int = 0):
        try:
            return Response(
                preview_entry(catalog.get(identity), role, frame), media_type="image/jpeg"
            )
        except (OSError, ValueError, StopIteration) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/episodes/{identity}")
    def detail(identity: str):
        entry = catalog.get(identity)
        entry["cameras"] = cameras(entry["metadata"])
        return entry

    return app


def main():
    parser = argparse.ArgumentParser(description="悟演智能离线数据集工作台（不连接机械臂）")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--catalog", type=Path, default=Path("data/dataset_workbench"))
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(args.catalog), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

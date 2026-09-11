"""Local-only offline workbench; bounded single-worker background queue."""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..hil.storage import atomic_json
from .catalog import Catalog
from .operations import check_episode, export_selected, preview


class Action(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    action: str = ""
    value: str = ""
    path: str = ""
    name: str = ""
    deep: bool = False
    expert_only: bool = False


class Jobs:
    def __init__(self, catalog):
        self.catalog = catalog
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dataset-offline")
        self.slots = threading.BoundedSemaphore(4)

    def save(self, identity, state, payload):
        with self.catalog.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (identity, state, json.dumps(payload))
            )

    def submit(self, kind, operation):
        if not self.slots.acquire(blocking=False):
            raise ValueError("后台已有 4 项任务，请等待完成")
        identity = uuid.uuid4().hex
        payload = {"kind": kind, "created_at": time.time(), "progress": 0, "message": "等待执行"}
        self.save(identity, "queued", payload)

        def run():
            def progress(count, message):
                payload.update(progress=count, message=message)
                self.save(identity, "running", payload)

            try:
                progress(0, "正在执行")
                result = operation(progress)
                report_path = self.catalog.root / "jobs" / f"{identity}.json"
                report_path.parent.mkdir(exist_ok=True)
                atomic_json(report_path, result)
                payload["report_available"] = True
                payload["message"] = "已完成"
                self.save(identity, "complete", payload)
            except Exception as exc:
                payload.update(error=str(exc), message="执行失败")
                self.save(identity, "failed", payload)
            finally:
                self.slots.release()

        self.pool.submit(run)
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
    def episodes(view: str = "all", query: str = "", collection: str = "", offset: int = 0):
        return catalog.listing(view=view, query=query, collection=collection, offset=offset)

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

    @app.post("/api/import")
    def import_data(body: Action):
        if not body.path.strip():
            raise ValueError("请填写原始数据目录")
        return {"id": jobs.submit("导入索引", lambda p: catalog.scan(body.path, p))}

    @app.post("/api/curate")
    def curate(body: Action):
        catalog.curate(body.ids, body.action, body.value)
        return {"ok": True}

    @app.post("/api/collections")
    def collection(body: Action):
        return {"id": catalog.collection(body.name, body.ids)}

    @app.post("/api/check")
    def check(body: Action):
        entries = [catalog.get(i) for i in body.ids]
        if not entries:
            raise ValueError("请选择需要检查的集")

        def operation(progress):
            reports, duplicates = [], {}
            for index, entry in enumerate(entries):
                try:
                    report = check_episode(entry["path"], body.deep)
                except Exception as exc:
                    report = {"ok": False, "issues": [str(exc)], "checked_at": time.time()}
                if report.get("content_hash"):
                    duplicates.setdefault(report["content_hash"], []).append(entry["id"])
                with catalog.db() as db:
                    db.execute(
                        "UPDATE episodes SET report=? WHERE id=?", (json.dumps(report), entry["id"])
                    )
                reports.append({"id": entry["id"], **report})
                progress(index + 1, f"{index + 1}/{len(entries)} 集")
            return {
                "reports": reports,
                "duplicate_groups": [v for v in duplicates.values() if len(v) > 1],
            }

        return {"id": jobs.submit("深度检查" if body.deep else "快速检查", operation)}

    @app.post("/api/export")
    def export(body: Action):
        entries = [catalog.get(i) for i in dict.fromkeys(body.ids)]
        if not body.path.strip() or not entries:
            raise ValueError("请选择集并填写新的输出目录")
        return {
            "id": jobs.submit(
                "合并转换 LeRobot v3.0",
                lambda p: export_selected(
                    entries, body.path, expert_only=body.expert_only, progress=p
                ),
            )
        }

    @app.get("/api/episodes/{identity}/preview/{role}")
    def image(identity: str, role: str, frame: int = 0):
        try:
            return Response(
                preview(catalog.get(identity)["path"], role, frame), media_type="image/jpeg"
            )
        except (OSError, ValueError, StopIteration) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/episodes/{identity}")
    def detail(identity: str):
        return catalog.get(identity)

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

"""Local-only offline workbench; bounded single-worker background queue."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import Catalog
from .formats import cameras
from .jobs import Jobs
from .reading import preview_entry


class Action(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    action: str = ""
    value: str = ""
    path: str = ""
    name: str = ""
    deep: bool = False
    expert_only: bool = False
    destination: str = ""


def create_app(root):
    catalog = Catalog(root)
    jobs = Jobs(catalog)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

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

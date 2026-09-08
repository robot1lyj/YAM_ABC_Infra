"""Same-origin local operator API; device writes belong to the runtime owner."""

import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

STATIC = Path(__file__).with_name("static")


class Connect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ready: bool = False
    url: str | None = Field(default=None, max_length=500)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    instruction: str = Field(min_length=1, max_length=300)


class Disconnect(BaseModel):
    supported: bool = False


class JogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arm: str
    joint: int = Field(strict=True, ge=0, le=6)
    delta: float = Field(allow_inf_nan=False)


def create_app(runtime):
    app = FastAPI(title="悟演智能采集工作台")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.middleware("http")
    async def local_control(request: Request, call_next):
        # Cross-origin forms cannot issue motor commands; no permissive CORS.
        if request.method == "POST":
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if request.headers.get("x-yam-control") != "1" or (origin and origin != expected):
                return JSONResponse({"detail": "拒绝跨站控制请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, queue.Full) as exc:
            raise HTTPException(409, str(exc) or "指令队列忙，请稍后重试") from exc

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/status")
    def status():
        return runtime.status

    @app.post("/event/{event}")
    def event(event: str):
        invoke(runtime.event, event)
        return {"queued": event}

    @app.post("/heartbeat")
    def heartbeat():
        if hasattr(runtime, "heartbeat"):
            runtime.heartbeat()
        return {"ok": True}

    @app.post("/tasks")
    def create_task(body: TaskCreate):
        return invoke(runtime.create_task, body.name, body.instruction)

    @app.post("/tasks/{task_id}/select")
    def select_task(task_id: str):
        return invoke(runtime.select_task, task_id)

    @app.post("/cameras/connect")
    def connect_cameras():
        invoke(runtime.connect_cameras)
        return {"queued": "cameras_connect"}

    @app.post("/cameras/disconnect")
    def disconnect_cameras():
        invoke(runtime.disconnect_cameras)
        return {"queued": "cameras_disconnect"}

    @app.post("/connect")
    def connect(body: Connect):
        if not hasattr(runtime, "connect"):
            raise HTTPException(409, "此演示会话已连接")
        invoke(runtime.connect, **body.model_dump())
        return {"queued": "connect"}

    @app.post("/disconnect")
    def disconnect(body: Disconnect):
        if not hasattr(runtime, "disconnect"):
            raise HTTPException(409, "请在启动终端结束此演示")
        invoke(runtime.disconnect, **body.model_dump())
        return {"queued": "disconnect"}

    @app.post("/jog")
    def jog(body: JogRequest):
        invoke(runtime.request_jog, **body.model_dump())
        return {"queued": "jog"}

    @app.get("/camera/{role}.jpg")
    def camera(role: str):
        data = runtime.preview(role) if hasattr(runtime, "preview") else None
        if data is None:
            raise HTTPException(404, "相机预览尚未就绪或已过期")
        return Response(data, media_type="image/jpeg")

    return app


def start_dashboard(runtime, port):
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(create_app(runtime), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    print(f"Workstation: http://127.0.0.1:{port}", flush=True)
    return server

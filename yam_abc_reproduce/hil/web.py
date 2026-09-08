"""Local-only controls for the unified workstation runtime."""

import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>YAM 工作站</title>
<style>body{font:18px system-ui;max-width:900px;margin:48px auto;padding:0 20px;background:#101820;color:#e9f0f4}
button{font:inherit;padding:12px 18px;margin:6px;border:0;border-radius:8px;cursor:pointer}pre{white-space:pre-wrap;background:#20303c;padding:20px}
</style><h1>YAM 双臂工作站</h1><p>模式切换先保持；点击开始后执行。HIL用键盘i冻结介入，人工阶段用手柄①交还模型。</p>
<div><button onclick="send('mode:teleop')">遥操作</button><button onclick="send('mode:inference')">推理</button><button onclick="send('mode:hil')">DAgger / HIL</button><button onclick="send('mode:collect')">数据采集</button></div>
<div><button onclick="send('record')">开始 / 结束一段录制</button><button onclick="send('discard')">放弃当前集</button></div>
<div><button onclick="send('start')">开始 / 恢复</button><button onclick="send('takeover')">键盘 i：介入（或点此）</button><button onclick="send('resume_policy')">交还模型（同手柄①）</button><button onclick="send('hold')" style="background:#ffbf69">暂停运动（空格）</button></div>
<p>HIL：键盘 i 冻结并介入，手柄①交还模型，②无功能。采集：①开始/结束，②放弃当前集。遥操作/推理：手柄按钮无功能。退出程序会结束电机控制，请先支撑机械臂。</p>
<div><button onclick="send('success')">标记成功</button><button onclick="send('failure')">标记失败</button></div><button onclick="send('quit')">结束会话（先支撑机械臂）</button><p id="error"></p><pre id="status">正在读取状态…</pre><script>
async function send(event){const r=await fetch('/event/'+encodeURIComponent(event),{method:'POST'});document.getElementById('error').textContent=r.ok?'':await r.text()}
async function poll(){try{const s=await(await fetch('/status')).json();document.getElementById('status').textContent=JSON.stringify(s,null,2)}catch(e){document.getElementById('error').textContent='工作站连接已断开'}}setInterval(poll,300);poll();
</script></html>"""


def create_app(runtime):
    app = FastAPI(title="YAM 四模式工作站")

    @app.get("/", response_class=HTMLResponse)
    def home():
        return PAGE

    @app.get("/status")
    def status():
        return runtime.status

    @app.post("/event/{event}")
    def event(event: str):
        try:
            runtime.event(event)
        except (ValueError, queue.Full) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"queued": event}

    return app


def start_dashboard(runtime, port):
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(create_app(runtime), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    print(f"Workstation: http://127.0.0.1:{port}", flush=True)
    return server

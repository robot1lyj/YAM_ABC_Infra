"""Thin local client for the persistent workstation device owner.

The public Web/API process never imports robot or camera drivers.  Every command
crosses a Unix-domain HTTP socket into the single process that owns Runtime,
CAN, cameras, recording and policy state.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str | Path, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DeviceClient:
    """Workbench-compatible proxy used by the public FastAPI process."""

    def __init__(self, socket_path: str | Path, args=None):
        self.socket_path = Path(socket_path)
        self.args = args or SimpleNamespace(web_host="127.0.0.1", web_allowed_host=[])

    def _request(self, method: str, path: str, body=None, *, raw=False):
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Host": "localhost", "X-YAM-Control": "1"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        connection = _UnixHTTPConnection(self.socket_path)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ValueError("设备控制进程不可用，请检查 yam-device.service") from exc
        finally:
            connection.close()
        if response.status >= 400:
            try:
                detail = json.loads(data).get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise ValueError(detail or f"设备控制请求失败（HTTP {response.status}）")
        if raw:
            return data
        return None if not data else json.loads(data)

    @property
    def status(self):
        try:
            return self._request("GET", "/status")
        except ValueError as exc:
            return {
                "connection": "unavailable",
                "camera_connection": "unavailable",
                "phase": "hold",
                "running": False,
                "error": str(exc),
                "log": [],
            }

    def event(self, event):
        return self._request("POST", f"/event/{quote(event, safe=':')}")

    def configure_policy(self, *, fusion, rtc_delay_steps=None):
        body = {"fusion": fusion}
        if rtc_delay_steps is not None:
            body["rtc_delay_steps"] = rtc_delay_steps
        return self._request("POST", "/policy/settings", body)

    def restart_policy(self):
        return self._request("POST", "/policy/restart")

    def reload_interaction(self):
        return self._request("POST", "/control/reload")

    def change_policy_source(self, *, url):
        return self._request("POST", "/policy/source", {"url": url})

    def restart_planner(self):
        return self._request("POST", "/policy/planner/restart")

    def heartbeat(self):
        return self._request("POST", "/heartbeat")

    def create_task(self, **body):
        return self._request("POST", "/tasks", body)

    def update_task(self, task_id, **body):
        return self._request("POST", f"/tasks/{quote(task_id, safe='')}/update", body)

    def select_task(self, task_id):
        return self._request("POST", f"/tasks/{quote(task_id, safe='')}/select")

    def connect_cameras(self):
        return self._request("POST", "/cameras/connect")

    def disconnect_cameras(self):
        return self._request("POST", "/cameras/disconnect")

    def connect(self, **body):
        return self._request("POST", "/connect", body)

    def initialization_preflight(self):
        return self._request("POST", "/initialize/preflight")

    def complete_initialization(self, **body):
        return self._request("POST", "/initialize/complete", body)

    def exit_initialization(self):
        return self._request("POST", "/initialize/exit")

    def disconnect(self, **body):
        return self._request("POST", "/disconnect", body)

    def request_jog(self, arm, joint, delta):
        return self._request(
            "POST", "/jog", {"arm": arm, "joint": joint, "delta": delta}
        )

    def preview(self, role):
        try:
            return self._request("GET", f"/camera/{quote(role, safe='')}.jpg", raw=True)
        except ValueError:
            return None

    def hold_on_attach(self):
        """A new Web process must never silently inherit moving policy state."""
        try:
            self.event("hold")
        except ValueError:
            # Normal when the persistent owner has no connected arm session yet.
            pass

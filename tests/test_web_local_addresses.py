"""DHCP-safe public listener; no robot owner or network reconfiguration."""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.web import create_app


def make_app(*, enabled=True):
    events = []
    owner = SimpleNamespace(
        status={"connection": "disconnected", "phase": "hold"},
        event=events.append,
        args=SimpleNamespace(
            web_host="0.0.0.0", web_allowed_host=[], web_allow_local_addresses=enabled,
        ),
    )
    return create_app(owner), events


def socket_destination(app, address):
    async def routed(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "server": None if address is None else (address, 8766)}
        await app(scope, receive, send)
    return routed


def test_new_local_addresses_work_without_recreating_app():
    app, events = make_app()
    # One app/middleware instance handles both old and newly assigned destinations.
    for address in ("10.18.10.82", "192.168.110.87", "192.168.250.2"):
        with TestClient(socket_destination(app, address), base_url=f"http://{address}:8766") as client:
            assert client.get("/").status_code == 200
            assert client.get("/assets/app.js").status_code == 200
            assert client.get("/status").json()["phase"] == "hold"
    assert events == []


@pytest.mark.parametrize("host", ["evil.example", "10.18.10.82", "0.0.0.0", "192.168.110.87.evil.example"])
def test_host_must_match_socket_not_arbitrary_or_old_address(host):
    app, events = make_app()
    with TestClient(socket_destination(app, "192.168.110.87")) as client:
        assert client.get("/status", headers={
            "Host": host,
            "X-Forwarded-Host": "192.168.110.87",
            "X-Forwarded-For": "192.168.110.87",
            "Forwarded": "host=192.168.110.87",
        }).status_code == 400
    assert events == []


def test_cross_origin_and_command_header_guards_remain():
    app, events = make_app()
    with TestClient(socket_destination(app, "192.168.110.87"), base_url="http://192.168.110.87:8766") as client:
        assert client.post("/event/hold").status_code == 403
        assert client.post("/event/hold", headers={
            "X-YAM-Control": "1", "Origin": "http://evil.example",
        }).status_code == 403
        assert client.post("/event/hold", headers={
            "X-YAM-Control": "1", "Origin": "http://192.168.110.87:8766",
        }).status_code == 200
    assert events == ["hold"]


def test_dynamic_address_policy_is_opt_in():
    app, _ = make_app(enabled=False)
    with TestClient(socket_destination(app, "192.168.110.87"), base_url="http://192.168.110.87:8766") as client:
        assert client.get("/status").status_code == 400


@pytest.mark.parametrize("address", [None, "0.0.0.0", "untrusted.example", "::"])
def test_unknown_socket_destination_fails_closed(address):
    app, _ = make_app()
    with TestClient(socket_destination(app, address)) as client:
        assert client.get("/status", headers={"Host": "192.168.110.87"}).status_code == 400
        assert client.get("/status", headers={"Host": "localhost"}).status_code == 200


def test_real_wildcard_socket_accepts_changed_destination_without_restart():
    app, events = make_app()
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(trust_env=False, timeout=2) as client:
            # Linux loopback /8 provides two destinations without touching NICs/DHCP.
            for address in ("127.0.0.2", "127.0.0.3"):
                assert client.get(f"http://{address}:{port}/status").status_code == 200
            assert client.get(f"http://127.0.0.3:{port}/status", headers={"Host": "127.0.0.2"}).status_code == 400
            assert client.get(f"http://127.0.0.3:{port}/status", headers={"Host": "evil.example"}).status_code == 400
        assert thread.is_alive() and events == []
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
    assert not thread.is_alive()


def test_start_script_does_not_require_wifi_or_capture_dhcp_address(tmp_path):
    executable = tmp_path / ".venv/bin/yam-workstation"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts/start_ipc_web.sh"
    result = subprocess.run(
        ["bash", str(script)], check=True, capture_output=True, text=True,
        env={**os.environ, "YAM_WORKSTATION_ROOT": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "run")},
    )
    assert result.stdout.splitlines() == [
        "--web-only", f"--device-socket={tmp_path / 'run/yam-device.sock'}",
        "--web-port", "8766", "--web-host", "0.0.0.0", "--web-allow-local-addresses",
    ]

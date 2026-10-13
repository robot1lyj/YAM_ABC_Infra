import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.policy_process import ProcessPolicyClient
from yam_abc_reproduce.hil.web import create_app


def _late_policy_reply(conn):
    conn.send(("ready", {}, None))
    conn.recv()
    time.sleep(1.4)
    conn.send(({"actions": np.zeros((50, 14))}, None, None))


def _ignore_termination(conn):
    import signal

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    conn.send("ready")
    while True:
        time.sleep(1)


def test_policy_timeout_reaps_old_child_and_requires_explicit_restart():
    client = ProcessPolicyClient("ws://unused", timeout=.02)
    parent, child = client._context.Pipe()
    process = client._context.Process(target=_late_policy_reply, args=(child,))
    process.start()
    child.close()
    assert parent.poll(5) and parent.recv()[0] == "ready"
    client._conn, client._process = parent, process
    try:
        with pytest.raises(TimeoutError, match="response timeout"):
            client.infer({})
        assert parent.closed and not process.is_alive()
        assert client._process is None and client.metadata is None
        with pytest.raises(RuntimeError, match="restart.*HOLD"):
            client.infer({})
    finally:
        client.close()


def test_shared_process_reaper_kills_unresponsive_child():
    import multiprocessing as mp

    from yam_abc_reproduce.hil.process_pipe import close_pipe_process, start_pipe_process

    process, conn = start_pipe_process(mp.get_context("spawn"), _ignore_termination, name="test-stubborn")
    try:
        assert conn.poll(5) and conn.recv() == "ready"
        close_pipe_process(process, conn, timeout=.1)
        assert conn.closed and not process.is_alive()
        close_pipe_process(process, conn, timeout=.1)  # Repeated shutdown is harmless.
    finally:
        if process.is_alive():
            process.kill()
            process.join(1)


def test_rtc_does_not_start_unused_planner_and_sync_switch_owns_its_lifecycle():
    from yam_abc_reproduce.hil.planner_process import ProcessActionPlanner
    from yam_abc_reproduce.hil.policy import PolicyWorker

    class Transport:
        rtc = True

        def restart(self):
            pass

        def set_rtc(self, enabled):
            self.rtc = enabled

    def wait_ready(worker):
        deadline = time.monotonic() + 5
        while not worker.ready and time.monotonic() < deadline:
            time.sleep(.01)
        assert worker.ready, worker.restart_error

    planner = ProcessActionPlanner()
    worker = PolicyWorker(Transport(), planner=planner)
    try:
        wait_ready(worker)
        assert planner._process is None
        worker.request_transport_mode(False)
        wait_ready(worker)
        process = planner._process
        assert process.is_alive()
        worker.mark_failed("old synchronous planner failure", planner=True)
        worker.request_transport_mode(True)
        wait_ready(worker)
        assert not process.is_alive() and planner._process is None
        assert worker.restart_error is None
        worker.request_transport_mode(False)
        wait_ready(worker)
        assert planner._process.pid != process.pid
    finally:
        worker.close()


def test_policy_process_can_reload_without_replacing_device_owner():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({"action_horizon": 50}))
        while True:
            try:
                obs = msgpack_numpy.unpackb(ws.recv(timeout=2))
            except Exception:
                return
            ws.send(packer.pack({"actions": np.tile(obs["observation.state"], (50, 1))}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ProcessPolicyClient(
            f"ws://127.0.0.1:{server.socket.getsockname()[1]}", timeout=2
        )
        observation = {"observation.state": np.zeros(14), "prompt": "test"}
        try:
            assert client.infer(observation)["actions"].shape == (50, 14)
            old_pid = client._process.pid
            client.restart()
            assert client._process.pid != old_pid
            assert client.infer(observation)["actions"].shape == (50, 14)
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)


def test_policy_transport_switches_to_trained_rtc_in_child_only():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({"rtc_mode": "trained", "rtc_max_delay_steps": 10,
                             "action_horizon": 50, "action_dim": 14,
                             "action_dt_s": 1 / 30}))
        while True:
            try:
                payload = msgpack_numpy.unpackb(ws.recv(timeout=2))
            except Exception:
                return
            rows = np.zeros((50, 14))
            is_rtc = payload.get("type") == "infer"
            if is_rtc:
                prefix = payload["rtc"]["committed_actions"]
                rows[:len(prefix)] = prefix
            ws.send(packer.pack({"actions": rows,
                                 "server_timing": {"rtc_used": is_rtc}}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ProcessPolicyClient(
            f"ws://127.0.0.1:{server.socket.getsockname()[1]}", timeout=2
        )
        image = np.zeros((4, 6, 3), dtype=np.uint8)
        obs = {"observation.state": np.zeros(14), "prompt": "test",
               **{f"observation.images.{role}_rgb": image
                  for role in ("top", "left", "right")}}
        try:
            assert client.infer(obs)["actions"].shape == (50, 14)
            old_pid = client._process.pid
            client.set_rtc(True)
            assert client._process.pid != old_pid
            prefix = np.zeros((8, 14))
            prefix[:, 0] = .2
            result = client.infer_rtc(obs, target_start_tick=100,
                                      committed_actions=prefix)
            assert result["server_timing"]["rtc_used"] is True
            np.testing.assert_allclose(result["actions"][:8], prefix)
            client.set_rtc(False)
            assert client.infer(obs)["server_timing"]["rtc_used"] is False
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)


def test_policy_settings_api_validates_and_forwards_without_motor_calls():
    configured = []
    reloaded = []
    planner_reloaded = []
    sources = []
    owner = SimpleNamespace(
        status={"phase": "hold"},
        configure_policy=lambda **settings: configured.append(settings),
        restart_policy=lambda: reloaded.append(True),
        restart_planner=lambda: planner_reloaded.append(True),
        change_policy_source=lambda **settings: sources.append(settings),
    )
    headers = {"X-YAM-Control": "1"}
    with TestClient(create_app(owner)) as client:
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "sync_hold",
        }).status_code == 200
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "rtc",
            "rtc_delay_steps": 9,
        }).status_code == 200
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "rtc", "rtc_delay_steps": 11,
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "ensemble",
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "smooth",
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "raw",
        }).status_code == 422
        assert client.post("/policy/restart", headers=headers).status_code == 200
        assert client.post("/policy/planner/restart", headers=headers).status_code == 200
        assert client.post("/policy/source", headers=headers,
                           json={"url": "ws://127.0.0.1:8002"}).status_code == 200
    assert configured == [
        {"fusion": "sync_hold"},
        {"fusion": "rtc", "rtc_delay_steps": 9},
    ]
    assert reloaded == [True]
    assert planner_reloaded == [True]
    assert sources == [{"url": "ws://127.0.0.1:8002"}]


def test_source_change_only_queues_and_requires_hold():
    import queue

    from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
    from yam_abc_reproduce.hil.run import Runtime

    owner = Runtime.__new__(Runtime)
    owner.task_switching = False
    owner.session = SimpleNamespace(arbiter=Arbiter(Mode.INFERENCE))
    owner.maintenance = SimpleNamespace(state="idle", latched=False)
    owner.emergency = threading.Event()
    owner.status = {"phase": "hold"}
    owner.recorder = SimpleNamespace(recording=False)
    owner.worker = SimpleNamespace(client=SimpleNamespace(url="ws://old:8000"))
    owner.policy_commands = queue.Queue()
    owner.change_policy_source(url="ws://new:8002")
    envelope, command, receipt = owner.policy_commands.get_nowait()
    assert envelope == "command" and command == ("source", "ws://new:8002")
    assert receipt["state"] == "queued" and receipt is owner.policy_command_status
    assert owner.worker.client.url == "ws://old:8000"
    owner.session.arbiter.phase = Phase.POLICY
    with pytest.raises(ValueError, match="暂停"):
        owner.change_policy_source(url="ws://new:8002")
    owner.session.arbiter.phase = Phase.HOLD
    with pytest.raises(ValueError, match="地址"):
        owner.change_policy_source(url="http://invalid")

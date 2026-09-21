import threading
import time
from types import SimpleNamespace

import uvicorn

from yam_abc_reproduce.hil.device_client import DeviceClient
from yam_abc_reproduce.hil.web import create_app


class FakeDeviceOwner:
    def __init__(self):
        self.args = SimpleNamespace(web_host="127.0.0.1", web_allowed_host=[])
        self.events = []
        self.heartbeats = 0
        self.policy_settings = []
        self.policy_restarts = 0

    @property
    def status(self):
        return {"connection": "connected", "phase": "policy", "tick": 42}

    def event(self, event):
        self.events.append(event)

    def heartbeat(self):
        self.heartbeats += 1

    def configure_policy(self, **settings):
        self.policy_settings.append(settings)

    def restart_policy(self):
        self.policy_restarts += 1

    def preview(self, role):
        return b"jpeg" if role == "top" else None


def test_unix_proxy_restart_holds_without_closing_device_owner(tmp_path):
    owner = FakeDeviceOwner()
    socket_path = tmp_path / "device.sock"
    server = uvicorn.Server(
        uvicorn.Config(create_app(owner), uds=str(socket_path), log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert socket_path.exists()

    try:
        first_web = DeviceClient(socket_path)
        assert first_web.status["tick"] == 42
        first_web.hold_on_attach()
        assert owner.events == ["hold"]

        # A replacement Web/API process attaches to the same persistent owner.
        second_web = DeviceClient(socket_path)
        second_web.hold_on_attach()
        assert owner.events == ["hold", "hold"]
        assert second_web.status["connection"] == "connected"
        assert second_web.preview("top") == b"jpeg"
        second_web.heartbeat()
        assert owner.heartbeats == 1
        second_web.configure_policy(fusion="tda_smooth")
        second_web.configure_policy(fusion="rtc", rtc_delay_steps=7)
        second_web.restart_policy()
        assert owner.policy_settings == [{"fusion": "tda_smooth"}, {"fusion": "rtc", "rtc_delay_steps": 7}]
        assert owner.policy_restarts == 1
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def test_missing_device_owner_is_reported_as_unavailable(tmp_path):
    status = DeviceClient(tmp_path / "missing.sock").status
    assert status["connection"] == "unavailable"
    assert status["phase"] == "hold"

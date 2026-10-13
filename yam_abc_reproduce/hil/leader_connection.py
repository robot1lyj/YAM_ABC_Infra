"""Independent Leader SDK lifetime; Follower control never waits for construction.

The runtime tick starts/polls operations and alone attaches/detaches agents.
The worker owns new or detached Leaders exclusively; it never touches Followers.
"""
import threading

import numpy as np

from ..resource_qos import place_on_cpus


class LeaderConnection:
    def __init__(self, io, factory, *, prepare=lambda: None, stop=lambda: None):
        self.io, self.factory, self.prepare, self.stop = io, factory, prepare, stop
        self.state = "connected" if io.leaders_connected else "disconnected"
        self.error = None
        self.thread = None
        self.cancel = threading.Event()
        self._owners = []
        self._owns_can = io.leaders_connected
        self._enable = False

    @property
    def busy(self):
        return self.thread is not None

    def start(self, enabled):
        if self.busy:
            raise ValueError("Leader连接操作正在进行，请等待")
        if enabled == self.io.leaders_connected and not self.error:
            return
        if enabled and self._owners:
            raise ValueError("上次Leader清理未完成，请先支撑并重试停用")
        self.error, self._enable = None, enabled
        self.cancel.clear()
        if not enabled and self.io.leaders_connected:
            self._owners = [u.agent for u in self.io.units]
            self.io.set_leaders([None, None])
        self.state = "connecting" if enabled else "disconnecting"
        self.thread = threading.Thread(target=self._work, name="leader-lifecycle", daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.thread = None
            if not enabled and self._owners:
                self.io.set_leaders(self._owners)
                self._owners = []
            self.state = "connected" if self.io.leaders_connected else "disconnected"
            raise

    def _release(self):
        errors = []
        for agent in self._owners.copy():
            try:
                close = getattr(agent, "close_hil", None) or agent.stop
                close()
                self._owners.remove(agent)
            except Exception as exc:
                errors.append(str(exc))
        if not self._owners and self._owns_can:
            try:
                self.stop()
                self._owns_can = False
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def _work(self):
        place_on_cpus("CONTROL")
        try:
            if self._enable:
                self._owns_can = True
                self.prepare()
                for agent in self.factory():
                    self._owners.append(agent)
                    if self.cancel.is_set():
                        raise RuntimeError("Leader启用已取消")
                    if not self.io.mock:
                        position, _, age = agent.hil_read()
                        self.io._check_hold_feedback(position, age)
                        # Capture the new Leader's own pose, never the Follower
                        # pose: activation is position HOLD, not an align/move.
                        agent.hil_leader_command(np.asarray(position)[:6], manual=False, gain_scale=1.)
                if len(self._owners) != 2:
                    raise RuntimeError("需要同时连接左右两台Leader")
            else:
                errors = self._release()
                if errors:
                    raise RuntimeError("；".join(errors))
        except Exception as exc:
            self.error = str(exc)
            if self._enable:
                errors = self._release()
                if errors:
                    self.error += "；清理失败：" + "；".join(errors)

    def poll(self, *, cancelled=False):
        if self.thread is None:
            return False
        if cancelled:
            self.cancel.set()
        if self.thread.is_alive():
            return False
        self.thread.join()
        self.thread = None
        if self._enable and not self.error:
            if self.cancel.is_set():
                self.error = "Leader启用已取消；正在关闭未接入的Leader"
                self._enable = False
                self.start(False)
                return False
            self.io.set_leaders(self._owners)
            self._owners = []
        self.state = "fault" if self.error else "connected" if self.io.leaders_connected else "disconnected"
        return True

    def close(self):
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(10)
            if self.thread.is_alive():
                return ["Leader连接线程尚未退出，保留设备所有权；请勿启动第二个控制进程"]
            self.thread = None
        if self.io.leaders_connected:
            self._owners += [u.agent for u in self.io.units]
            self.io.set_leaders([None, None])
        return self._release()

    def snapshot(self):
        return dict(state=self.state, busy=self.busy, enabled=self.io.leaders_connected, error=self.error)

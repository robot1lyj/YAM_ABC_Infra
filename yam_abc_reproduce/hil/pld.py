"""PLD-Grasp wire identity only. Thor owns gating, residuals and rewards.

No local grasp selector, action blending, learner or exploration is implemented.
An identity is pinned explicitly between episodes, never silently replaced by a
new handshake during a run. Raw replies are retained separately from validation.
"""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import asdict, dataclass

import numpy as np

SCHEMA = "pld_sac_block_v2"
MODES = ("off", "shadow", "probe", "collect", "eval")


@dataclass(frozen=True)
class PldConfig:
    mode: str = "off"
    run_id: str | None = None
    contract_sha256: str | None = None
    snapshot_sha256: str | None = None

    @classmethod
    def from_dict(cls, value=None):
        config = cls(**(value or {}))
        if config.mode not in MODES:
            raise ValueError("PLD 模式必须为 off/shadow/probe/collect/eval")
        if config.run_id is not None and (
            not isinstance(config.run_id, str) or not 0 < len(config.run_id) <= 160
        ):
            raise ValueError("PLD run_id 必须为1–160字符")
        for key in ("contract_sha256", "snapshot_sha256"):
            value = getattr(config, key)
            if value is not None and not valid_sha(value):
                raise ValueError(f"PLD {key} 必须为64位SHA256")
        return config

    def as_dict(self):
        return asdict(self)


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def handshake(metadata, mode):
    if not isinstance(metadata, dict) or metadata.get("rtc_mode") != "trained":
        raise ValueError("PLD-Grasp 需要训练式 RTC 服务，请切换为 RTC 并重载推理通信")
    pld = metadata.get("pld")
    if not isinstance(pld, dict):
        raise ValueError("Thor 未加载 PLD-Grasp 专家；当前不能启用该模式，请加载专家或选择 off")
    if mode != "probe" and pld.get("actor_loaded") is False:
        raise ValueError("Thor 当前仅支持 probe 诊断，尚未加载正式专家；不能启用 shadow/collect/eval")
    if pld.get("schema") != SCHEMA or not isinstance(pld.get("modes"), list) or mode not in pld["modes"]:
        raise ValueError("PLD-Grasp 协议或模式不匹配，请核对 Thor 服务版本")
    identity = {key: pld.get(key) for key in ("contract_sha256", "snapshot_sha256")}
    if not all(valid_sha(value) for value in identity.values()):
        raise ValueError("Thor PLD-Grasp 握手缺少有效合同/快照 SHA")
    return identity


class PldClient:
    def __init__(self, config=None):
        self.config = PldConfig.from_dict(config)
        self.run_id = self.config.run_id or uuid.uuid4().hex
        self.identity = None
        self.error = None
        self.last_trace = None

    @property
    def enabled(self):
        return self.config.mode != "off"

    def prepare(self, metadata, *, rtc):
        if not self.enabled:
            return
        try:
            if not rtc:
                raise ValueError("PLD-Grasp 仅使用原生 RTC 队列，不支持同步执行")
            identity = handshake(metadata, self.config.mode)
            expected = {key: getattr(self.config, key) for key in identity}
            if any(value is not None and value != identity[key] for key, value in expected.items()):
                raise ValueError("PLD-Grasp 合同或快照与配置不匹配；未启用修正")
            if self.identity is not None and self.identity != identity:
                raise ValueError("PLD-Grasp 专家身份在运行中变化；请结束本集后重新应用模式")
            self.identity = identity
            self.error = None
        except ValueError as exc:
            self.error = str(exc)
            raise

    def build_request(self, token, *, episode_id, previous_command):
        if not self.enabled:
            return {"mode": "off"}
        if self.identity is None:
            raise ValueError("PLD-Grasp 身份未核对，禁止发送修正请求")
        if not isinstance(episode_id, str) or not 0 < len(episode_id) <= 160:
            raise ValueError("PLD-Grasp 请求缺少原录制 episode_id")
        previous = np.asarray(previous_command, dtype=np.float64)
        if previous.shape != (14,) or not np.isfinite(previous).all():
            raise ValueError("PLD-Grasp 需要观测前实际下发的14D命令，不能用测量state替代")
        return dict(schema=SCHEMA, mode=self.config.mode, **self.identity,
                    run_id=self.run_id, episode_id=episode_id,
                    epoch=token.epoch, request_id=token.request_id,
                    observation_id=token.observation_id, previous_command=previous.copy())

    def snapshot(self, metadata=None, *, rtc=True):
        error = self.error
        ready = not self.enabled
        advertised = None
        if self.enabled:
            try:
                identity = handshake(metadata, self.config.mode) if rtc else None
                advertised = identity
                expected = {key: getattr(self.config, key) for key in (identity or {})}
                ready = identity is not None and error is None and all(
                    value is None or value == identity[key] for key, value in expected.items())
                if identity is None:
                    error = "PLD-Grasp 需要原生 RTC"
                elif not ready:
                    error = error or "PLD-Grasp 合同或快照与配置不匹配"
                elif self.identity is not None and self.identity != identity:
                    ready = False
                    error = "PLD-Grasp 专家身份已变化，请结束本集后重新应用模式"
            except ValueError as exc:
                error = str(exc)
        trace = self.last_trace or {}
        server = metadata.get("pld") if isinstance(metadata, dict) else None
        server = server if isinstance(server, dict) else {}
        modes = server.get("modes")
        supported = [mode for mode in MODES if mode in modes] if isinstance(modes, list) else []
        if not rtc or server.get("schema") != SCHEMA:
            supported = []
        return dict(mode=self.config.mode, run_id=self.run_id, ready=ready, error=error,
                    supported_modes=list(dict.fromkeys(["off", *supported])),
                    server_phase=server.get("phase"), actor_loaded=server.get("actor_loaded"),
                    replay_contract_ready=server.get("replay_contract_ready"),
                    disabled_reason=server.get("disabled_reason"),
                    config=self.config.as_dict(), **(self.identity or advertised or {}),
                    gate=copy.deepcopy(trace.get("gate")), decision_id=trace.get("decision_id"),
                    window_id=trace.get("window_id"), arm=trace.get("arm"))


def validate_reply(request, response, *, token=None, observation=None):
    """Check attribution and non-executing modes; do not whitelist/drop trace fields."""
    if request is None or request.get("mode") == "off":
        if response.get("pld") is not None:
            raise ValueError("PLD-Grasp 已关闭，服务却返回修正记录；请核对来源与模式")
        return
    trace = response.get("pld")
    if not isinstance(trace, dict):
        raise ValueError("PLD-Grasp 回复缺少完整pld记录，已暂停，未静默执行原Pi")
    for key in ("schema", "mode", "contract_sha256", "snapshot_sha256", "run_id",
                "episode_id", "epoch", "request_id", "observation_id"):
        if trace.get(key) != request[key]:
            raise ValueError(f"PLD-Grasp 回复身份/请求不匹配：{key}")
    base = np.asarray(trace.get("base"), dtype=np.float64)
    if base.shape != (50, 14) or not np.isfinite(base).all():
        raise ValueError("PLD-Grasp 回复缺少有限50×14基础计划")
    if not isinstance(trace.get("gate"), dict):
        raise ValueError("PLD-Grasp 回复缺少Thor门控记录")
    if type(trace["gate"].get("active")) is not bool:
        raise ValueError("PLD-Grasp Thor门控有效位缺失")
    state = np.asarray(trace.get("state"), dtype=np.float64)
    if state.shape != (14,) or not np.isfinite(state).all():
        raise ValueError("PLD-Grasp 回复缺少有限14D观测状态")
    if observation is not None and not np.allclose(
            state, observation["observation.state"], rtol=0, atol=2e-6):
        raise ValueError("PLD-Grasp 回复观测状态与原请求不一致")
    if token is not None:
        for key, expected in (("start_tick", token.observation_policy_tick),
                              ("delay_steps", token.rtc_delay_steps)):
            if type(trace.get(key)) is not int or trace[key] != expected:
                raise ValueError(f"PLD-Grasp 回复时间轴与原RTC请求不匹配：{key}")
    previous = np.asarray(trace.get("previous_command"), dtype=np.float64)
    if previous.shape != (14,) or not np.allclose(previous, request["previous_command"], rtol=0, atol=2e-6):
        raise ValueError("PLD-Grasp 回复中的上一实际下发命令不匹配")
    if request["mode"] in ("shadow", "probe") and not np.array_equal(response["actions"], base):
        raise ValueError("PLD-Grasp shadow/probe 必须执行原Pi，服务却改写了动作")
    if trace.get("actions") is not None and not np.array_equal(trace["actions"], response["actions"]):
        raise ValueError("PLD-Grasp trace与顶层动作计划不一致")

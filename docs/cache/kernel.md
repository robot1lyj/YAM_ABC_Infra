# 工作记忆摘要

- 项目使用 uv / Python 3.12；采集入口为 yam-abc-gui。来源：pyproject.toml、docs/environment.md。
- 硬件为 2 follower + 2 官方电动 leader，夹爪精确型号未确认。来源：docs/workstation.md。
- 上游默认 station 是被动 GELLO，不能直接用于该硬件。来源：configs/station_yam.yaml。
- 真实构造/Start Teleop 可能立即运动；本次环境安装不包含真机验收。来源：docs/workstation.md。
- pyproject.toml、uv.lock 进 Git；.venv 和运行数据不进 Git。来源：AGENTS.md、.gitignore。

- 产品三模式：遥操作、推理、DAgger/HIL；Thor模型、RK3588采集/控制。来源：docs/dagger_architecture.md、docs/condapi_interface.md。
- 2026-09-08用户放宽记忆硬预算，继续按需读取及写检查点；不伪称宿主已压缩。来源：AGENTS.md。

- 用户2026-09-08确认三路均为D405；不支持多相机外部硬同步，采用软件时间对齐。来源docs/workstation.md、docs/synchronization_design.md。

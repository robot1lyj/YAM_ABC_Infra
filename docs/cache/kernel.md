# 工作记忆摘要

- 项目使用 uv / Python 3.12；当前四模式入口为 python -m yam_abc_reproduce.hil.run；旧GUI保留上游工具。来源：pyproject.toml、docs/environment.md。
- 硬件为 2 follower + 2 官方电动 leader，夹爪精确型号未确认。来源：docs/workstation.md。
- 上游默认 station 是被动 GELLO，不能直接用于该硬件。来源：configs/station_yam.yaml。
- 真实构造/Start Teleop 可能立即运动；本次环境安装不包含真机验收。来源：docs/workstation.md。
- pyproject.toml、uv.lock 进 Git；.venv 和运行数据不进 Git。来源：AGENTS.md、.gitignore。

- 产品四模式：遥操作、推理、DAgger/HIL、数据采集；Thor模型、RK3588采集/控制。来源：docs/dagger_architecture.md、docs/condapi_interface.md。
- 2026-09-08用户放宽记忆硬预算，继续按需读取及写检查点；不伪称宿主已压缩。来源：AGENTS.md。

- 用户2026-09-08确认三路均为D405；不支持多相机外部硬同步，采用软件时间对齐。来源docs/workstation.md、docs/synchronization_design.md。

- 第一版已接通四模式/官方leader接管/非RTC异步/连续录制和专家导出；线程架构、接收时间配对、宽松可配置阈值。来源docs/hil_quickstart.md。未真机验收。

- 采集模式由Leader遥操作，手动开关多段录制；顶部按模式执行主操作，第二按钮按住持续保持，释放后不自动恢复。来源：docs/collect.md。
- 中文规范通过路由检索，历史英文手册位于docs/archive；指纹/断链检查用scripts/check_project_memory.py，不能替代语义和现场验证。来源：docs/memory.md。

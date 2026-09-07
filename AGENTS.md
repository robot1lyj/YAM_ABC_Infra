# YAM 项目工作约定

## 当前项目

此仓库是本地 YAM 工作站的软件主仓库。`origin` 是用户的内网 YAM 仓库，
`upstream` 是 i2rt-robotics/yam-abc-reproduce。i2rt 保持为固定提交的子模块。

## 记忆入口与所有权

- 从 `docs/cache/context_index.md` 选择当前任务相关的规范文档，不全量加载历史。
- 硬件事实与待确认项由 `docs/workstation.md` 管理；环境由 `docs/environment.md` 管理。
- `docs/cache/kernel.md` 只投影上述事实；进度由 `docs/cache/checkpoint.md` 管理。
- 验收证据保存在 `docs/evidence/`，经验记录保存在 `docs/cache/records/`。
- 使用 mlops-memory 技能；仓库提供标准库工具 `scripts/memory_gate.py`。
  每个实际上下文仅初始化一个 ledger；每包 12,288 字节，累计 32,768 字节。
  按 `docs/memory.md` 操作。ledger 存在 `docs/cache/runtime/`，不提交。
- 记录命令、实际结果、代码/配置哈希和未确认项，不记录私有推理、凭证或完整环境变量。
- 硬件连接、进程、网络状态必须现场复查；历史通过不等于当前可用。

## 环境与验证

- 使用 uv 和 Python 3.12。复现：`uv sync --locked --extra camera --extra gui`。
- 提交 `pyproject.toml` 和 `uv.lock`；不提交 `.venv/`、数据、模型、运行时 ledger。
- 镜像使用项目配置。保留 PyTorch 显式索引，不使用 unsafe 索引策略。
- 默认只安装采集环境；训练后端需按任务选择，不能混装互斥组。
- 配置/依赖改动后运行对应离线验收；不得把 mock 或导入成功写成真机通过。

## 真机边界

- 用户设备：2 台标准 YAM follower、2 台官方电动 YAM leader；不是被动 GELLO。
- 官方 leader 使用 `yam_lead_left/right` 与 `yam_teaching_handle`。
- 平行夹爪具体电机型号尚未核验；不能将 linear_4310 当作已确认事实。
- 不运行 GELLO 清零，不默认写电机零位、刷固件或关闭超时。
- 构造真实机器人可能施加力矩、自动校准夹爪；不能称为只读操作。
- 启动 GUI 不等于启动电机；Start Teleop 可立即校准并同步运动。
- 电机运动前确认现场已固定、清空行程、有人照看；软件停止不能替代硬件急停。

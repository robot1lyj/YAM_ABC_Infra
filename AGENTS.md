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
  2026-09-08 用户明确放宽记忆读取限制：按需读取，原 12,288/32,768 字节
  作为诊断参考，不因工具额度中止任务或要求用户压缩。保留原 ledger，不伪造重置。
  使用短检查点和目标章节管理信息；只有宿主实际执行压缩才称为已压缩。
  按 `docs/memory.md` 操作。ledger 存在 `docs/cache/runtime/`，不提交。
- 记录命令、实际结果、代码/配置哈希和未确认项，不记录私有推理、凭证或完整环境变量。
- 硬件连接、进程、网络状态必须现场复查；历史通过不等于当前可用。

## 环境与验证

- 使用 uv 和 Python 3.12。复现：`uv sync --locked --extra camera --extra gui --extra deploy`。
- 提交 `pyproject.toml` 和 `uv.lock`；不提交 `.venv/`、数据、模型、运行时 ledger。
- 镜像使用项目配置。保留 PyTorch 显式索引，不使用 unsafe 索引策略。
- 新四模式入口 `python -m yam_abc_reproduce.hil.run`，配置 `configs/station_hil.yaml`，教程 `docs/hil_quickstart.md`。
- 运行四模式需追加 `--extra deploy`；模型训练后端仍需按任务选择，不能混装互斥组。
- 配置/依赖改动后运行对应离线验收；不得把 mock 或导入成功写成真机通过。

## 真机边界

- 产品明确四种模式：遥操作、纯推理、DAgger/HIL、数据采集。HIL 内部切换策略/人工/恢复，
  不把这些内部状态另做产品模式。方案所有者为 `docs/dagger_architecture.md`。
- HIL只由键盘/界面介入，先冻结再相对遥操作；手柄①交还模型、②无功能。
- 采集手柄①开始/结束、②放弃；遥操作/推理手柄无功能。数据为Follower反馈/提交目标和三路相机，自动LeRobot；格式整理不得放进控制循环。
- 用户要求优化建议只在对话提出，不建立优化建议文档。
- 主项目是 yam-abc-reproduce；同级 i2rt 已删除，SDK 子模块仍有效。
- Thor 模型/微调归 condapi；RK3588 的采集、控制、仲裁与记录归本项目。
  对接约束见 `docs/condapi_interface.md`，不把 condapi 的旧 LoRA 默认照搬到本项目。

- 用户设备：2 台标准 YAM follower、2 台官方电动 YAM leader；不是被动 GELLO。
- 官方 leader 使用 `yam_lead_left/right` 与 `yam_teaching_handle`。
- 平行夹爪具体电机型号尚未核验；不能将 linear_4310 当作已确认事实。
- 不运行 GELLO 清零，不默认写电机零位、刷固件或关闭超时。
- 构造真实机器人可能施加力矩、自动校准夹爪；不能称为只读操作。
- 启动 GUI 不等于启动电机；Start Teleop 可立即校准并同步运动。
- 电机运动前确认现场已固定、清空行程、有人照看；软件停止不能替代硬件急停。

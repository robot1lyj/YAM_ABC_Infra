# YAM 项目工作约定

## 当前项目

此仓库是本地 YAM 工作站的软件主仓库。默认分支为 `main`。
**内网优先、双远端同步**：`origin` = `ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git`；
`github` = `git@github.com:robot1lyj/YAM_ABC_Infra.git`。代码提交后先推 `origin main`，再推 `github main`，
核对两个远端指向同一提交；单端失败需明确报告，不能称为同步完成。main 跟踪 origin/main。
`upstream` 是 i2rt-robotics/yam-abc-reproduce。i2rt 保持为固定提交的子模块。

## 记忆入口与所有权

- 从 `docs/cache/context_index.md` 选择当前任务相关的规范文档，不全量加载历史。
- 2026-09-14 开工架构基线与 P0–P8 agent 工作包由 `docs/dagger_architecture.md` 持有；RK3588 就是已登记的底层 IPC，Thor 属 condapi。实施先读该基线，再按工作包取对应 owner；物理映射、模型身份与现场验收不能用设计值补齐。
- 硬件事实与待确认项由 `docs/workstation.md` 管理；环境由 `docs/environment.md` 管理。
- `docs/cache/kernel.md` 只投影上述事实；进度由 `docs/cache/checkpoint.md` 管理。
- 验收证据保存在 `docs/evidence/`，经验记录保存在 `docs/cache/records/`。
- 使用用户指定的 `/home/wuyan-lyj/condapi/skills/mlops-memory/SKILL.md` 最新版技能；仓库可选工具 `scripts/memory_gate.py` 同步其检索与证据检查实现。
  完整资料长期保存，无文档行数上限；每次先确定任务和缺失事实，读取相关摘要与规范章节，
  必要时展开原文，信息足以支持下一步就停止检索，不预加载全部历史。
  无默认累计读取额度，不因计数达到固定阈值中止任务、要求压缩或新开对话。
  工作摘要保留目标、用户约束、适用事实、来源证据、未解问题和下一步；有上下文压力时
  将已完成工作整理为可恢复检查点，不删除有用信息。写摘要不代表宿主已执行压缩。
  按 `docs/memory.md` 操作；ledger 仅记录声明预载和检索包，存于 `docs/cache/runtime/`，不提交。
- 先按问题查现有工具、检查方法与历史尝试，再决定复用或修改；检索命中不构成执行授权。
  可复用工具记录入口、环境/配置、输入输出、适用条件、验证方法与验收边界。
  失败尝试保留原因假设、实际干预/结果、混杂因素和重试条件；不把假设写成已证实原因。
  配置预期与实测分开记录，缺失信息标未知；现场条件使用前复查。新增字段按需使用，
  优先当前主线和高价值经验，不要求补齐全部旧记录。规范、记录和问题索引按来源关联。
- 记录命令、实际结果、代码/配置哈希和未确认项，不记录私有推理、凭证或完整环境变量。
- 硬件连接、进程、网络状态必须现场复查；历史通过不等于当前可用。

## 环境与验证

- 使用 uv 和 Python 3.12。复现：`uv sync --locked --extra camera --extra gui --extra deploy`。
- 提交 `pyproject.toml` 和 `uv.lock`；不提交 `.venv/`、数据、模型、运行时 ledger。
- 镜像使用项目配置。保留 PyTorch 显式索引，不使用 unsafe 索引策略。
- 新四模式入口 `uv run --no-sync yam-workstation`，配置 `configs/station_hil.yaml`，教程 `docs/hil_quickstart.md`。
- 运行四模式需追加 `--extra deploy`；模型训练后端仍需按任务选择，不能混装互斥组。
- 配置/依赖改动后运行对应离线验收；不得把 mock 或导入成功写成真机通过。

## 真机边界

- 产品明确四种模式：遥操作、纯推理、DAgger/HIL、数据采集。HIL 内部切换策略/人工/恢复，
  不把这些内部状态另做产品模式。方案所有者为 `docs/dagger_architecture.md`。
- HIL只由键盘/界面介入，先冻结再相对遥操作；手柄①交还模型、②无功能。
- 采集手柄①开始/结束、②放弃；遥操作/推理手柄无功能。数据为Follower反馈/提交目标和三路相机，原始MP4＋HDF5落盘；LeRobot仅独立离线转换，不进入控制循环。
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

## 操作工作台约定

- 交互 `--web-port` 启动不构造硬件；必须由界面连接，真实连接可能施力矩。CLI无界面仍在启动时构造设备。
- 四任务模式不变；回准备位/重力补偿是独占维护状态，不能与模型或遥操作同时写电机。
- 用户明确要求不显示零点标定和底层速度调参；回位用示教准备位，不发送全零关节目标。
- 软件紧急暂停可解除锁存但仍保持；硬件故障不能由此恢复。
- 预览最多5Hz，独立进程、有界缓存，允许丢预览帧；不得将浏览器请求/编码放入控制或录制循环。

- 工作台先建/选任务；连接相机与连接机械臂独立。任务固定到机械臂会话，保存后才可切换；任务身份随原始与LeRobot数据归档。

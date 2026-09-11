# 工作记忆摘要

- 项目使用 uv / Python 3.12；当前四模式入口为 uv run --no-sync yam-workstation；旧GUI保留上游工具。来源：pyproject.toml、docs/environment.md。
- 硬件为 2 follower + 2 官方电动 leader，夹爪精确型号未确认。来源：docs/workstation.md。
- 上游默认 station 是被动 GELLO，不能直接用于该硬件。来源：configs/station_yam.yaml。
- 真实构造/Start Teleop 可能立即运动；本次环境安装不包含真机验收。来源：docs/workstation.md。
- pyproject.toml、uv.lock 进 Git；.venv 和运行数据不进 Git。来源：AGENTS.md、.gitignore。

- 产品四模式：遥操作、推理、DAgger/HIL、数据采集；Thor模型、RK3588采集/控制。来源：docs/dagger_architecture.md、docs/condapi_interface.md。
- 2026-09-08用户放宽记忆硬预算，继续按需读取及写检查点；不伪称宿主已压缩。来源：AGENTS.md。

- 用户2026-09-08确认三路均为D405；不支持多相机外部硬同步，采用软件时间对齐。来源docs/workstation.md、docs/synchronization_design.md。

- 第一版已接通四模式/官方leader接管/非RTC异步/连续录制和专家导出；控制线程＋录制/预览独立进程、接收时间配对、宽松可配置阈值。来源docs/hil_quickstart.md。未真机验收。

- 采集模式由Leader遥操作，手动开关多段录制；HIL键盘i冻结介入、手柄①交还，②无功能；采集①开始/结束、②放弃；遥操作/推理手柄无功能。来源：docs/collect.md。
- 中文规范通过路由检索，历史英文手册位于docs/archive；指纹/断链检查用scripts/check_project_memory.py，不能替代语义和现场验证。来源：docs/memory.md。

- 操作工作台：--web-port启动未连接，界面连接后保持；四模式与维护状态分开。软件紧急暂停→解除后保持→明确开始/回准备位/重力补偿。示教准备位绑定station哈希，mock/real分开。来源：docs/hil_quickstart.md。
- 三路预览最多5Hz、独立低优先级进程与单槽共享内存；控制/录制不编码预览。允许丢预览，不能声称真实RK无资源竞争。来源：docs/hil_quickstart.md、docs/acceptance.md。

- 任务归属：白色悟演智能工作台先建/选任务，再独立连接相机和四臂。任务固定到整个机械臂会话，数据按UUID隔离；断开机械臂后可保留相机预览。来源：docs/collect.md。

- 采集界面优先三路视觉，任务栏紧凑，录制控制集中，急停大尺寸固定在连接旁；详情/日志按需展开。来源：docs/collect.md。

- 工作台任务：name/instruction供采集员中文查看，英文task用于模型prompt、原始task和LeRobot任务文本；完整任务身份存collection_task。旧任务需编辑补填，历史数据不改写。来源：docs/collect.md。

- 新采集格式MP4＋HDF5＋JSON分段清单，默认60秒物理段不拆逻辑集；工作台逐集保存与独立暂停/恢复转换，目标LeRobot v3.0。旧JSONL可读；恢复须审核。来源：docs/convert.md。

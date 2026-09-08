# 当前续作：四模式、双按钮、中文文档

2026-09-08 用户新增独立数据采集，并要求设计官方 Leader 的两个按钮。本轮在三模式基线1c04c83上实现：

- collect 复用 Leader 遥操作，不需要模型；明确开关录制，同一设备会话多段示范。
- 顶部按钮：HOLD时启动；HIL运行中接管/交还；采集HUMAN中开关录制。第二按钮按住持续保持，松开不自动恢复。
- 左右按键独立上升沿、整站250ms去抖、同时触发合并；保持取消排队启动/模式指令。
- RecordingSession 有界后台管理片段；会话目录下 episode_000001 等独立JSONL/三路MP4/manifest，最终session.json索引。
- 采集HOLD/退出中断活动片段，aborted默认不导出；采集human不冒充HIL干预。模式切换关闭旧段。
- 当前README、规范手册、项目决策和优化建议中文化；旧英文说明归档docs/archive。
- scripts/check_project_memory.py只读检查路由、链接、记录与指纹；不提升candidate、不伪造上下文压缩。

验收数值和代码指纹以 docs/evidence/20260908-collection-buttons-v1.json 为准；测试包含多段视频、专家导出、按钮冲突与写盘错误。
真实本机HTTP+mock完成两段成功采集、四模式切换、HIL接管、保持和退出；输出5个episode。没有运行真实电机或Thor模型。

硬件仍为2官方电动Leader、2平行夹爪Follower、3D405；夹爪电机型号与相机序列号待填。D405采用主机接收时间配对，不是曝光同步。
下一步：现场确认按钮位置/CAN映射/方向，逐对低速验证重力补偿和夹爪；随后三路采集、Thor冻结观测契约核验、四臂HIL联调。
剩余架构建议见 docs/optimization_review.md：完整设备关闭结果落盘、模型语义契约、分阶段耗时统计；先测量再决定多进程/编码升级，不做RTC。

环境仍在 /home/wuyan-lyj/YAM/yam-abc-reproduce/.venv，uv为 /home/wuyan-lyj/.local/bin/uv，使用camera/gui/deploy extras。
依赖锁未变；安装环境历史记录不代表设备在线。预算按用户要求仅诊断，按需读取与真实检查点续作。

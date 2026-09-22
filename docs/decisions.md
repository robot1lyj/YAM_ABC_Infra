# 已接受的项目决策

本页维护当前用户约束与工程取舍，不是部署记录。按 `0bc1be5` 源码与截至2026-09-22的用户约定整理；早期决策由来保留在 [历史快照](archive/decisions_20260922.md)。

| 主题 | 当前约定 | 所有者 |
|---|---|---|
| 产品 | 设备与采集平台、数据集平台；专业稳定、完整失败恢复，LAN浏览器操作 | [架构](dagger_architecture.md) |
| 硬件 | 两官方电动Leader、两标准DM4310夹爪Follower、三D405；仅固定i2rt子模块 | [工作站](workstation.md) |
| 人工控制 | teleop/collect/HIL共用绝对1:1映射，不用相对偏移；官方手动增益、SDK重力及开启摩擦补偿 | [采集](collect.md) |
| HIL介入 | Follower保持、Leader辅助对齐；右①进入人工，无对齐门禁；人工①高增益锁定，页面明确交还 | [运行](hil_quickstart.md) |
| 推理 | 仅同步完整50步、TDA、训练式RTC；默认30Hz直接SDK；100Hz二阶仅备用，失败纯线性100Hz不恢复 | [接口](condapi_interface.md) |
| 普通夹爪实验 | 低于0.3映射0.1，仅普通模型；RTC和提交动作回放不做此trick | [接口](condapi_interface.md) |
| 动作边界 | 去掉反馈相对速度包络；保留SDK硬限位、有限值/单位/时效/epoch检查 | [同步](synchronization_design.md) |
| 模型信息 | 不要求记录名称、后端、指纹；当前policy_url诊断残留与旧要求的差异显式登记 | [接口](condapi_interface.md#计时和记录边界) |
| 任务切换 | 暂停、介入/维护结束且保存完成后换数据session，不为切任务断臂 | [采集](collect.md#从遥操作切到采集或换任务) |
| 数据 | MP4/HDF5/JSON实时原始记录，LeRobot显式离线转换；删两类HIL等待，连续训练时间与原始时间并存 | [字段](hil_dataset_fields.md)、[转换](convert.md) |
| 资源与恢复 | 正常控制不等网络/编码/落盘；失败允许HOLD，但局部服务恢复不应重连SDK | [架构](dagger_architecture.md#故障等级与恢复合同) |
| SDK解耦 | 默认device仍持有SDK；可选executor及系统盘运行时已实现、未现场迁移 | [环境](environment.md) |
| 安全 | 不自动恢复运动；软件暂停不是实体急停；设备核心重启须重新确认支撑与现场许可 | [运行](hil_quickstart.md) |
| 环境/交付 | uv/Python3.12、国内镜像、固定锁文件；检查后仅提交本任务，两远端同SHA | [环境](environment.md)、[AGENTS](../AGENTS.md) |
| 记忆 | 先规范后摘要，历史保留、热入口预算、按需检索；不把实现/模拟/部署/实测混写 | [记忆](memory.md) |

功能建议在对话提出；不另造与当前规范并行的一套建议缓存。模型训练、转换、Thor服务归condapi。本项目不以硬件在线、单次成功或用户手感通过代替完整验收。

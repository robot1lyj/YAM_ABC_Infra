# 保留的上游工具

项目已经以官方 YAM Leader 四模式工作站为主。保留旧工具用于兼容和追溯，不让它们成为默认入口。

| 工具 | 当前定位 | 注意事项 |
|---|---|---|
| `yam-abc-gui` | 明确标记为 Legacy 的上游采集、回放、训练管理界面 | 默认使用 `yam-workstation`；CAN 所有权锁拒绝与新工作站重复打开同一通道 |
| `yam-abc-teleop` | 上游遥操作入口 | 不含当前统一HIL状态机 |
| `yam-abc-deploy` | 上游模型部署入口 | 可能只打开Follower，不可直接替代本项目HIL |
| `yam-abc-convert` | canonical转训练格式 | 新HIL原始数据先经过专家段导出 |
| `yam-abc-cameras` | 相机枚举与序列号辅助 | 现场每次重新核对角色与设备 |
| `yam-abc-doctor` | 环境诊断 | 导入成功不等于真机验收 |

当前用户手册已是中文：[采集](collect.md)、[部署](deploy.md)、[训练交接](training.md)、[硬件](hardware.md)。

本项目的 YAM Follower、官方电动 Leader、新工作站和旧 GUI 共用按 CAN 通道划分的跨进程 `flock`。SDK 关闭成功后才释放所有权；暂停、重力补偿和丢弃 Python 引用均不释放。旧 GUI 的显式 Reset Session 会关闭其自持 SDK；自主运行时暂留的电动 Leader 也在最终会话退出时关闭。连接冲突明确显示占用进程，不能通过删锁文件、自动抢占或 Reset CAN 绕过。

锁文件固定放在 `/tmp/yam-can-ownership`，文件本身不删除；遗留文件不代表仍被占用，内核锁才是依据。进程退出后操作系统回收描述符。CAN up/down 和显式 Reset CAN 使用同一所有权检查，重置前先锁住全部枚举目标；任一通道占用时不执行重置。正常 SDK 关闭后到 CAN down 之间若别的 owner 已接入，down 会拒绝，避免关闭新 owner 的总线。

依赖或配置的前置错误可直接修正重试。上游 SDK 工厂若在线程启动后抛错且未返回对象，无法证明半成品线程已清理，当前进程会保留锁并明确要求支撑机械臂后受控重启该 owner；不冒充已安全释放。以上只约束本项目入口，不能阻止直接运行外部官方脚本或系统 CAN 命令。该互斥经模拟验证，不能替代现场运动和断开前的安全确认。
原始上游英文内容保留在 `docs/archive/`，带版本和适用范围标识；不要求翻译第三方源码和全部历史资料。

[采集归档](archive/collect-1c04c83.md) · [部署归档](archive/deploy-1c04c83.md) · [训练归档](archive/training-1c04c83.md) · [硬件归档](archive/hardware-1c04c83.md)

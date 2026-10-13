# 续作检查点（2026-10-13）

- 待部署：普通推理复用Follower-only，不使能Leader；HIL/遥操作不变。本地代码与模拟验证归[验收](../acceptance.md)，线上仍是旧四臂路径；部署须保存完成、支撑并取得当次许可，不自动运动。
- 架构与健康检查`50ed746`已部署，SDK/力矩不改，executor未迁移；职责归[架构](../dagger_architecture.md)。静止检查不证明运动恢复。
- 左Follower J6曾不跟随且红灯，USB重插后一次重连因CAN未UP失败，硬件根因未明，不连续重试。健康门禁归[手册](../hil_quickstart.md#开始前与运行中的设备检查)；历史许可不授权新动作。
- RTC/PLD与C/N归[PLD](../pld_client.md)，Thor无线入口归[环境](../environment.md#ipc连接与地址)，USB仅排障备用；3 N与峰值边界归[硬件](../hardware.md#夹爪受阻软限力)。后续开发继续隔离。

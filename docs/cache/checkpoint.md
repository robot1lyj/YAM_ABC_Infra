# 续作检查点（2026-10-13）

- 已按当次支撑许可部署4ff5939：默认仅Follower，Leader在HOLD显式独立启停，不断开Follower；切模式不暗中启停。IPC隔离251项通过；现场仅核对未连接/无运动，真机启停待用户测试，见[验收](../acceptance.md)。
- executor未迁移；历史进程/IP不代表现在状态，入口复查[环境](../environment.md#ipc连接与地址)。更新后保持未连接，不自动恢复运动。
- 左Follower J6红灯/不跟随根因未明，模拟通过不等于硬件复验成功。健康门禁与操作归[手册](../hil_quickstart.md#开始前与运行中的设备检查)，历史许可不授权新动作。
- RTC/PLD与C/N归[PLD](../pld_client.md)；3 N与峰值边界归[硬件](../hardware.md#夹爪受阻软限力)。继续隔离开发，不新建进度账本。

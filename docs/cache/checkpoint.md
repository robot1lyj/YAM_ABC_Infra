# 当前续作：第一版控制核心与完整改造方案

2026-09-08 用户授权开始实施，三模式：teleop/inference/hil；先不做RTC。
第一版使用整套双臂按键接管；Evo默认i键，非握持感应，无单独四臂停住等待阶段。
实现 hil/core.py、session.py、policy.py、snapshots.py 和 scripts/probe_thor_policy.py。
尚未接入四臂驱动、GUI、异步录制和完整DAgger导出；绝不能说真机HIL已可运行。
Kai0用户已clone：/home/wuyan-lyj/kai0，HEAD 9d93078c757840f50e75248c5c5a94ab7b41e13a。
已审其ARX sync脚本和Agilex关键路径，采用普通块调度，协议依condapi而非ARX。
Evo源码 /tmp/yam-evo-rl-reference，固定6f2db449a21e1bac750b996f2e27cac6739aa63f。
condapi接口规范在docs/condapi_interface.md，来源哈希在docs/evidence；未改参考仓库。
uv环境 .venv（Python3.12.14），增加安装deploy extra；使用绝对路径 /home/wuyan-lyj/.local/bin/uv。
验证：全套149 passed/2 skipped/9 subtests；新HIL测试10项包含本地WebSocket往返。
记忆使用mlops-memory，用户已覆盖严格字节停机要求；按需读取、检查点续作。
未启动任何真机、未连接Thor；下一步依docs/dagger_architecture.md B/C接入并离线验收，再现场D。

最新用户补充：Thor/RK3588是现场本地边缘推理；要求审Kai0优化与成熟同步方案。
已审Kai0 temporal_smooth/ensembling源码，确认非RTC异步/裁剪/线性融合路径；ARX采集并未时间戳配对。
已联网核验Diffusion Policy/UMI、ros2_control、message_filters、RealSense、linuxptp原始资料。
最新方案docs/synchronization_design.md覆盖旧“无预取作为最终方案”表述；现有无预取代码只是基准。
相机型号与硬件同步线已异步询问，尚未获答；不能按仓库默认序列号推断现场设备。

用户已答：三路均D405。官方2025年8月数据手册7.13明确无多相机硬件同步；已更新事实与方案，不再等待型号/同步线答案。

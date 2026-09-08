# 当前续作：三模式可运行第一版

2026-09-08 用户授权按实用方案落地，允许放宽同步容差，避免钻牛角尖。
当前入口 python -m yam_abc_reproduce.hil.run，教程 docs/hil_quickstart.md。
已完成：常驻四臂统一执行、官方YAM leader增益/重力补偿切换、键盘/手柄/本地Web界面、
teleop/inference/hil模式切换先HOLD、非RTC异步重规划与时间裁剪、epoch迟到响应隔离、
D405采集时间元数据/8帧历史/接收时间配对/状态插值、独立有界连续JSONL+MP4记录、
连续专家段导出到原canonical格式。模型调用仍是现场Thor与RK3588以太网本地边缘推理。
现有旧GUI未替换；新三模式有独立Web界面，不能同时打开相同CAN。
尚未实现曝光时钟校准、共享内存多进程、RTC、时间集成/块间融合、真机性能和任务成功率验收。
用户三台D405无外部多机硬同步，当前明确使用主机接收时刻，40ms偏差警告/120ms拒绝新观测，
500ms持续过期保持；阈值在configs/station_hil.yaml可调。SDK状态时间不是每电机CAN接收时刻。
配置已是2官方leader+2平行夹爪follower；实际夹爪电机型号和D405序列号仍占位，真机启动前必须填写。
不清零、不自动机械臂回零；启动构造可能校准夹爪；故障真机会话尝试保持直到明确退出，退出可能撤力矩。
环境 .venv，uv absolute /home/wuyan-lyj/.local/bin/uv；需要camera/gui/deploy extras，无模型训练栈。
验证：全套161 passed、2 skipped、9 subtests；新HIL相关共22项。
4秒模拟120tick完成policy→human→resume，视频三路可解码、专家段导出成功。
真实HTTP模拟冒烟完成三模式切换、接管、保持、成功标记、退出，63条记录、无错误。
12秒保持模拟360tick无deadline miss。均非RK3588硬件性能结论。
证据 docs/evidence/20260908-hil-runtime-v1.json。
下一步填实物映射、逐对低速核验，再Thor冻结观测回放和四臂三D405联调，不盲目改已通过离线路径。
记忆预算用户已覆盖硬停止要求；按需读取/检查点，不伪称自动压缩。

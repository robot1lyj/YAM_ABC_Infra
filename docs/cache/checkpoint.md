# 当前续作：确认后的四模式与DAgger事件链路

2026-09-08用户纠正按钮用途并授权完整改造：

- 遥操作：Leader驱动Follower，可用界面/键盘录制；手柄无功能。
- 纯推理：Thor驱动Follower；手柄无功能。
- HIL：模型阶段Leader镜像跟随；键盘i优先介入，冻结本周期目标并作废旧策略；下一周期自动相对姿态遥操作。手柄①仅在人工阶段交还模型，②无功能。
- 采集：手柄①开始/结束当前episode，②放弃当前集；均不停止遥操作。弃集后台关闭并删除，不删除之前的好集。
- 键盘空格独立暂停，s只从HOLD启动；i不会交还模型，手柄不会启动设备。
- HIL同一episode保留policy/hold/human全部阶段，记录干预编号、事件位、请求与实际提交时刻，不按epoch拆开。

实现位于hil/core.py、session.py、buttons.py、run.py、station.py；TAKEOVER是一周期冻结，之后HUMAN使用接管锚点偏移。夹爪软接管保留。
录制会话队列32项/编码队列8项，后台JSONL+MP4，每个编码器单线程；metrics.py提供最近300样本的分阶段延迟与队列峰值。完整事件/失败不会由格式转换掩盖。
LeRobot v3.0由hil/lerobot_export.py在设备关闭后自动生成，运行期不做格式整理。PyArrow/Pandas/PyAV写入，无RK PyTorch依赖；LeRobot读取器在独立/tmp CPU环境验证。转换写partial后原子改名，原始数据保留；--raw-only可测录制性能。

用户要求建议只在对话提出：optimization_review.md已删除，不再恢复此文档。当前中文手册/README要保持新按钮定义。

硬件仍待现场确认：2官方Leader、2平行夹爪Follower、3D405；夹爪具体型号/CAN/序列号配置占位。没有启动真实电机、没有Thor模型/任务成功率或RK性能验收。SDK状态时间不是每电机CAN到达时间，D405不是曝光同步。
环境仍为项目.venv；uv通过现有清华镜像安装新增轻量数据依赖，pyproject和uv.lock须一起提交。历史离线record依赖变更必须标stale，不能假称仍适用；178项测试通过、2跳过、9子测试通过；官方LeRobot0.5.1回读119帧完整HIL和三路RGB。新测试与性能报告见docs/acceptance.md。
下一步以现场逐对低速验证、三路相机/USB测试、Thor冻结观测契约核对、HIL纠正任务为序。

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

2026-09-08后续：教程统一 `uv run --no-sync yam-workstation`，导出 `uv run --no-sync yam-export`。先完整 `uv sync --locked --extra camera --extra gui --extra deploy`；--no-sync不安装或验证锁。新增--check仅配置/模块可发现性检查，不构造设备或数据目录；真实占位仍拒绝。全套187通过/2跳过/9子测试，旧record已stale，新record为uv-cli-20260908-offline。uv.lock检查通过且无需修改，无依赖升级。

2026-09-08操作界面改造：workbench.py生命周期+web/static双页中文工作台；打开不连接，点击连接后保持，浏览器操作四模式与录制。maintenance.py同控制线程独占示教准备位回位/重力补偿，软件暂停锁存可解除但仍保持；物理故障不能绕过。Follower夹爪进入补偿/回位的开度保留。回位无碰撞规划，真实路径待现场验证。点动仅collect保持未录制。
预览最多5Hz，preview.py独立低优先级进程，单槽共享内存和输出队列1，关闭预览停止编码；独立3s心跳监测请求hold。界面不显示零点标定/底层速度调参。准备位data/workstation按mock/real与station哈希隔离，不提交。
全量200通过/2跳过/9子测试，新增夹爪漂移测试后维护专项13通过。浏览器模拟采集1082帧与官方LeRobot首尾读取通过；预览开/关各15s零deadline miss、每路452帧，非真机性能保证。离线导出RGB直方图统计经过逐像素对照，减少等待。最新verified记录operator-workbench-20260908-offline，旧uv-cli已stale。下一步仍是物理设备信息填写、逐对低速/回位路径验证、RK长时录制与Thor契约核对。

2026-09-08任务工作台：白色悟演智能品牌，急停邻接独立相机/机械臂连接。Tasks本机JSON任务库+UUID会话隔离；连接机械臂前必选任务，会话保存完成才允许切换；三路预览可独立运行。原始和LeRobot provenance保存任务身份，任务文本使用指令。全量203通过/2跳过，最终任务专项5通过；浏览器493帧模拟回读。规范docs/collect.md，证据docs/evidence/20260908-task-workbench.json。

2026-09-09：按具身采集流程收敛布局：71px任务栏、大视觉区、147×66px急停（当前浏览器实测），录制控制集中；HIL按钮按模式显示，详情/日志折叠，可放大视觉区。浏览器验证三路mock预览、HIL/collect切换、放大恢复、任务详情；工作台13测试通过。采集后端未改。

2026-09-09：新增英文task与旧任务编辑补填，中文显示名/说明保留。原始task改为英文字符串，完整身份collection_task，LeRobot/模型prompt使用英文。维护区四个78px大按钮、全局蓝灰配色。全量211通过/2跳过/9子测试；浏览器验证乐高任务补填保存、维护尺寸，无真机动作。

2026-09-09字体/全屏：统一字号层级和深色文字、主色#164e7c；流式主区域，1600px以上设备维护/关节调试双栏。浏览器验证1920×1080和2560×1440无横向溢出，全屏按钮往返；Node语法及diff检查通过。仅前端改动，未重跑后端测试。

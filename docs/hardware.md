# 硬件配置手册

当前设备为两台标准 YAM Follower、两台官方电动 YAM Leader、三台 D405。完整硬件事实、CAN映射和待核验项集中维护在 [工作站事实](workstation.md)，操作入口为 [四模式手册](hil_quickstart.md)。

## 本工作站采用的流程

1. 安装固定、电源和急停准备完成后核验四路 CAN 映射。
2. 核对平行夹爪电机型号和相机序列号，填写 [专用配置](../configs/station_hil.yaml)。
3. 不执行被动 GELLO 编码器清零，不默认改写机械臂零点。
4. 逐台、逐对低速验证方向、重力补偿、夹爪与手柄，再验证四臂接管。
5. D405采用软件时间配对，方案见 [同步设计](synchronization_design.md)。

启动设备构造可能立即施力矩和校准夹爪。退出可能撤力矩，必须先支撑机械臂；软件保持不能替代硬件急停。

## 夹爪受阻软限力

每个 `robot.robots[]` 可配置 `gripper_force_limit_n`，单位 **N**；未指定时保持官方默认 50 N。左右可独立设置，配置进入共用 Follower SDK 构造入口，覆盖推理、遥操作、键盘和页面夹爪操作。2026-10-10已按用户授权将两侧 **10 N 试验值**及SDK参数补丁部署 IPC，并受控重启一次；面板初始化清单已读回10 N，未自动重连或操作夹爪，真实夹持效果与冲击仍待现场验收。参数随 SDK 连接时加载，不在活动抓取中自动热更新，避免突然降力掉物；部署配置不是实测夹持力。

安装时执行 `scripts/apply_i2rt_safety_patches.sh`；新增补丁只把官方工厂写死的 50 N 开放为参数，不重写 `GripperForceLimiter`。未安装对应补丁时，在打开 CAN 前拒绝连接，不静默退回 50 N。初始化设备清单显示“软限力配置（连接时应用）”，不是实测夹持力；录制 manifest 的 station 配置保留该参数。站点编辑切换夹爪型号时保留同侧 N 限值，不悄悄升回 50 N。

官方算法检测约 100 ms 力矩历史及低速受阻后，按夹爪类型将夹持力转换为电机力矩，并调整位置目标。**不是瞬时峰值硬上限**，接触冲击、机械摩擦和电机内部 PD 仍会影响实际受力；SDK `clip_motor_torque` 仅裁剪前馈，不能用它冒充总力矩限制。线性 4310 的公式为 `N × 0.096 / 6.57`，SDK 另加 0.3 Nm 摩擦补偿；10 N 对应约 0.446 Nm 的受阻目标，但不保证实测峰值低于该数。官方受阻检测门槛仍为约 0.5 Nm 平均力矩及低速条件，首次接触可能先超过保持目标；本轮没有暗改官方检测阈值或摩擦项。

原抓取诊断/PARTS 的 `abs(effort)>0.65 Nm` 是电机反馈判据，不是夹持力或安全上限。降低夹持力后必须用新数据复核，不能继续据此把低力矩当作抓空，也不能未经验证直接降低 RL 判据。低力试验应先停用依赖旧判据的 RL 行为，仅记录反馈并人工确认。现场在无负载支撑、路径清空、有人照看下，从低力起逐步验证夹持与损伤；这项软限力不承诺不夹坏所有物体。

参考：[官方限力说明](https://doc.i2rt.com/products/yam#gripper-force-limiting)、[算法与换算](https://github.com/i2rt-robotics/i2rt/blob/main/i2rt/robots/utils.py)。

## P0 USB 物理角色识别

四个 USB-CAN 和三台 D405 都插好后，机械臂保持断电或急停，运行只读向导：

```bash
uv run --no-sync python scripts/identify_arm_usb.py \
  --output docs/evidence/yam-hardware-identity-$(date +%Y%m%d-%H%M%S).json
```

机械臂识别顺序固定为 `RIGHT follower -> LEFT follower -> RIGHT leader -> LEFT leader`；相机识别顺序固定为 `RIGHT wrist -> TOP -> LEFT wrist`。相机序列号通过 RealSense API 读取，IPC 会优先使用 `/home/linux/.venv-yam-camera310/bin/python`，不依赖 `/dev/video0` 编号。

中途失败时可单独重跑 `--only arms` 或 `--only cameras`。向导不会启动 CAN、发送 CAN 帧、构造机器人、写 udev 规则或修改 `cameras.yaml`；报告人工复核后，再生成稳定设备入口和更新相机配置。2026-09-14 的三台 D405 已完成该流程，详见 [相机身份证据](evidence/20260914-rk3588-ipc-cameras.txt)。

上游针对 GELLO 等设备的原始英文内容保留在 [历史归档](archive/hardware-1c04c83.md)，不作为本工作站的默认初始化教程。

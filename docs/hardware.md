# 硬件配置手册

当前设备为两台标准 YAM Follower、两台官方电动 YAM Leader、三台 D405。完整硬件事实、CAN映射和待核验项集中维护在 [工作站事实](workstation.md)，操作入口为 [四模式手册](hil_quickstart.md)。

## 本工作站采用的流程

1. 安装固定、电源和急停准备完成后核验四路 CAN 映射。
2. 核对平行夹爪电机型号和相机序列号，填写 [专用配置](../configs/station_hil.yaml)。
3. 不执行被动 GELLO 编码器清零，不默认改写机械臂零点。
4. 逐台、逐对低速验证方向、重力补偿、夹爪与手柄，再验证四臂接管。
5. D405采用软件时间配对，方案见 [同步设计](synchronization_design.md)。

启动设备构造可能立即施力矩和校准夹爪。退出可能撤力矩，必须先支撑机械臂；软件保持不能替代硬件急停。

## P0 USB 物理角色识别

四个 USB-CAN 和三台 D405 都插好后，机械臂保持断电或急停，运行只读向导：

```bash
uv run --no-sync python scripts/identify_arm_usb.py \
  --output docs/evidence/yam-hardware-identity-$(date +%Y%m%d-%H%M%S).json
```

机械臂识别顺序固定为 `RIGHT follower -> LEFT follower -> RIGHT leader -> LEFT leader`；相机识别顺序固定为 `RIGHT wrist -> TOP -> LEFT wrist`。相机序列号通过 RealSense API 读取，IPC 会优先使用 `/home/linux/.venv-yam-camera310/bin/python`，不依赖 `/dev/video0` 编号。

中途失败时可单独重跑 `--only arms` 或 `--only cameras`。向导不会启动 CAN、发送 CAN 帧、构造机器人、写 udev 规则或修改 `cameras.yaml`；报告人工复核后，再生成稳定设备入口和更新相机配置。2026-09-14 的三台 D405 已完成该流程，详见 [相机身份证据](evidence/20260914-rk3588-ipc-cameras.txt)。

上游针对 GELLO 等设备的原始英文内容保留在 [历史归档](archive/hardware-1c04c83.md)，不作为本工作站的默认初始化教程。

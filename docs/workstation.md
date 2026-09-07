# 工作站事实与初始化状态

## 已确认的硬件需求

用户于 2026-09-07 明确：2 台标准 YAM follower、2 台官方 YAM leader，
follower 配标准平行夹爪。leader 不是被动 GELLO。

| 角色 | CAN 名称 | 配置类型 |
|---|---|---|
| 左 follower | can_left | yam_left |
| 右 follower | can_right | yam_right |
| 左官方 leader | can_lead_l | yam_lead_left |
| 右官方 leader | can_lead_r | yam_lead_right |

官方 leader 手柄类型是 `yam_teaching_handle`。默认配置
`configs/station_yam.yaml` 仍是上游被动 GELLO 工作站示例，不能直接启动本工作站。
完成实物核验后再修改它，或建立专用 station 文件。

## 待核验

- 平行夹爪具体型号：linear_4310 / linear_3507 / 其他；待电机标识或装箱单核验。
- 四个 USB-CAN 序列号、左右映射、相机型号与序列号。
- 实际控制电脑、机械安装、电源、急停、零位与关节方向。
- 本次尚未完成真机上电、重力补偿、夹爪校准或遥操作验收。

## 现场初始化顺序

1. 固定四台臂，断电检查接线，每台独立 CAN 通道，准备硬件停止手段。
2. 逐个识别适配器序列号，按上表命名；CAN 速率 1 Mbit/s。
3. 核验夹爪型号，配置官方 leader；不要执行 GELLO 编码器清零。
4. 逐台测试重力补偿；启动会给电机施加力矩，夹爪可能自动开闭。
5. 验证既有零位、夹爪行程和手柄输入；不例行写电机零位。
6. 先相近姿态、小幅验证一对，再验证另一对，最后双臂遥操作。
7. 配置真实相机序列号，录制短演示并回放验收。

退出、Power Off Arms 或 Reset Session 可能撤掉力矩，应先支撑机械臂。
手柄按钮在启动时保持松开，以便当前驱动学习空闲电平。

## 来源

- 用户本轮硬件说明，2026-09-07；型号细节仍以实物为准。
- 本地 `yam_abc_reproduce/config.py`、`robot/yam_adapter.py`（位于包目录内）。
- 官方 https://doc.i2rt.com/products/yam-cell 。官方 CAN 命名与本项目不同。
- 上游 `docs/hardware.md` 的 GELLO 专用段落不适用于此工作站。

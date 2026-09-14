# 工作站事实与初始化状态

## 已确认的硬件需求

用户于 2026-09-07 明确：2 台标准 YAM follower、2 台官方 YAM leader，
follower 配标准平行夹爪。leader 不是被动 GELLO。
用户于2026-09-08进一步确认：三路相机均为 RealSense D405；2026-09-14 已完成人工标签复核与序列号/角色映射。
用户于2026-09-14说明上述硬件已到齐，RK3588 IPC 已接入、Thor 已在 condapi 准备；这是用户到货/准备状态说明，不能替代逐项通讯、推理服务和运动验收。运行节点分工见 [架构基线](dagger_architecture.md#2026-09-14-架构基线-v1)。
同日追加确认：机械臂经 hub 接入 IPC，四根臂为两根 follower、两根 leader；**第一项现场工作是识别各机械臂所接 USB-CAN 下联口并完成角色映射**，不要求读取机械臂本体 S/N。随后用户确认接法为 **USB Hub连接各机械臂的USB-CAN接口**。固定 USB 口映射已完成，Hub型号仍待核验；适配器审计序列号只作交叉核对，不按已枚举的板载bcan直接安排接臂。

| 角色 | CAN 逻辑角色（待映射物理口） | 配置类型 |
|---|---|---|
| 左 follower | can_left | yam_left |
| 右 follower | can_right | yam_right |
| 左官方 leader | can_lead_l | yam_lead_left |
| 右官方 leader | can_lead_r | yam_lead_right |

官方 leader 手柄类型是 `yam_teaching_handle`。当前专用配置为
[configs/station_hil.yaml](../configs/station_hil.yaml)，已绑定官方leader类型；电机型号仍需实物填写，相机序列号已写入 [configs/cameras.yaml](../configs/cameras.yaml)。
`configs/station_yam.yaml` 是保留的上游被动GELLO示例，不能直接启动本工作站。

## 第一步：设备身份登记 P0

本节是物理身份/拓扑的唯一登记处。四臂的本体与适配器角色仍待现场逐台核验；三台 D405 已按报告、拔插复核和物理标签完成登记。机械臂严格为两 follower＋两官方电动 leader。

| 设备角色 | 本体型号 / 本体S/N或资产标签（非必需） | 机械臂所接 USB-CAN 下联口 / 适配器审计S/N | IPC端口与hub层级 / CAN通道 | 当前结论 |
|---|---|---|---|---|
| 左 follower / yam_left | 标准YAM；本体S/N不要求；夹爪型号待核 | `5-2.3`；适配器审计S/N `207D34A258455017` | USB Hub `5-2.3`；稳定名 `can_left` | USB口已核验；本体S/N不适用 |
| 右 follower / yam_right | 标准YAM；本体S/N不要求；夹爪型号待核 | `5-2.4`；适配器审计S/N `207C378445465006` | USB Hub `5-2.4`；稳定名 `can_right` | USB口已核验；本体S/N不适用 |
| 左 leader / yam_lead_left | 官方电动YAM；本体S/N不要求；yam_teaching_handle | `5-2.1`；适配器审计S/N `207F34A658455017` | USB Hub `5-2.1`；稳定名 `can_lead_l` | USB口已核验；本体S/N不适用 |
| 右 leader / yam_lead_right | 官方电动YAM；本体S/N不要求；yam_teaching_handle | `5-2.2`；适配器审计S/N `205534A258455017` | USB Hub `5-2.2`；稳定名 `can_lead_r` | USB口已核验；本体S/N不适用 |
| 顶部相机 / top | D405；`260522275397` | 相机自身 RealSense S/N | USB3；`/dev/yam-camera-top`（当前 `/dev/video6`） | 已核验 |
| 左相机 / left | D405；`260522271298` | 相机自身 RealSense S/N | USB2；`/dev/yam-camera-left`（当前 `/dev/video0`） | 已核验 |
| 右相机 / right | D405；`260422271123` | 相机自身 RealSense S/N | USB3；`/dev/yam-camera-right`（当前 `/dev/video12`） | 已核验 |

登记顺序：

1. 核对已确认的USB Hub→USB-CAN接法，识别Hub型号、供电、IPC上联口及各USB-CAN下联口，记录实际USB/CAN拓扑。机械臂通信适配器与相机是否共用 hub 当前未知，不自行推定。只做设备枚举和实物标签对应，不启动工作台真实连接、不构造机器人 SDK。
2. 在设备允许且电机链路停用的条件下，逐台接入或对应标签，建立机械臂↔USB-CAN↔hub物理口↔IPC接口的对应。机械臂本体 S/N 不是本流程的必要条件；记录 USB-CAN 的 VID/PID、适配器审计序列号（若提供）、稳定路径与驱动；系统枚举次序如can0/USB设备号不是永久身份。
3. 三台D405逐台对应其设备序列号与top/left/right实际视角，另存USB路径及共享上联。该步骤已完成；相机硬件序列号绑定角色，不靠启动顺序或/dev/videoN。
4. 本体无可读S/N时明确写“未提供/无法读取”，贴本地唯一资产标签，保留与适配器关系；适配器无唯一S/N时使用固定端口拓扑加物理标签并标明换口会失效。不得编造序列号或把电机CAN ID（可能重复）当整臂唯一序列号。
5. 清点恰为4臂、3相机，复枚举确认不重号、不串角色；涉及换口/重新上电的复核按现场允许条件安排。四个 USB-CAN 下联口到逻辑名的规则已固化，仍需核对夹爪型号、端接和实际CAN通信。记录证据、观察时间和适用接线；P0只完成身份，不表示CAN通讯、相机流、夹爪校准或运动通过。

P0交付：填好的本表、hub/USB-CAN拓扑、未识别项与原因、枚举命令和脱敏输出，写入独立 `docs/evidence/` 证据。缺少身份只阻断相关设备映射，不通过启用电机自动校准来获取编号。

### P0 首轮枚举快照（2026-09-14）

环境部署完成后的首轮只读枚举显示 IPC 当前仅有板载 USB2 Hub 和蓝牙设备；没有外接
USB-CAN、D405 或 `/dev/video*`。因此四臂本体、适配器、Hub 下联端口和三路相机序列号均
保持“待识别”，没有用 `bcan0`～`bcan3` 或系统枚举顺序填充角色。辅助 RealSense 环境的
`rs.context().query_devices()` 返回空列表。详见
[P1 部署与 P0 枚举证据](evidence/20260914-rk3588-ipc-p1-deploy.txt)。硬件接入并贴好物理
标签后，按本节登记顺序重新执行 P0；此快照不表示相机或机械臂通信通过。

### P0 相机身份与稳定入口（2026-09-14）

报告 `yam_hardware_identity-20260914-143334.json` 的 RealSense API 基线和三次逐台拔插复核均无错误；用户随后人工核对了相机物理标签，确认角色映射如下：

| 角色 | RealSense S/N | RealSense physical_port | 稳定入口 |
|---|---|---|---|
| right | `260422271123` | `usb10/10-1/.../video12` | `/dev/yam-camera-right` |
| top | `260522275397` | `usb8/8-1/.../video6` | `/dev/yam-camera-top` |
| left | `260522271298` | `usb3/3-1/.../video0` | `/dev/yam-camera-left` |

IPC 已应用 `/etc/udev/rules.d/91-yam-cameras.rules`，当前三个稳定入口已触发并解析到 `video12`、`video6`、`video0`。right/top 的 UVC 层提供内部序列号；left 的 USB2 UVC 层没有暴露该序列号，因此规则对 left 使用其已核验的固定 USB 路径。YAM 应继续用 `cameras.yaml` 中的 RealSense S/N 选择设备，不把 `/dev/videoN` 当永久身份。此步骤只创建 udev 符号入口并更新配置，未启动相机流、CAN 或电机。

## 待核验

- 平行夹爪具体型号：linear_4310 / linear_3507 / 其他；待电机标识或装箱单核验。
- 用户USB Hub链路的型号/拓扑、四个USB-CAN与机械臂本体/接线/终端电阻、夹爪型号；固定USB下联口到 `can_left`/`can_right`/`can_lead_l`/`can_lead_r` 的稳定规则已应用，板载 `bcan0`～`bcan3` 仍仅为已枚举资源，不默认用于机械臂。
- 相机流兼容性、采集性能/时间戳、标定，以及机械安装、电源、急停、零位、关节方向与夹爪端点；底层控制电脑已确定为下节 RK3588 IPC。
- 本次尚未完成真机上电、重力补偿、夹爪校准或遥操作验收。

## 现场初始化顺序

1. 先完成上节P0身份与hub拓扑登记；固定四台臂，断电检查接线，每台独立CAN通道，准备硬件停止手段。
2. 用已登记的通信适配器与hub映射配置稳定通道；CAN初始配置速率1 Mbit/s，须核验当前设备总线配置。hub提供的逻辑独立通道数需确认，不能把四条臂不加核验地并在同一CAN总线上。
3. 核验夹爪型号，配置官方 leader；不要执行 GELLO 编码器清零。
4. 逐台测试重力补偿；启动会给电机施加力矩，夹爪可能自动开闭。
5. 验证既有零位、夹爪行程和手柄输入；不例行写电机零位。
6. 先相近姿态、小幅验证一对，再验证另一对，最后双臂遥操作。
7. 配置真实相机序列号，录制短演示并回放验收。

退出、Power Off Arms 或 Reset Session 可能撤掉力矩，应先支撑机械臂。
手柄按钮在启动时保持松开，以便当前驱动学习空闲电平。

## RK3588 IPC 现场事实（2026-09-14）

已现场登录并核验一台作为 YAM 底层控制器的 IPC。产品资料对应 KiWiBot/阿普奇 TER30R-A2；设备树实际标识为
`Rockchip RK3588 EVB7 LP4 V10 Board`，因此设备树型号不能单独替代机箱标签核验。

- 系统为 Ubuntu 22.04.3 LTS、`arm64`，内核 `6.1.118 PREEMPT_RT`。
- 8 核 Cortex-A55/A76，现场观察内存总量 15 GiB；根分区为 eMMC `mmcblk0p6`，NVMe `nvme0n1p1` 为 953.9 GiB、ext4、标签 `yam-data`，已挂载到 `/data`。
- YAM 当前唯一工作目录为 `/data/YAM`（现场约 1.5 GiB）；旧 `/home/linux/YAM` 已移除，不保留兼容软链接。NVMe 的 UUID 为 `501fb615-1346-455d-9d50-61c1d113faf5`，已写入 `/etc/fstab`，使用 `noatime,nofail` 开机挂载。
- 厂商内核未启用 `CONFIG_CAN_GS_USB`；已针对运行中的 `6.1.118 PREEMPT_RT` 编译并安装 `gs_usb.ko`，路径为 `/lib/modules/6.1.118/extra/gs_usb.ko`，并写入 `/etc/modules-load.d/gs_usb.conf`。当前四个 CANable 2.5 均显示为 USB-backed `can0`～`can3`，接口保持 `DOWN/STOPPED`，尚未进行 CAN 通讯验收。
- 当前 USB 下联口与稳定名为：`5-2.1`→`can_lead_l`（适配器审计S/N `207F34A658455017`）、`5-2.2`→`can_lead_r`（`205534A258455017`）、`5-2.3`→`can_left`（`207D34A258455017`）、`5-2.4`→`can_right`（`207C378445465006`）。运行时锁定的是 USB 口；机械臂本体 S/N 不参与映射。
- 三台 D405 已按物理标签和 RealSense API 序列号登记：`right=260422271123`、`top=260522275397`、`left=260522271298`。IPC 已应用 `/etc/udev/rules.d/91-yam-cameras.rules`，建立 `/dev/yam-camera-right`、`/dev/yam-camera-top`、`/dev/yam-camera-left`；当前分别解析到 `video12`、`video6`、`video0`。`/data/YAM/configs/cameras.yaml` 已使用上述 RealSense S/N。
- NPU 节点为 `/dev/dri/renderD129`；`bcan0`～`bcan3` 均存在，但现场均为 `STOPPED/DOWN`，尚未接 CAN 总线或机械臂验收。
- 网卡命名为 `wlan0`、`lan1`～`lan5`。当前 `wlan0=192.168.110.140/23` 保持 IPC 上网；`lan1=192.168.250.2/24` 为无网关、无 DNS 的独立调试链路。
- 本机通过 `enp1s0=192.168.250.1/24` 强制走网线 SSH 验证成功。IPC 和本机的互联网默认路由均未由该私网口接管。
- SSH 密钥通行已完成：本机管理公钥已加入 IPC `linux` 用户的 `authorized_keys`，网线免密登录已验证。
- IPC 已生成独立 Gitea 客户端 Ed25519 密钥，路径为 `/home/linux/.ssh/id_ed25519_gitea`；私钥不离开 IPC，公钥指纹为 `SHA256:L7VjWh8BHNTOMsrNp0l6St2v/OYQ14WPcIXDOBJNhtA`。
- 当前 Wi-Fi 配置 `琶洲模方` 已是 `connection.autoconnect=yes` 且 `wlan0` 在线；Gitea 已接受 IPC 专用密钥并认证为 `wuyan_lyj`，目标为 `192.168.110.142:2222`。
- 当前 IPC 使用 uv `0.12.13`（`aarch64-unknown-linux-gnu`）管理 Python 3.12 环境；YAM 主 checkout 已迁移至 `/data/YAM`，并在新路径完成锁定依赖同步。首次 bootstrap 时的 Python 3.10/未部署状态仅保留在历史证据中。
- `ssh.service` 与厂商 `autorun.service` 正在运行；本次未停止服务、未启动 YAM、未操作电机或 CAN。

详细现场证据见 [20260914-rk3588-ipc-bootstrap.txt](evidence/20260914-rk3588-ipc-bootstrap.txt)、[20260914-rk3588-ipc-nvme.txt](evidence/20260914-rk3588-ipc-nvme.txt) 和 [20260914-rk3588-ipc-gs-usb.txt](evidence/20260914-rk3588-ipc-gs-usb.txt)。密码只做交互式引导，不写入项目记忆；设备重刷、网络变更、密钥轮换或正式部署前须重新核验。

## 来源

- 用户本轮硬件说明，2026-09-07；型号细节仍以实物为准。
- 本地 `yam_abc_reproduce/config.py`、`robot/yam_adapter.py`（位于包目录内）。
- 官方 https://doc.i2rt.com/products/yam-cell 。官方 CAN 命名与本项目不同。
- 上游 `docs/hardware.md` 的 GELLO 专用段落不适用于此工作站。

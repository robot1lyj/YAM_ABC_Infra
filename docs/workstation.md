# 工作站事实与初始化状态

## 已确认的硬件需求

用户于 2026-09-07 明确：2 台标准 YAM follower、2 台官方 YAM leader，
follower 配标准平行夹爪。leader 不是被动 GELLO。
用户于2026-09-08进一步确认：三路相机均为 RealSense D405；现场序列号/角色映射待核验。
用户于2026-09-14说明上述硬件已到齐，RK3588 IPC 已接入、Thor 已在 condapi 准备；这是用户到货/准备状态说明，不能替代逐项通讯、推理服务和运动验收。运行节点分工见 [架构基线](dagger_architecture.md#2026-09-14-架构基线-v1)。
同日追加确认：机械臂经 hub 接入 IPC，四根臂为两根 follower、两根 leader；**第一项现场工作是识别机械臂与相机的序号、完成角色映射**。随后用户确认接法为 **USB Hub连接各机械臂的USB-CAN接口**。这是已确认的接线方案，实际Hub型号/物理端口/适配器序列号仍待枚举，不按已枚举的板载bcan直接安排接臂。

| 角色 | CAN 逻辑角色（待映射物理口） | 配置类型 |
|---|---|---|
| 左 follower | can_left | yam_left |
| 右 follower | can_right | yam_right |
| 左官方 leader | can_lead_l | yam_lead_left |
| 右官方 leader | can_lead_r | yam_lead_right |

官方 leader 手柄类型是 `yam_teaching_handle`。当前专用配置为
[configs/station_hil.yaml](../configs/station_hil.yaml)，已绑定官方leader类型，电机型号/相机序列号仍需实物填写。
`configs/station_yam.yaml` 是保留的上游被动GELLO示例，不能直接启动本工作站。

## 第一步：设备身份登记 P0

本节是物理身份/拓扑的唯一登记处。当前以下均是角色占位，**尚未读取任何真实序列号**；后续 agent 先完成此表，再生成正式 station 和稳定设备映射。相机仍按此前确认的三台 D405，机械臂严格为两 follower＋两官方电动 leader。

| 设备角色 | 本体型号 / 本体S/N或资产标签 | USB-CAN适配器S/N / 稳定ID | IPC端口与hub层级 / CAN通道 | 当前结论 |
|---|---|---|---|---|
| 左 follower / yam_left | 标准YAM；S/N待识别；夹爪型号待核 | 待识别；不可拿适配器S/N当臂本体S/N | 经USB Hub/USB-CAN，物理口待识别；逻辑can_left | 未核验 |
| 右 follower / yam_right | 标准YAM；S/N待识别；夹爪型号待核 | 待识别 | 经USB Hub/USB-CAN，物理口待识别；逻辑can_right | 未核验 |
| 左 leader / yam_lead_left | 官方电动YAM；S/N待识别；yam_teaching_handle | 待识别 | 经USB Hub/USB-CAN，物理口待识别；逻辑can_lead_l | 未核验 |
| 右 leader / yam_lead_right | 官方电动YAM；S/N待识别；yam_teaching_handle | 待识别 | 经USB Hub/USB-CAN，物理口待识别；逻辑can_lead_r | 未核验 |
| 顶部相机 / top | D405；RealSense序列号待识别 | 相机自身序列号 | USB端口/hub归属/安装视角待识别 | 未核验 |
| 左相机 / left | D405；RealSense序列号待识别 | 相机自身序列号 | USB端口/hub归属/安装视角待识别 | 未核验 |
| 右相机 / right | D405；RealSense序列号待识别 | 相机自身序列号 | USB端口/hub归属/安装视角待识别 | 未核验 |

登记顺序：

1. 核对已确认的USB Hub→USB-CAN接法，识别Hub型号、供电、IPC上联口及各USB-CAN下联口，记录实际USB/CAN拓扑。机械臂通信适配器与相机是否共用 hub 当前未知，不自行推定。只做设备枚举和实物标签对应，不启动工作台真实连接、不构造机器人 SDK。
2. 在设备允许且电机链路停用的条件下，逐台接入或对应标签，建立四臂本体↔通信适配器↔hub物理口↔IPC接口的对应。记录 VID/PID、序列号（若提供）、稳定路径与驱动；系统枚举次序如can0/USB设备号不是永久身份。
3. 三台D405逐台对应其设备序列号与top/left/right实际视角，另存USB路径及共享上联。相机硬件序列号绑定角色，不靠启动顺序或/dev/videoN。
4. 本体无可读S/N时明确写“未提供/无法读取”，贴本地唯一资产标签，保留与适配器关系；适配器无唯一S/N时使用固定端口拓扑加物理标签并标明换口会失效。不得编造序列号或把电机CAN ID（可能重复）当整臂唯一序列号。
5. 清点恰为4臂、3相机，复枚举确认不重号、不串角色；涉及换口/重新上电的复核按现场允许条件安排。记录证据、观察时间和适用接线；再据此制作稳定CAN命名及真实station。P0只完成身份，不表示CAN通讯、相机流、夹爪校准或运动通过。

P0交付：填好的本表、hub/USB-CAN拓扑、未识别项与原因、枚举命令和脱敏输出，写入独立 `docs/evidence/` 证据。缺少身份只阻断相关设备映射，不通过启用电机自动校准来获取编号。

### P0 首轮枚举快照（2026-09-14）

环境部署完成后的首轮只读枚举显示 IPC 当前仅有板载 USB2 Hub 和蓝牙设备；没有外接
USB-CAN、D405 或 `/dev/video*`。因此四臂本体、适配器、Hub 下联端口和三路相机序列号均
保持“待识别”，没有用 `bcan0`～`bcan3` 或系统枚举顺序填充角色。辅助 RealSense 环境的
`rs.context().query_devices()` 返回空列表。详见
[P1 部署与 P0 枚举证据](evidence/20260914-rk3588-ipc-p1-deploy.txt)。硬件接入并贴好物理
标签后，按本节登记顺序重新执行 P0；此快照不表示相机或机械臂通信通过。

## 待核验

- 平行夹爪具体型号：linear_4310 / linear_3507 / 其他；待电机标识或装箱单核验。
- 用户USB Hub链路的型号/拓扑、四个USB-CAN接口的身份/驱动/独立通道、角色/接线/终端电阻；板载 `bcan0`～`bcan3` 仅为已枚举资源，不默认用于机械臂。现有四个逻辑名不代表物理映射已完成。
- 相机序列号/安装视角/角色/USB 拓扑，机械安装、电源、急停、零位、关节方向与夹爪端点；底层控制电脑已确定为下节 RK3588 IPC。
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
- 8 核 Cortex-A55/A76，现场观察内存总量 15 GiB；根分区为 eMMC `mmcblk0p6`，另有 953.9 GiB NVMe 未挂载。
- NPU 节点为 `/dev/dri/renderD129`；`bcan0`～`bcan3` 均存在，但现场均为 `STOPPED/DOWN`，尚未接 CAN 总线或机械臂验收。
- 网卡命名为 `wlan0`、`lan1`～`lan5`。当前 `wlan0=192.168.110.140/23` 保持 IPC 上网；`lan1=192.168.250.2/24` 为无网关、无 DNS 的独立调试链路。
- 本机通过 `enp1s0=192.168.250.1/24` 强制走网线 SSH 验证成功。IPC 和本机的互联网默认路由均未由该私网口接管。
- SSH 密钥通行已完成：本机管理公钥已加入 IPC `linux` 用户的 `authorized_keys`，网线免密登录已验证。
- IPC 已生成独立 Gitea 客户端 Ed25519 密钥，路径为 `/home/linux/.ssh/id_ed25519_gitea`；私钥不离开 IPC，公钥指纹为 `SHA256:L7VjWh8BHNTOMsrNp0l6St2v/OYQ14WPcIXDOBJNhtA`。
- 当前 Wi-Fi 配置 `琶洲模方` 已是 `connection.autoconnect=yes` 且 `wlan0` 在线；Gitea 已接受 IPC 专用密钥并认证为 `wuyan_lyj`，目标为 `192.168.110.142:2222`。
- 当前 IPC 只有系统 Python 3.10.12，未发现 `uv`，在有限搜索范围内未发现 YAM checkout；项目部署仍需按 [环境](environment.md) 准备 Python 3.12/uv。
- `ssh.service` 与厂商 `autorun.service` 正在运行；本次未停止服务、未启动 YAM、未操作电机或 CAN。

详细现场证据见 [20260914-rk3588-ipc-bootstrap.txt](evidence/20260914-rk3588-ipc-bootstrap.txt)。密码只做交互式引导，不写入项目记忆；设备重刷、网络变更、密钥轮换或正式部署前须重新核验。

## 来源

- 用户本轮硬件说明，2026-09-07；型号细节仍以实物为准。
- 本地 `yam_abc_reproduce/config.py`、`robot/yam_adapter.py`（位于包目录内）。
- 官方 https://doc.i2rt.com/products/yam-cell 。官方 CAN 命名与本项目不同。
- 上游 `docs/hardware.md` 的 GELLO 专用段落不适用于此工作站。

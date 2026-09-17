# 中国工作站环境复现

## 项目与依赖

主仓库为 YAM-ABC-Reproduce 的本地工作站分支，保留上游历史。
内网优先，两个托管地址都同步：
- `origin`：`ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git`，主远端，main跟踪origin/main。
- `github`：`git@github.com:robot1lyj/YAM_ABC_Infra.git`，同步远端。
- 顺序：`git push -u origin main`，然后 `git push github main`；核对两端SHA一致。失败时保留本地提交，报告未同步的远端，不强制覆盖远端历史。

upstream 是 `https://github.com/i2rt-robotics/yam-abc-reproduce.git`。
i2rt 使用 `third_party/i2rt` 固定提交，不依赖工作区同级 i2rt 目录。

采用 Python 3.12 和 uv；Git 跟踪 pyproject.toml、uv.lock、.python-version，
忽略 .venv。采集不需要安装训练后端。

## 首次安装

```bash
git clone --recurse-submodules ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git
cd YAM
git remote add github git@github.com:robot1lyj/YAM_ABC_Infra.git
scripts/apply_i2rt_safety_patches.sh
sudo apt update
sudo apt install build-essential python3-dev git curl iproute2 can-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv python install 3.12.14
uv sync --locked --extra camera --extra gui --extra deploy
uv run --no-sync python -c "import i2rt, yam_abc_reproduce, cv2, pyrealsense2, av; print('imports OK')"
```

uv 自动创建 .venv，不需要系统 pip。本工作站固定 uv 管理的 Python 3.12.14，
该分发自带头文件。首次用系统 Python 3.12.3 编译 ruckig 时因缺少 Python.h 失败；
详见 docs/evidence/20260907-system-python-build-failure.txt。
ruckig 仍需要本地 C++ 编译工具。仅克隆普通仓库后需要先 `git submodule update --init --recursive`。

## 国内镜像

pyproject 中 `[[tool.uv.index]]` 配置清华镜像为 default：
https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple 。
正式使用 uv 解析锁文件，不能手工替换锁中的下载 URL。
PyTorch 的 cpu/cu121/cu128 索引保留 explicit 及按组绑定。
镜像不加速 GitHub 子模块、uv/Python 安装器或模型权重。
保留 TLS 校验和 first-index，不用 trusted-host 或 unsafe-best-match。

换源应执行 uv sync、审查锁变化并提交，不以 --frozen 掩盖锁与项目不一致。
阿里镜像是可选替代，未配置自动故障转移。

参考：
- https://docs.astral.sh/uv/concepts/indexes/
- https://mirrors.tuna.tsinghua.edu.cn/help/pypi/

## 日常操作

在项目根目录运行，不需要激活虚拟环境。首次安装或拉取依赖变更后执行上面的完整 `uv sync --locked`。
日常 `uv run --no-sync` 直接使用已准备的项目环境，不同步依赖、不访问包索引；
它不会验证环境与锁文件一致，不能替代安装步骤。`uv run` 默认进行非精确同步，
而 `uv sync` 默认精确同步，会移除本次未选择的可选依赖。

先检查，再启动当前四模式模拟界面：

```bash
uv run --no-sync yam-workstation --mock --mode collect --check
uv run --no-sync yam-workstation --mock --mode collect --web-port 8766
```

浏览器访问 http://127.0.0.1:8766 。真机前按 [工作站核验](workstation.md)填写
[专用配置](../configs/station_hil.yaml)。带 `--web-port` 时先打开未连接的工作台，点击“连接设备”才构造设备，构造期间可能施力矩和校准夹爪；不带界面的CLI会在启动时连接。

RK3588正式实例包含常驻`yam-device.service`（独占四臂、相机、推理与安全仲裁）及可独立重启的`yam-workstation.service` Web/API，私有接口为`%t/yam-device.sock`，局域网入口为`http://192.168.110.140:8766`。IPC SSH用户名为`linux`，连接目标`linux@192.168.110.140`；不是开发机用户名`wuyan-lyj`，密码不入库。2026-09-17只读核查：IPC `wlan0=192.168.110.140/23`，与本机`wlp2s0=192.168.110.142/23`同连“琶洲模方”、默认网关同为`192.168.111.254`；这些网络状态下次操作仍要复查。新版设备会话在连接时另启动录制owner子进程：控制进程只向有界RAM队列提交引用，低优先级传输线程序列化三路RGB，录制进程负责NVMe暂存、HDF5与编码子进程；录制故障返回控制进程HOLD。该子进程目前随设备会话创建/结束，**不是**可单独systemd重启或在活动集内无损热升级的服务。监听LAN不会关闭Host/Origin校验。重启Web不会关闭SDK或释放机械臂，但新Web附着时先发送HOLD；重启或停止`yam-device`仍会释放硬件，必须执行真机安全流程。设备服务预设Thor `ws://192.168.250.1:8000`，启动本身保持未连接。分进程已部署IPC并完成四臂断开下90秒三相机录制/读回；运动和长时验收仍待完成，见[验收](acceptance.md#2026-09-16录制owner-ipc三相机短测)。

首次从旧单进程服务迁移需要一次有计划的力矩释放：现场支撑机械臂后安装两个unit、执行`systemctl --user daemon-reload`，停止旧`yam-workstation`，再依次启用并启动`yam-device`和`yam-workstation`。迁移完成后的页面更新只重启后者；设备代码、驱动或配置变更才重启前者。
2026-09-15曾试验按角色分区：UI/相机/桥接/预览0–3号A55、控制及四臂读数
4–5号A76、MPP编码6–7号A76。现场运动A/B发现两核控制分区的遥操作体感退化，
并在30秒运动窗口出现最大约169ms控制间隔；临时恢复旧CPU4–7掩码后的
30秒右臂运动窗口最大约42ms且操作员明确反馈顺滑。正式服务配置因此保留
设备进程`CPUAffinity=4 5 6 7`，不缩窄控制可用的四个A76核；录制传输线程、录制owner子进程、桥接线程、编码子进程和FFmpeg子进程通过`YAM_ABC_RECORDING_CPUS=0,1,2,3`、`YAM_ABC_ENCODER_CPUS=0,1,2,3`迁到0–3核。Web/API也在0–3核。该录制分区尚待现场运动验收，不能称为硬实时保证；与曾导致遥操作退化的“两核控制”方案不同。
未经新一轮现场运动验收不得在IPC重新启用两核控制方案。用户确认四臂支撑后已
通过页面断开四臂、四CAN DOWN，再重启用户服务；新主PID`29438`确认为
`CPUAffinity=4-7`，页面与设备均未连接。部署/安全重启状态
与数据完整性见[现场记录](workstation.md)。当前已是独立控制进程，但不是硬实时保证。

本机环境路径：`/home/wuyan-lyj/YAM/yam-abc-reproduce/.venv`；uv路径：
`/home/wuyan-lyj/.local/bin/uv`。这些路径描述当前开发机，不意味着已在RK3588/Thor安装完成。

旧GUI使用8042端口，属于 [保留工具](legacy_tools.md)，不作为当前HIL入口。
需要旧GUI管理总线时再按需配置sudoers；本任务不自动更改系统CAN权限。

## 验收边界

本次安装、离线验收结果见 docs/evidence/。mock 只验证软件路径，
不证明电机、相机、急停或实时控制可用。训练要选择一个后端；
uv sync 会移除本次未选择的 extras/groups。

## 数据输出依赖

当前基础依赖明确包含PyArrow和Pandas，用于会话结束后的LeRobot v3.0表格写入，PyAV用于视频。常规工作站sync仍无需安装PyTorch/完整LeRobot训练包；依赖由pyproject.toml和uv.lock固定。官方LeRobot读取验证使用独立临时CPU环境，不能当成本项目.venv安装了模型栈。

`--check` 不创建数据目录、不启动相机或电机、不连接Thor；它检查配置和依赖模块可发现性，不能证明二进制加载或硬件可用。去掉 `--mock` 后会检查真实夹爪型号与相机序列号占位；HIL/推理还需提供 `--url`。

希望一步同步并运行时：`uv run --locked --extra camera --extra gui --extra deploy yam-workstation --mock --mode collect --web-port 8766`。`.venv/bin/python` 仍是同一个环境的解释器，但教程统一通过uv启动。

## HDF5录制依赖（2026-09-11）

主依赖新增h5py（uv.lock锁定3.16.0），继续使用项目.venv及现有镜像；`uv sync --locked --extra camera --extra gui --extra deploy`。录制/转换进程使用当前Python，不需要Torch。离线官方读取验收另用临时CPU环境，版本与结果见[验收](acceptance.md)。录制进程隔离依赖Linux父进程退出信号，适用于目标RK3588/Linux与开发机Linux。转换已独立，服务器使用scripts/convert_lerobot.py及其uv锁，只安装数据处理依赖。

## RK3588 IPC 现场环境（2026-09-14）

现场 IPC 已通过网线管理链路接入：IPC `lan1=192.168.250.2/24`，本机 `enp1s0=192.168.250.1/24`，两端均不设置网关或 DNS；IPC 的 Wi-Fi `wlan0=192.168.110.140/23` 继续承担默认路由。网线 SSH 已强制绑定 `enp1s0` 验证成功，IPC 访问互联网仍经 `wlan0`。

首次 bootstrap 快照为 Ubuntu 22.04.3 LTS、6.1.118 `PREEMPT_RT`、`arm64`、Python 3.10.12，未找到 `uv`，也未在有限搜索范围内发现 YAM checkout；随后已完成 P1 的 Python 3.12/uv/YAM 部署并迁移至 NVMe，当前状态见下方 P1 与 NVMe 小节。`bcan0`～`bcan3` 已枚举但均为停止状态，Thor 地址、模型服务可达性、四臂 USB-CAN 角色和相机流仍待现场验收；三台 D405 的身份映射已完成。

详细证据：[20260914-rk3588-ipc-bootstrap.txt](evidence/20260914-rk3588-ipc-bootstrap.txt)。

Wi-Fi 配置 `琶洲模方` 已现场复核为 `connection.autoconnect=yes`，`wlan0` 当前在线；IPC 的 Gitea 专用密钥已成功认证到 `git@192.168.110.142:2222`，Gitea 身份为 `wuyan_lyj`。详细证据见 [20260914-rk3588-ipc-gitea-wifi.txt](evidence/20260914-rk3588-ipc-gitea-wifi.txt)。

正式部署按 [架构基线 P1](dagger_architecture.md#后续-agent-工作包与依赖)执行：认证成功不等于目标 YAM 仓库/子模块已 clone；优先复用项目锁文件核验 ARM64 的相机、编码、HDF5 和 i2rt 二进制依赖，不能只做模块可发现性检查。NVMe 已完成初始化并挂载到 `/data`，YAM 绝对路径为 `/data/YAM`；后续原始数据的绝对 `save_root` 仍需在运行配置中明确，不能回落 eMMC。保留 eMMC 系统盘与已登记管理网络。配置/代码可以自启动为未连接界面，不随开机自动构造机器人、开始推理或恢复上一轮运动。

## RK3588 IPC P1 部署快照（2026-09-14）

已通过 Gitea 将当前 `main` checkout 到 `/data/YAM`，提交为
`de8053219525da4f0003fa6d0ca7f4be039bbb62`，并补齐固定 `third_party/i2rt` 子模块
`5d47b358bafb30c65e397f2ece506550a0db4594`。仓库配置了专用 Gitea SSH key，后续可直接
执行 `git fetch/pull origin`。uv `0.12.13`、Python `3.12.14` 和
`uv sync --locked --extra camera --extra gui --extra deploy` 已在 ARM64 IPC 完成，锁文件
dry-run 无待变更，核心依赖导入和 mock CLI 检查通过。随后已在 NVMe 新路径重新同步 editable 环境；完整命令、版本和边界见
[P1 部署证据](evidence/20260914-rk3588-ipc-p1-deploy.txt) 和 [NVMe 迁移证据](evidence/20260914-rk3588-ipc-nvme.txt)。

主环境的 `pyrealsense2` wheel 要求 GLIBC 2.38，与 IPC Ubuntu 22.04 的 GLIBC 2.35 不兼容；
没有升级系统 glibc。已另备仅用于设备身份枚举的 Python 3.10.12 辅助环境
`/home/linux/.venv-yam-camera310`，其 RealSense 导入成功并已识别三台外接 D405。相机身份
已写入 `configs/cameras.yaml`；相机流接入 YAM 主运行环境前仍需采用与 3.12/GLIBC 2.35
兼容的绑定方案并在硬件接入后复验。

2026-09-14 P2 已按 RealSense 官方 Ubuntu Python binding 流程，在 IPC 用 tag `v2.58.3`
（commit `dfd6aa91250f5c31521d72d627865417989bb4e7`）、系统 GCC 11.4 和项目 Python 3.12.14
本地构建静态 binding。产物最高需要 GLIBC 2.34，已替换 `.venv` 中同版本但要求 GLIBC
2.38 的模块；原 wheel 模块备份在忽略的构建目录。正式项目环境现可枚举并同时采集三台
D405。重建 `.venv` 后须重新安装该本机构建产物，不能直接沿用当前上游 ARM64 wheel；
设备位于中国境内时优先从国内镜像获取固定 tag，或由开发机校验 commit 后经局域网传入。
构建、ABI 和短流结果见 [P2 相机调试证据](evidence/20260914-rk3588-p2-camera-debug.json)。

### RK3588 视频编码适配

当前板卡为RK3588 EVB7、Ubuntu 22.04、BSP 6.1 PREEMPT_RT，8核（4×A76＋4×A55）。
系统FFmpeg/PyAV没有`h264_rkmpp`，枚举到的`h264_v4l2m2m`和`h264_omx`经实际开帧探针
不可用。现已按固定commit构建Rockchip MPP和最小FFmpeg-Rockchip运行时，安装到
`/opt/yam-rkmpp`；`/etc/udev/rules.d/92-yam-rkmpp.rules`仅向`video`组开放MPP、RGA和
DMA heap节点，用户`linux`已加入`video/render`组。构建依赖新增Ubuntu `libdrm-dev`。
可复用`scripts/build_rkmpp_ffmpeg.sh`；中国境内可用`YAM_MPP_REPO`和
`YAM_FFMPEG_ROCKCHIP_REPO`指向镜像，最终commit仍会核验。

录制器默认`auto`：每集开始先做真实一帧硬编探针，通过后用三个独立`h264_rkmpp`进程，
否则回到三路并行、每路单线程libx264；`YAM_ABC_HIL_VIDEO_ENCODER`可显式固定
`h264_rkmpp`或`libx264`，显式硬编失败会报错而不静默替换。后端写入episode manifest。
真实60秒三路640×480@30硬编完整回读1795帧，队列峰值9/32；三个FFmpeg进程各约
5.4–5.7%单核CPU和10MiB RSS，温区约58–60°C。20秒端到端对照的user+sys CPU时间从
libx264的43.38秒降至MPP的26.72秒（约38%）；5秒切段得到150/150/59帧且逐路一致。
完整对照见[P2相机调试证据](evidence/20260914-rk3588-p2-camera-debug.json)。

## RK3588 IPC NVMe 与 YAM 路径（2026-09-14）

现场确认 NVMe 型号为 `TIMAR S97M8-PY 1TB SSD`、序列号 `6HJ7011000234`；整盘原先没有可识别分区或文件系统，经用户明确授权后初始化为 GPT 单分区。当前 `nvme0n1p1` 使用 ext4，标签 `yam-data`，UUID 为 `501fb615-1346-455d-9d50-61c1d113faf5`，挂载点为 `/data`，约 938 GiB 可用。

`/etc/fstab` 已登记 `UUID=501fb615-1346-455d-9d50-61c1d113faf5 /data ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2`；root 权限的 `findmnt --verify` 检查无错误或警告。YAM 从 eMMC 上的 `/home/linux/YAM` 迁移至 NVMe 的 `/data/YAM`，rsync 差异校验返回 0，旧目录已删除，不创建软链接。

现有 uv `0.12.13`（aarch64）未重装；在 `/data/YAM` 执行 `uv sync --locked --extra camera --extra gui --extra deploy` 成功，editable 包路径已更新为 `/data/YAM`。`uv lock --check`、`yam-workstation --help`、YAM 与 `i2rt` 导入均通过。为避免 uv 缓存位于 eMMC、项目环境位于 NVMe 时的硬链接告警，用户登录环境已设置 `UV_LINK_MODE=copy`。

## RK3588 IPC USB-CAN 驱动（2026-09-14）

初始现场仅能通过 USB 枚举看到四个 CANable 2.5（`1d50:606f`），内核 `6.1.118` 的 `CONFIG_CAN_GS_USB` 未启用，因而没有 `can0`～`can3`。已使用匹配的 `/usr/src/linux-headers-6.1.118` 和官方 Linux stable `v6.1.118` 的 `drivers/net/can/usb/gs_usb.c` 编译模块；模块 vermagic 为 `6.1.118 SMP preempt_rt mod_unload aarch64`。

模块已安装到 `/lib/modules/6.1.118/extra/gs_usb.ko`，执行 `depmod` 并加载成功；`/etc/modules-load.d/gs_usb.conf` 已登记 `gs_usb` 以便开机加载。当前四个 USB-backed CAN 接口已按固定 USB-Hub 下联口固化为 `can_lead_l`、`can_lead_r`、`can_left`、`can_right`，均保持 `DOWN/STOPPED`。适配器序列号仅作为审计信息，机械臂本体 S/N 不参与映射；该规则不替代四个机械臂接线/端接验收。详细证据见 [gs_usb 修复证据](evidence/20260914-rk3588-ipc-gs-usb.txt) 和 [CAN端口命名证据](evidence/20260914-rk3588-ipc-can-port-names.txt)。

## RK3588 IPC D405 相机身份与稳定入口（2026-09-14）

三台 D405 已按用户确认的物理标签与 RealSense API 拔插复核完成角色登记：`right=260422271123`、`top=260522275397`、`left=260522271298`。`/data/YAM/configs/cameras.yaml` 已使用这些 RealSense S/N。IPC 已应用 `/etc/udev/rules.d/91-yam-cameras.rules`，当前稳定入口为 `/dev/yam-camera-right`→`video12`、`/dev/yam-camera-top`→`video6`、`/dev/yam-camera-left`→`video0`；入口只作为 UVC 便利路径，应用层仍按 RealSense S/N 选择设备。2026-09-14 15:54 复核时三台相机均为 USB 3.2/5000M，三台 UVC 父设备均暴露稳定内部序列号，规则不依赖 `videoN` 或当前物理路径。此步骤未启动相机采集或任何电机/CAN动作。

## 构建失败的检查与重试条件

2026-09-07系统Python构建失败的 [诊断摘录](evidence/20260907-system-python-build-failure.txt) 记录退出1、缺失patchlevel.h/Development.Module，独立检查Python.h不存在；它不是完整构建日志。
原因假设为选用的系统Python缺开发头文件。后续改用uv管理的3.12.14，[环境审计](evidence/20260907-environment-audit.json) 记录安装退出0及ruckig导入，但同时改变了Python分发/补丁版本，不能声称只补头文件的单变量因果已验证。

重试前先用所选解释器的 `sysconfig.get_path('include')` 定位并检查Python.h，核对编译器、Python版本和所用锁文件。路径/头文件/解释器选择变化后才有理由重试原构建；不要在相同缺失条件下重复安装。当前机器头文件和构建条件本次未重测，均为未知。可复用安装流程见“首次安装”，历史范围见 [尝试记录](cache/records/system-python-attempt-20260907.json)。

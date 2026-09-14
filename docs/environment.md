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

实测目标环境为 Ubuntu 22.04.3 LTS、6.1.118 `PREEMPT_RT`、`arm64`、Python 3.10.12；未找到 `uv`，也未在有限搜索范围内发现 YAM checkout。项目当前规范要求 Python 3.12/uv，因此 IPC 尚未达到直接运行当前 YAM 工作站入口的部署条件。`bcan0`～`bcan3` 已枚举但均为停止状态，Thor 地址、模型服务可达性、板载或 USB-CAN 角色和相机映射仍待现场配置与验收。

详细证据：[20260914-rk3588-ipc-bootstrap.txt](evidence/20260914-rk3588-ipc-bootstrap.txt)。

Wi-Fi 配置 `琶洲模方` 已现场复核为 `connection.autoconnect=yes`，`wlan0` 当前在线；IPC 的 Gitea 专用密钥已成功认证到 `git@192.168.110.142:2222`，Gitea 身份为 `wuyan_lyj`。详细证据见 [20260914-rk3588-ipc-gitea-wifi.txt](evidence/20260914-rk3588-ipc-gitea-wifi.txt)。

正式部署按 [架构基线 P1](dagger_architecture.md#后续-agent-工作包与依赖)执行：认证成功不等于目标 YAM 仓库/子模块已 clone；优先复用项目锁文件核验 ARM64 的相机、编码、HDF5 和 i2rt 二进制依赖，不能只做模块可发现性检查。NVMe 挂载和绝对 `save_root` 尚未确定，先盘点已有内容，不直接格式化；保留 eMMC 系统盘与已登记管理网络。配置/代码可以自启动为未连接界面，不随开机自动构造机器人、开始推理或恢复上一轮运动。

## RK3588 IPC P1 部署快照（2026-09-14）

已通过 Gitea 将当前 `main` checkout 到 `/home/linux/YAM`，提交为
`de8053219525da4f0003fa6d0ca7f4be039bbb62`，并补齐固定 `third_party/i2rt` 子模块
`5d47b358bafb30c65e397f2ece506550a0db4594`。仓库配置了专用 Gitea SSH key，后续可直接
执行 `git fetch/pull origin`。uv `0.12.13`、Python `3.12.14` 和
`uv sync --locked --extra camera --extra gui --extra deploy` 已在 ARM64 IPC 完成，锁文件
dry-run 无待变更，核心依赖导入和 mock CLI 检查通过。完整命令、版本和边界见
[P1 部署证据](evidence/20260914-rk3588-ipc-p1-deploy.txt)。

主环境的 `pyrealsense2` wheel 要求 GLIBC 2.38，与 IPC Ubuntu 22.04 的 GLIBC 2.35 不兼容；
没有升级系统 glibc。已另备仅用于设备身份枚举的 Python 3.10.12 辅助环境
`/home/linux/.venv-yam-camera310`，其 RealSense 导入成功，但本次枚举没有发现外接 D405、
USB-CAN 或视频节点。相机流接入 YAM 主运行环境前仍需采用与 3.12/GLIBC 2.35 兼容的绑定
方案并在硬件接入后复验。

## 构建失败的检查与重试条件

2026-09-07系统Python构建失败的 [诊断摘录](evidence/20260907-system-python-build-failure.txt) 记录退出1、缺失patchlevel.h/Development.Module，独立检查Python.h不存在；它不是完整构建日志。
原因假设为选用的系统Python缺开发头文件。后续改用uv管理的3.12.14，[环境审计](evidence/20260907-environment-audit.json) 记录安装退出0及ruckig导入，但同时改变了Python分发/补丁版本，不能声称只补头文件的单变量因果已验证。

重试前先用所选解释器的 `sysconfig.get_path('include')` 定位并检查Python.h，核对编译器、Python版本和所用锁文件。路径/头文件/解释器选择变化后才有理由重试原构建；不要在相同缺失条件下重复安装。当前机器头文件和构建条件本次未重测，均为未知。可复用安装流程见“首次安装”，历史范围见 [尝试记录](cache/records/system-python-attempt-20260907.json)。

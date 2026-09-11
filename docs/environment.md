# 中国工作站环境复现

## 项目与依赖

主仓库为 YAM-ABC-Reproduce 的本地工作站分支，保留上游历史。
origin 是 `ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git`；
upstream 是 `https://github.com/i2rt-robotics/yam-abc-reproduce.git`。
i2rt 使用 `third_party/i2rt` 固定提交，不依赖工作区同级 i2rt 目录。

采用 Python 3.12 和 uv；Git 跟踪 pyproject.toml、uv.lock、.python-version，
忽略 .venv。采集不需要安装训练后端。

## 首次安装

```bash
git clone --recurse-submodules ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git
cd YAM
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

主依赖新增h5py（uv.lock锁定3.16.0），继续使用项目.venv及现有镜像；`uv sync --locked --extra camera --extra gui --extra deploy`。录制/转换进程使用当前Python，不需要Torch。离线官方读取验收另用临时CPU环境，版本与结果见[验收](acceptance.md)。新进程隔离依赖Linux的文件锁、SIGSTOP/SIGCONT及父进程退出信号，适用于目标RK3588/Linux与开发机Linux。

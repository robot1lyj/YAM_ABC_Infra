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
uv sync --locked --extra camera --extra gui
source .venv/bin/activate
python -c "import i2rt, yam_abc_reproduce, cv2, pyrealsense2, av; print('imports OK')"
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

```bash
source .venv/bin/activate
yam-abc-gui --host 127.0.0.1
```

浏览器访问 http://127.0.0.1:8042 。首次真机启动前完成 docs/workstation.md
的型号、CAN、安装核验；当前默认 station 仍是上游 GELLO 示例。
需要 GUI 管理总线时再执行 `sudo bash scripts/setup_can_sudoers.sh`。
本次环境安装不自动改系统 CAN 权限、不启动电机。

## 验收边界

本次安装、离线验收结果见 docs/evidence/。mock 只验证软件路径，
不证明电机、相机、急停或实时控制可用。训练要选择一个后端；
uv sync 会移除本次未选择的 extras/groups。

# YAM 交互架构图

[打开架构图](yam.html) · [可编辑规格](yam.architecture.json) · [交付校验回执](yam.delivery.json) · [浏览器检查](yam.visual-check.json) · [源码快照](yam.sources.json)

使用 Archify 2.17 生成的自包含 HTML，可离线查看、搜索节点、追踪连接、切换亮暗主题及导出图像。类型为 `architecture`。本图是系统总览，精细状态转换仍以 [架构规范](../dagger_architecture.md) 和源码为准。

## 范围与证据

2026-09-22 核查当前工作区。仓库存在其他任务的未提交修改；本次只新增图和说明，不将那些修改纳入提交。图中 SRC 是基线提交 `76b6834d947475444a3d87503da15392745a11b9` 的源码定位，采用 `local-only` 模式。Archify 核验的是该提交中的文件存在性；当前工作区相关文件的独立哈希记在 `yam.sources.json`，不能把 SRC 当作未提交代码的验证。

| 图中关系 | 核查来源与解释 |
|---|---|
| 浏览器 → Web/API → Runtime | `hil/web.py`、`hil/workbench.py`、`deploy/yam-workstation.service`；Web 独立服务通过设备 Unix socket 请求控制 |
| Runtime → 四臂 | `hil/run.py`、`hil/station.py`；默认直接装配 StationIO 和 i2rt，反馈也回到设备侧；箭头突出目标下发 |
| D405 → Runtime | `hil/observation.py`；三路图像配对与状态对齐，摄像头通过设备侧 worker 读取 |
| Runtime → 策略/规划 → Thor | `hil/run.py`、`hil/policy_process.py`、`hil/planner_process.py`；图中合并表示两个独立子进程；观测发往模型，动作和计划返回 Runtime |
| Runtime → 录制 → 原始集 | `hil/recording_service.py`、`hil/recording.py`；进程隔离、队列传输、MP4/HDF5/JSON 落盘 |
| 原始集 → 数据集平台 | `dataset_workbench/web.py`、`dataset_workbench/catalog.py`；关闭后显式导入、审核、转换，SQLite 目录索引不在主图展开 |

IPC 包含 Web、Runtime、策略通信/规划和录制；Thor 是外部 condapi 模型服务；数据集平台部署在工作站或服务器。两平台是独立浏览器入口，主图的操作员节点只画设备操作链路，数据平台入口由其自身节点代表。

主图对应默认 `yam-device.service` 装配。`--executor-socket` 可选路径由 `RemoteStationIO` 经 Unix socket 连接常驻 SDK owner，未在总览中展开。非 RTC 和 RTC 的内部时间线也未展开。不将文档中的早期规划或新增源码视为现场已部署事实；本任务没有连接硬件或验证模型服务。:8766、:8767、:8000 表示服务约定端口，不证明网络当前可达。

## 重建

在仓库根目录运行，`ARCHIFY` 指向本机安装目录：

```sh
ARCHIFY="$HOME/.codex/skills/archify"
node "$ARCHIFY/bin/archify.mjs" validate architecture docs/architecture/yam.architecture.json --repo-root . --quality showcase --json
node "$ARCHIFY/bin/archify.mjs" deliver architecture docs/architecture/yam.architecture.json docs/architecture/yam.html --repo-root . --quality showcase --json > docs/architecture/yam.delivery.json
node "$ARCHIFY/bin/archify.mjs" visual-check docs/architecture/yam.html --json
```

自包含 HTML 不依赖已安装的 skill。修改 JSON 后需要重新验证、交付和视觉检查；成功的旧回执不能证明新文件通过。

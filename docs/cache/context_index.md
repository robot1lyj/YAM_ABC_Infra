# 项目记忆路由

先明确本次任务和缺失事实，再读相关摘要与对应规范章节；必要时展开原文，足以支持下一步就停止检索。完整资料保留，不默认读取所有摘要或全部历史，无默认累计读取额度。下面的“规范所有者”是当前入口；历史英文归档和旧工具不进入默认检索。
本表由人工维护，`scripts/check_project_memory.py` 检查其链接和记录指纹，不自动判断语义正确性。

| 任务 | 规范所有者 | 何时补充读取 |
|---|---|---|
| 项目主题、已接受的取舍 | [项目决策](../decisions.md) | 用户更改范围时 |
| 硬件到齐开工、RK3588 IPC/Thor分工、agent交接 | [架构基线v1与P0–P8工作包](../dagger_architecture.md#2026-09-14-架构基线-v1) | 再按包读硬件/环境/模型契约；设计不等于已部署 |
| 安装、uv、镜像、代码托管 | [环境](../environment.md) | 当前机器状态需重新检查 |
| 设备、CAN、序列号、初始化 | [硬件事实](../workstation.md) | 真机动作前现场核验 |
| 四模式使用、配置、操作 | [运行手册](../hil_quickstart.md) | 对照实际配置和CLI帮助 |
| 接管、状态机、模块职责 | [架构](../dagger_architecture.md) | 修改核心/运行循环时 |
| D405配对、多臂同步、性能 | [同步设计](../synchronization_design.md) | 先看实测，再决定升级 |
| Thor/condapi模型接口 | [模型契约](../condapi_interface.md) | 再读condapi对应章节，不全量搬库 |
| 示范采集、双按钮、片段管理 | [采集手册](../collect.md) | 操作与事件仲裁变更时 |
| 数据集导入、清洗、分类、合并界面 | [数据集工作台](../dataset_workbench.md) | 索引、检查和离线任务变更时 |
| 专家导出、格式转换 | [转换手册](../convert.md) | 核对具体episode和转换产物 |
| 软件/真机验收与下一步 | [验收边界](../acceptance.md) | 具体证据和现场状态 |
| 外部参考与源码来源 | [参考记录](../reference_sources.md) | 依赖源变更时重新核对 |
| 记忆维护 | [记忆规则](../memory.md) | 证据/记录验证工具 |
| 上游兼容工具 | [保留工具](../legacy_tools.md) | 明确需要旧功能才读 |

[稳定摘要](kernel.md)只投影上述事实；[续作检查点](checkpoint.md)只记最近完成范围和下一步。
检查点不能把未验证方案提升为事实。设备在线、进程、网络、模型版本不从旧检查点直接继承。

按需证据放在 `docs/evidence/`，带范围的记录放在 `docs/cache/records/`。
所有路径相对本仓库；外部大模型/数据仅记录身份与引用，不复制到记忆目录。

## 问题与行动路由

先选一行，按缺口只展开相应检查或尝试。命令在规范所有者处，索引不复制完整操作流程。

| 问题/关键词 | 检查方法及前提 | 历史尝试 / 可复用工具 |
|---|---|---|
| IPC开工、USB Hub/USB-CAN固定下联口、D405序列号与稳定入口、Thor服务、模型单位/action_dt | [设备身份登记P0](../workstation.md#第一步设备身份登记-p0)、[架构工作包](../dagger_architecture.md#后续-agent-工作包与依赖)、[接口合同](../condapi_interface.md#v1-接口验收合同) | [USB-CAN端口命名证据](../evidence/20260914-rk3588-ipc-can-port-names.txt)、[相机身份证据](../evidence/20260914-rk3588-ipc-cameras.txt)、[本地核查证据](../evidence/20260914-architecture-audit.json)、[规划记录](records/architecture-baseline-20260914.json)；现场状态需复核 |
| IPC NVMe挂载、YAM迁移、uv重同步 | [环境](../environment.md) | [NVMe现场证据](../evidence/20260914-rk3588-ipc-nvme.txt)、[NVMe记录](records/rk3588-ipc-nvme-20260914.json)；磁盘、重刷或路径变更后现场复查 |
| IPC gs_usb驱动、USB-CAN枚举、P0角色识别 | [硬件事实](../workstation.md)、[环境](../environment.md) | [gs_usb修复证据](../evidence/20260914-rk3588-ipc-gs-usb.txt)；驱动/内核或USB拓扑变更后现场复查 |
| 数据集检查、坏集、多task合并 | [检查和清洗](../dataset_workbench.md#检查和清洗)、[配置与实测](../dataset_workbench.md#配置预期与实测边界) | [工作台能力记录](records/dataset-workbench-capability-20260911.json)、[入口和验收](../dataset_workbench.md#可复用工具与验证) |
| 转换失败、partial、恢复、帧率不一致 | [转换工具条件](../convert.md#可复用工具与重试条件)、[异常恢复](../convert.md#异常恢复) | [离线转换验收](../acceptance.md#2026-09-11后续采集与转换完全分离)；具体新失败的原因和结果从conversion_report.json或.partial/failure.json读取，未取得日志时标未知 |
| Python.h、ruckig、构建失败 | [环境排查与重试](../environment.md#构建失败的检查与重试条件) | [失败尝试记录](records/system-python-attempt-20260907.json)、[首次安装](../environment.md#首次安装) |
| 测试互斥、BlockingIOError | [历史目录隔离尝试](../acceptance.md#历史尝试测试目录互斥) | [当前测试](../../tests/test_workbench.py)；旧自动转换队列已退出主线，不能直接照搬旧修复 |
| 记忆找不到工具、旧额度、证据失效 | [工程经验与问题检索](../memory.md#工程经验与问题检索)、[计量边界](../memory.md#选择性检索与计量边界) | [检索工具](../../scripts/memory_gate.py)、[检查工具](../../scripts/check_project_memory.py)；证据失效先核查，不自动更新哈希 |

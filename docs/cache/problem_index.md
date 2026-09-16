# 问题与行动路由

只在具体排查或检索经验时读取；默认入口为 [context_index](context_index.md)。本页仅做二级导航，事实仍由链接的owner持有。


先选一行，按缺口只展开相应检查或尝试。命令在规范所有者处，索引不复制完整操作流程。

| 问题/关键词 | 检查方法及前提 | 历史尝试 / 可复用工具 |
|---|---|---|
| 项目日报、阶段进度、对外汇报 | [日报目录](../reports/)；台账/日报/周报统一按“项目名称、已完成工作、当前问题、下一步计划”；不用内部阶段编号和相对日期，按来源版本区分已完成与待验收 | [2026-09-15汇报](../reports/2026-09-15-daily.md)、[9月15日来源快照](../reports/2026-09-15-daily-sources.json)、[2026-09-14日报](../reports/2026-09-14-daily.md)；日报不替代现场当前状态 |
| IPC开工、USB Hub/USB-CAN固定下联口、D405序列号与稳定入口、Thor服务、模型单位/action_dt | [设备身份登记P0](../workstation.md#第一步设备身份登记-p0)、[架构工作包](../dagger_architecture.md#后续-agent-工作包与依赖)、[接口合同](../condapi_interface.md#v1-接口验收合同) | [USB-CAN端口命名证据](../evidence/20260914-rk3588-ipc-can-port-names.txt)、[相机身份证据](../evidence/20260914-rk3588-ipc-cameras.txt)、[本地核查证据](../evidence/20260914-architecture-audit.json)、[规划记录](records/architecture-baseline-20260914.json)；现场状态需复核 |
| IPC NVMe挂载、YAM迁移、uv重同步 | [环境](../environment.md) | [NVMe现场证据](../evidence/20260914-rk3588-ipc-nvme.txt)、[NVMe记录](records/rk3588-ipc-nvme-20260914.json)；磁盘、重刷或路径变更后现场复查 |
| IPC gs_usb驱动、USB-CAN枚举、P0角色识别 | [硬件事实](../workstation.md)、[环境](../environment.md) | [gs_usb修复证据](../evidence/20260914-rk3588-ipc-gs-usb.txt)；驱动/内核或USB拓扑变更后现场复查 |
| 数据集检查、坏集、多task合并 | [检查和清洗](../dataset_workbench.md#检查和清洗)、[配置与实测](../dataset_workbench.md#配置预期与实测边界) | [工作台能力记录](records/dataset-workbench-capability-20260911.json)、[入口和验收](../dataset_workbench.md#可复用工具与验证) |
| 转换失败、partial、恢复、帧率不一致 | [转换工具条件](../convert.md#可复用工具与重试条件)、[异常恢复](../convert.md#异常恢复) | [离线转换验收](../acceptance.md#2026-09-11后续采集与转换完全分离)；具体新失败的原因和结果从conversion_report.json或.partial/failure.json读取，未取得日志时标未知 |
| Python.h、ruckig、构建失败 | [环境排查与重试](../environment.md#构建失败的检查与重试条件) | [失败尝试记录](records/system-python-attempt-20260907.json)、[首次安装](../environment.md#首次安装) |
| 测试互斥、BlockingIOError | [历史目录隔离尝试](../acceptance.md#历史尝试测试目录互斥) | [当前测试](../../tests/test_workbench.py)；旧自动转换队列已退出主线，不能直接照搬旧修复 |
| 记忆找不到工具、旧额度、证据失效 | [工程经验与问题检索](../memory.md#工程经验与问题检索)、[计量边界](../memory.md#选择性检索与计量边界) | [检索工具](../../scripts/memory_gate.py)、[检查工具](../../scripts/check_project_memory.py)；证据失效先核查，不自动更新哈希 |

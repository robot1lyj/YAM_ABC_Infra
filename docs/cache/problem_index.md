# 问题与行动路由

只在具体排查或检索经验时读取；默认入口为 [context_index](context_index.md)。本页仅做二级导航，事实仍由链接的owner持有。


先选一行，按缺口只展开相应检查或尝试。命令在规范所有者处，索引不复制完整操作流程。

| 问题/关键词 | 检查方法及前提 | 历史尝试 / 可复用工具 |
|---|---|---|
| SSH登录被拒、IPC用户名 | [环境](../environment.md#日常操作)：linux@192.168.110.140；不是开发机用户，不在记忆存密码 | 网络/IP/凭据可用性现场复查，不连续猜用户名 |
| 重载模型为什么断臂、页面错误锁死全部操作 | [故障分域](../dagger_architecture.md#故障等级与恢复合同)、[重启边界](../deploy.md#改什么重启什么) | [本地恢复证据](../evidence/20260922-architecture-recovery.json)；默认SDK仍属device，executor未迁移 |
| episode queue full、录制owner退出、掉盘 | [记录与恢复](../dagger_architecture.md#记录恢复与数据身份)、[字段](../hil_dataset_fields.md) | 保留失败原件；后台新session恢复不等于救回全部RAM；NVMe物理根因未知 |
| HIL手感重、相对映射、按键无效 | [共用人工实现](../collect.md#遥操作与-hil-共用实现) | 绝对1:1、右①解锁、人工①锁定、页面交还；SDK补偿与实际增益分别检查 |
| RTC承诺改变、迟到、同步末步被裁 | [三模式合同](../synchronization_design.md#三种动作时间合同)、[RTC](../condapi_interface.md#训练式-rtc) | 不混用同步/TDA/RTC期限，不用重载策略清SDK故障 |
| 删等待后时间不连续、摘要混集 | [等待段及训练边界](../hil_dataset_fields.md#等待段及训练边界) | 原始tick/time保留；frame_index/fps连续；两个等待区间按集清空 |
| 项目日报、阶段进度、对外汇报 | [日报目录](../reports/)；台账/日报/周报统一按“项目名称、已完成工作、当前问题、下一步计划”；不用内部阶段编号和相对日期，按来源版本区分已完成与待验收 | [2026-09-17汇报](../reports/2026-09-17-daily.md)、[9月17日来源快照](../reports/2026-09-17-daily-sources.json)、[2026-09-16汇报](../reports/2026-09-16-daily.md)；日报不替代现场当前状态 |
| IPC开工、USB Hub/USB-CAN固定下联口、D405序列号与稳定入口、Thor服务、模型单位/action_dt | [设备身份登记P0](../workstation.md#第一步设备身份登记-p0)、[架构工作包](../dagger_architecture.md#2026-09-14-架构基线-v1)、[接口合同](../condapi_interface.md#v1-接口验收合同) | [USB-CAN端口命名证据](../evidence/20260914-rk3588-ipc-can-port-names.txt)、[相机身份证据](../evidence/20260914-rk3588-ipc-cameras.txt)、[本地核查证据](../evidence/20260914-architecture-audit.json)、[规划记录](records/architecture-baseline-20260914.json)；现场状态需复核 |
| IPC NVMe挂载、YAM迁移、uv重同步 | [环境](../environment.md) | [NVMe现场证据](../evidence/20260914-rk3588-ipc-nvme.txt)、[NVMe记录](records/rk3588-ipc-nvme-20260914.json)；磁盘、重刷或路径变更后现场复查 |
| IPC gs_usb驱动、USB-CAN枚举、P0角色识别 | [硬件事实](../workstation.md)、[环境](../environment.md) | [gs_usb修复证据](../evidence/20260914-rk3588-ipc-gs-usb.txt)；驱动/内核或USB拓扑变更后现场复查 |
| 数据集检查、坏集、多task合并 | [检查和清洗](../dataset_workbench.md#检查和清洗)、[配置与实测](../dataset_workbench.md#配置预期与实测边界) | [工作台能力记录](records/dataset-workbench-capability-20260911.json)、[入口和验收](../dataset_workbench.md#可复用工具与验证) |
| 转换失败、partial、恢复、帧率不一致 | [转换工具条件](../convert.md#可复用工具与重试条件)、[异常恢复](../convert.md#异常恢复) | [离线转换验收](../archive/acceptance_20260922.md#2026-09-11后续采集与转换完全分离)；具体新失败的原因和结果从conversion_report.json或.partial/failure.json读取，未取得日志时标未知 |
| Python.h、ruckig、构建失败 | [环境排查与重试](../environment.md#构建失败的检查与重试条件) | [失败尝试记录](records/system-python-attempt-20260907.json)、[首次安装](../environment.md#首次安装) |
| 测试互斥、BlockingIOError | [历史目录隔离尝试](../archive/acceptance_20260922.md#历史尝试测试目录互斥) | [当前测试](../../tests/test_workbench.py)；旧自动转换队列已退出主线，不能直接照搬旧修复 |
| 记忆找不到工具、旧额度、证据失效 | [工程经验与问题检索](../memory.md#工程经验与问题检索)、[计量边界](../memory.md#选择性检索与计量边界) | [检索工具](../../scripts/memory_gate.py)、[检查工具](../../scripts/check_project_memory.py)；证据失效先核查，不自动更新哈希 |

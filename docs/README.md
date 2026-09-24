# 文档导航

当前文档按职责维护，不按开发日期堆叠。**源码已实现、已部署、现场通过是三件事**；从当前任务对应的owner入手，现场使用时再核验验收与实时状态，续作才读检查点。

## 使用与维护

| 要做什么 | 唯一主要入口 |
|---|---|
| 连接、四模式、回零、故障恢复 | [运行手册](hil_quickstart.md) |
| 手柄、HIL切换、任务与episode | [采集操作](collect.md) |
| CAN、相机身份、夹爪行程、官方SDK | [工作站](workstation.md) |
| 安装、账号、服务、系统盘/NVMe迁移 | [环境](environment.md)、[部署边界](deploy.md) |
| 数据字段、删等待后的时间 | [原始字段合同](hil_dataset_fields.md) |
| 数据审阅、清洗、分类、转换 | [数据集工作台](dataset_workbench.md)、[转换](convert.md) |

## 开发与交接

| 要确认什么 | 所有者 |
|---|---|
| 模块分层、资源拥有者、局部失败恢复 | [架构](dagger_architecture.md) |
| 同步50步、TDA、RTC及Thor合同 | [接口](condapi_interface.md)、[同步与性能](synchronization_design.md) |
| 用户已接受的约束 | [决策](decisions.md) |
| 什么测过、什么未部署 | [验收状态](acceptance.md)；跨回合续作才读[检查点](cache/checkpoint.md) |
| 怎么检索/维护项目记忆 | [记忆规则](memory.md)、[任务路由](cache/context_index.md) |
| 官方来源与旧入口 | [来源](reference_sources.md)、[legacy工具](legacy_tools.md) |

当前架构图是源码快照，不是在线拓扑：[交互图](architecture/yam.html) / [范围与生成依据](architecture/README.md)。

## 文档维护约定

- 当前手册只保留生效合同；独有历史移至archive，原始测量保留evidence，不重写旧哈希冒充复验。
- 同一事实由一份owner负责，其余页面用链接；参数以配置/代码核对，现场状态重新检查。
- 修改公共数据合同同时核对采集、回读、转换；修改生命周期同时核对恢复入口和部署影响。
- 旧日期章节、报告和归档只证明当时状态，不能覆盖当前手册；未确认的根因写未知。
- 写回后运行 `python3 scripts/check_project_memory.py`，检查文件/章节链接、热文件预算与记录证据，再做语义复核。自动检查不能证明控制安全或文档与现场一致。

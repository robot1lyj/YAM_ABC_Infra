# 项目记忆路由

先定任务和缺失事实，从下表选一个owner的相关章节；证据不足再展开，不默认读kernel、checkpoint、历史或所有链接。[完整导航](../README.md)仅按需查看。检查器只验证链接、预算和指纹，不判断语义。

| 任务 | 规范所有者 | 何时补充读取 |
|---|---|---|
| 决策、产品、进程与恢复 | [决策](../decisions.md)、[架构](../dagger_architecture.md) | 部署状态另查验收 |
| 环境、设备、CAN、相机 | [环境](../environment.md)、[硬件](../workstation.md) | 地址/设备状态现场复核 |
| 四模式使用、配置、操作 | [运行手册](../hil_quickstart.md) | 对照实际配置和CLI帮助 |
| D405配对、多臂同步、性能 | [同步设计](../synchronization_design.md) | 先看实测，再决定升级 |
| Thor/condapi模型接口 | [模型契约](../condapi_interface.md) | 再读condapi对应章节，不全量搬库 |
| 设备与采集平台、双按钮、片段管理 | [采集手册](../collect.md) | 操作与事件仲裁变更时 |
| 数据集导入、清洗、分类、合并界面 | [数据集工作台](../dataset_workbench.md) | 索引、检查和离线任务变更时 |
| 专家导出、格式转换 | [转换手册](../convert.md) | 核对具体episode和转换产物 |
| HIL原始字段、删等待、训练与物理时间 | [数据合同](../hil_dataset_fields.md) | 不把policy目标/提交目标/反馈混用 |
| 更新哪个进程、是否掉使能 | [部署边界](../deploy.md) | executor迁移与现场许可须复核 |
| 软件/真机验收 | [验收边界](../acceptance.md) | 证据不自动成为现场状态 |
| 外部参考与源码来源 | [参考记录](../reference_sources.md) | 依赖源变更时重新核对 |
| 记忆维护 | [记忆规则](../memory.md) | 证据/记录验证工具 |
| 上游兼容工具 | [保留工具](../legacy_tools.md) | 明确需要旧功能才读 |

[稳定摘要](kernel.md)只供概览；[续作检查点](checkpoint.md)只供续作。在线状态、地址和版本均需复查。

证据在 `docs/evidence/`，结构化记录在 `docs/cache/records/`，均不默认展开。

## 问题与行动路由

具体故障、USB设备、迁移、转换和历史尝试从 [问题与行动路由](problem_index.md) 按关键词查找；不默认展开整表或全部records。

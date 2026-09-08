# 上下文路由

只选择当前决策所需的文档，通过 memory_gate 有界读取。

| 模式 | 规范所有者 | 按需证据 |
|---|---|---|
| 环境安装/复现 | docs/environment.md | docs/evidence/ |
| 三模式/HIL架构 | docs/dagger_architecture.md | docs/condapi_interface.md |
| 真机初始化 | docs/workstation.md | configs/ 与现场重新检查 |
| 采集 | docs/collect.md + docs/workstation.md | 具体 episode，勿全量读取数据 |
| 训练/评估 | docs/training.md | 具体运行 manifest、数据及模型哈希 |
| 部署 | docs/deploy.md + docs/workstation.md | 对应模型验收，需现场状态 |
| 记忆维护 | docs/memory.md | docs/cache/records/ |

短期续作读取 `docs/cache/checkpoint.md`；稳定摘要读取 `docs/cache/kernel.md`。
规范文档优先于摘要，当前版本证据优先于历史经验。

- Kai0非RTC优化、UMI/Diffusion Policy同步与本地边缘部署：`docs/synchronization_design.md`。

- 当前三模式运行、配置、接管按键、模拟测试与专家导出：`docs/hil_quickstart.md`。

# 项目记忆维护

本项目使用 mlops-memory 的文档所有权、按需检索和离线证据机制。没有外部向量库、W&B或独立的第二套记忆仓库。

## 信息层次

| 层 | 路径 | 用途 |
|---|---|---|
| 工作规则 | `AGENTS.md` | 授权边界、默认入口、维护约定 |
| 路由 | [context_index](cache/context_index.md) | 按任务选择规范所有者 |
| 当前规范 | 路由表中的中文文档 | 硬件/环境/架构/模型契约等各自维护 |
| 稳定摘要 | [kernel](cache/kernel.md) | 只引用稳定结论与来源，不承载完整历史 |
| 最近进度 | [checkpoint](cache/checkpoint.md) | 完成范围、未完成项、下一步 |
| 带证据记录 | `docs/cache/records/*.json` | 范围、状态、依赖指纹、验收引用 |
| 原始证据 | `docs/evidence/` 或运行产物目录 | 保留命令、结果与来源身份 |
| 归档 | `docs/archive/` | 明确需要历史时才读取 |

事实冲突时：遵守当前用户约定；检查当前代码/配置和现场证据；更新规范所有者；最后刷新摘要。参考库的历史性能和旧GUI说明不能覆盖当前契约。

## 开始任务

1. 读路由和短检查点，选择本次任务需要的一个或少数规范章节。
2. 明确项目、平台、代码/模型契约和证据时间；需要现场状态就重新检查。
3. 当前记录须通过scope、evidence和depends_on验证；过期、candidate或归档只用于审阅。
4. 不为“记住整个项目”全量读取历史、数据或第三方仓库。

## 上下文预算：用户覆盖优先

用户已明确放宽记忆字节预算，避免预算阻断协作。原12,288字节/包、32,768字节/上下文仅作诊断参考。

- 同一实际上下文保留原ledger，不重置来伪造新额度。
- 达到诊断限额时保存短检查点，继续按需读取；记录为直接读取，不能声称旧ledger覆盖了这些读取量。
- 只对可见、允许记录的项目文档记账，不复制隐藏指令、凭证或完整环境变量。
- 写检查点不等于自动压缩。宿主是否压缩由宿主决定，本项目没有强制宿主压缩或修改完整上下文上限的接口。
- 字节不是token；未接入完整请求和实际tokenizer时，不声称已统计隐藏/系统/完整历史上下文。

`memory_gate.py`保留原始严格诊断行为；拒绝pack不意味着用户要求暂停任务。与预算不同，证据哈希/范围不匹配不能通过放宽预算绕过。

可选诊断命令（仅在实际新上下文初始化；SESSION替换为实际唯一标识）：

```bash
python3 scripts/memory_gate.py init --root . --session SESSION --preloaded AGENTS.md docs/cache/context_index.md
python3 scripts/memory_gate.py pack --root . --session SESSION --required 'docs/hil_quickstart.md#操作规则'
python3 scripts/memory_gate.py audit --root . --session SESSION
```

Ledger位于被忽略的 `docs/cache/runtime/`，不提交Git。当前上下文已有ledger就沿用，不重复运行init。

## 更新任务结果

1. 新建证据，不改写旧日志使其“继续通过”；未知项明确说明。
2. 更新相应规范所有者，描述已实现/已验证/待现场验收，不保留相互冲突的顶部补丁。
3. 必要时新增record，字段为id、kind、claim、owner、scope、status、时间、evidence、recheck、valid_until、depends_on、supersedes。
4. 实测结论只在对应范围内verified；设计建议是candidate；失效结论为stale或superseded，旧证据保留。
5. 更新kernel的来源投影、checkpoint的下一步，不重复完整实现细节。
6. 运行下面的检查，审阅diff后提交。授权明确时按现有远端推送，不需要新建外部记忆服务。

```bash
python3 scripts/check_project_memory.py
python3 scripts/memory_gate.py validate-record --root . --record docs/cache/records/hil-runtime-20260908-offline.json
```

## 检查工具负责什么

`check_project_memory.py` 检查当前路由与中文入口的文件链接、record结构、证据与依赖指纹、重复ID以及规范owner是否在路由中。verified记录失效返回非零，提示重新验证；非current的历史记录单独列出，不能当作通过当前验收。

它不修复内容、不自动提升candidate、不运行机械臂或模型，也不验证网页可达性、Markdown锚点、记录的语义真伪或完整上下文token。

复用历史记录时仍须使用所有scope键做匹配；`validate-record`只校验本地结构和指纹，不替代实际任务的scope判断。

## 训练与部署记录

训练/转换/部署应记录代码提交、未提交变更指纹、命令、数据划分、norm/模型身份、配置、产物与验收范围。
condapi拥有训练事实，本仓库只保存接口依赖和读取快照。当前HIL manifest还没有完整自动采集这条模型来源链，不能在文档中描述成已经实现。

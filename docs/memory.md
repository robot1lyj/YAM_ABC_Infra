# 项目记忆的使用

本项目采用 mlops-memory 的单一规范所有者、有界检索、离线证据机制。
不依赖 W&B、向量数据库或网络记忆服务。

## 2026-09-08 用户覆盖：预算改为诊断参考

用户要求自动管理上下文、放宽硬限制，不因记忆字节预算打断协作。
因此本项目将工具原默认额度作为诊断，不作为工作暂停条件。
仍按任务选取规范章节，避免全量重复读取；保留已用 ledger，不重置来伪装额度。
超出工具额度后的按需读取记录为用户授权的直接读取；旧 ledger 不代表全部后续读取量。
保存检查点不等于宿主已压缩；当前无可调用的宿主自动压缩工具。
下方 gate 命令可用于诊断和证据验证，不要求用户操作会话来通过 gate。

## 开始一个实际新上下文

在仓库根目录执行；SESSION 换成该实际上下文的唯一名字。
`--preloaded` 要列出已经注入的所有指令/记忆，不能漏记已读取内容。

```bash
python scripts/memory_gate.py init --root . --session SESSION \
  --preloaded AGENTS.md docs/cache/context_index.md
python scripts/memory_gate.py pack --root . --session SESSION \
  --required docs/cache/checkpoint.md --required docs/environment.md
python scripts/memory_gate.py audit --root . --session SESSION
```

同一上下文跨工具调用和用户轮次继续使用同一 ledger。不要重建以规避上限。
既有上下文安装本机制时，应先将实际已加载的外部指令复制到被忽略的 runtime
目录再计入 ledger；工具只接受项目内相对路径。引用不是新增事实所有者。

## 更新记忆

1. 执行实际任务，保存验收证据到 docs/evidence 或实际运行产物目录。
2. 在规范文档更新事实、范围及未知项，不复制整段日志到 kernel。
3. 经验记录使用 JSON：id/kind/claim/owner/scope/status、带时区时间、
   evidence 的 path/sha256、recheck/valid_until/depends_on/supersedes。
4. 未完成实验只记 candidate；通过实际验收且审核适用范围后才能 verified。
5. 使用 `validate-record` 校验；当前检索须匹配 scope、证据哈希及重查策略。
6. 配置变化令依赖哈希过期时重验；保留原记录，不覆盖历史来伪装成功。

训练/评估/部署的 manifest 应记录代码提交、dirty patch 哈希、实际命令、环境、
输入数据/划分/归一化/基础模型身份、随机种子、输出和验收阈值。未知项显式留空。
本次只接入记忆流程和验收记录，未声称已有训练启动器自动采集这些字段。

## 上限与宿主限制

每包 12,288 UTF-8 字节、累计 32,768 字节，含封装；不是精确 token 数。
额度接近上限时更新 checkpoint 并缩小后续读取范围，按上述用户覆盖继续任务。
当前宿主未提供完整请求及 tokenizer 集成，无法保证系统/历史/隐藏内容的总 token 上限。
不把这个工具描述成自动管理完整 Codex 上下文。

## 工具来源

`scripts/memory_gate.py` 复制自本机 mlops-memory 技能，标准库实现。
ledger 与临时参考副本位于 docs/cache/runtime，不进入 Git。

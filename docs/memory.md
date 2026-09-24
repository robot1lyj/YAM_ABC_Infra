# 项目记忆维护

本项目使用 mlops-memory 的文档所有权、按需检索和离线证据机制。没有外部向量库、W&B或独立的第二套记忆仓库。

## 信息层次

温度表示默认加载频率，不表示真实性高低。低频硬件事实降为按需读取后仍由原owner负责；不得因“冷”而丢失证据或自动改变record状态。

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

## 热记忆预算与降级规则

2026-09-14用户要求分层且限制热记忆增长。本节是本项目预算的唯一owner；共享技能原已要求精简入口，本轮进一步加入准入、预算、降级及写回检查，见 `/home/wuyan-lyj/condapi/skills/mlops-memory/references/layers.md`。本项目采取以下完整文件UTF-8字节上限，包含标题、链接和标点：

| 入口 | 内容与预算 | 加载条件 |
|---|---|---|
| AGENTS.md | 必须遵守的工作/产品/设备边界；≤3.5 KiB | 项目工作规则，宿主已注入则不重复读取 |
| cache/kernel.md | 稳定事实的短投影；≤1.5 KiB，最多8条主题 | 仅需概览且AGENTS/路由不足时 |
| cache/context_index.md | 一级任务导航；≤3 KiB；具体问题转 [problem_index](cache/problem_index.md) | 选择本次owner时，不跟随全部链接 |
| cache/checkpoint.md | 当前目标、活跃阻断、下一步和来源；≤1 KiB | 续作时，不作为历史日志 |

2026-09-24 按实际任务审查收紧预算：即使三份入口都读取，AGENTS＋kernel＋一级index合计≤8 KiB；续作加checkpoint≤9 KiB。平常由AGENTS选择一级路由，不预读kernel/checkpoint；这些字节上限是文件预算，不是完整会话token计数。缩减原因是早期规则和快照在热层重复、造成不必要输入；安全前提仍保留在AGENTS或可直达的owner。不得更换文件名/新增热摘要规避合计。

新增文件门槛：普通状态、结论和下一步直接替换现有owner/检查点，不按日期创建“本轮记忆”或备份摘要。只有独有且长期可复用的原始证据、失败反例或公共合同确实没有现有owner时才新增文件，并给出路由和用途；Git历史已保留的旧热文件副本不再自动再归档一份。现存冷证据只按需读取，删除重复元数据前先确认没有独有结论或引用。

写入时执行：

1. 新结果先写对应owner；设备序列号/IP/USB拓扑/版本放硬件或环境文档，页面细节放产品手册，命令/完整指标/失败过程放证据或cold记录。旧计划/旧进度只在归档保留。
2. 只有跨当前任务反复影响决策、带来源且无法由短导航充分替代的内容进入kernel；“刚刚完成”本身不是准入理由。近期进度进入checkpoint，完成后替换下一步，不按日期不断叠加。
3. 热项按主题替换与合并。超限先将低频细节移回owner/冷记录、修正相对链接、留下短入口；不硬截断句子或条件，不删除独有事实/证据，不把旧错误状态搬成新当前状态。
4. 每次记忆写回后统计上述完整文件字节，核对总量与路由；选择一个实际下一步确认能找到设备映射、操作或验收条件。超限应先整理，不能以达到读取额度为由中止用户工作。

`scripts/check_project_memory.py` 自动检查以上完整UTF-8字节预算、默认/续作合计及kernel最多8个顶层主题，同时检查当前文档和热入口的文件/Markdown章节链接、record指纹。缺失热文件、超预算、失效章节返回非零。默认输出只含错误、热预算和历史记录数量，确需定位历史条目才用 `--details`，避免每轮把旧记录列表送进上下文。章节解析覆盖本库使用的ATX标题及显式HTML锚点，不是通用Markdown渲染器；仍需语义复核。不增加第二套缓存、检索服务或自动“清理历史”程序。

核查与本轮整理来源：[产品与记忆基线](evidence/20260914-product-memory-baseline.json)，实际字节统计与检查结果见 [写回检查](evidence/20260914-product-memory-check.json)。

2026-09-22按冻结基线0bc1be5重新整理当前规范、归档和路由，见[本轮整理证据](evidence/20260922-memory-docs-refresh.json)。检查工具新增预算与章节验证，旧record不更新哈希冒充复验；人工合同抽查与机械检查不等同于真实agent任务效率评测，不宣称节省了多少token或时间。

## 架构固定与现场身份登记

架构设计由 `docs/dagger_architecture.md` 持有，工作包引用硬件、环境、合同与验收owner；不另建一套建议/规划缓存。用户确认的设备数量、接法和执行顺序作为带日期的需求记录；具体S/N、端口、单位、模型身份仍需实测。设计基线可以固定，未实现/未测要求在record中仍为candidate，不写成verified运行能力。

设备本体S/N（如设备提供且后续有需要）、USB-CAN适配器审计S/N、Hub物理端口、系统通道名、相机S/N分别记录，附观察时间、证据和重查条件；本工作站运行时以机械臂所接 USB 口为稳定身份，不要求机械臂本体S/N；系统枚举号不冒充稳定身份。当前登记入口为 [P0设备身份](workstation.md#第一步设备身份登记-p0)。condapi读取快照必须包含本地HEAD/文件指纹及范围，归本仓库接口owner，不在这里复制一套训练事实。

## 开始任务与按需展开

1. 先确定下一步决策、当前目标、用户约束和缺失事实，再从路由选择相关摘要及适用任务章节；检查点仅在续作需要时读取。
2. 优先当前项目、平台、代码/模型契约和版本匹配的资料。摘要不足、有冲突或需验证时，展开对应原文及必要前提，保留单位、版本、适用条件、反证和证据引用。
3. 当前记录须通过全部scope键、evidence和depends_on验证；过期、candidate或归档只用于审阅。设备、进程和网络等现场状态重新检查。
4. 信息足以支持下一步就停止检索；不重复加载仍在上下文中的内容，不全量读取历史、数据、第三方仓库或技能参考资料。
5. 搜索、日志和工具结果先在模型外过滤，返回相关片段或统计；原始产物留在所有者处。截断输出或分批倾倒完整历史不能降低累计上下文成本。

完整规范与证据长期保存，无文档行数上限；热入口另遵守上节文件预算。细节留在规范所有者、记录、原始证据和归档中，不为缩短上下文删除有用信息。
工作摘要保留当前目标、用户约束、适用事实、未解问题、下一步和可恢复的来源引用；摘要不能替代验证结论所需的证据。

## 选择性检索与计量边界

本项目按2026-09-11用户要求采用新版技能，取消旧默认累计读取额度。不得因读取计数达到固定阈值中止任务、要求压缩或新开对话。
观察到上下文压力时，整理已完成工作并在现有缓存保存可恢复检查点；写摘要不会移除既有消息，只有宿主实际压缩或替换上下文才改变保留历史。

`scripts/memory_gate.py` 是同步自已安装技能的可选标准库工具，不是每次读取的强制包装器。普通有界章节读取也可使用，仍需检查证据、范围和时效。

- 默认每个序列化检索包12,288 UTF-8字节，包含JSON封装；这是可用 `pack --max-bytes` 调整的检索设置，不是模型上下文上限。先缩小无关选择，必要完整证据可提高包大小，不能割裂结论与前提来适配。
- 无默认累计额度。若今后用户明确设置累计传输限制，则遵守该限制；加载新版工具不会偷偷移除已有ledger的限制。
- ledger记录声明预载文本字节及成功检索包的累计字节和去重历史；`tracked_bytes` 不代表当前上下文占用或模型剩余容量，也不覆盖未记账的直接读取。
- 只记录可见且允许记录的项目资料，不复制隐藏指令、凭证或完整环境变量。已知预载章节才传 `--preloaded`，未知时省略。
- `context_tokens` 为null，`exact_token_enforcement` 为false；完整请求计量需宿主使用实际tokenizer计算指令、历史、工具和封装并预留输出，项目未安装该宿主集成。字节计数不能代替完整token计数。
- 单文件2 MiB工具读取保护不是长期资料存储上限；更大日志用过滤工具或带来源的证据摘要读取，保留原件。
- 包溢出时不输出不完整的必需章节，不更新账本；缩小选择或按需提高包大小。证据/范围失效必须核查，不能通过调整字节限制绕过。

可选命令（在仓库根目录执行，SESSION为检索任务标识，已有ledger继续复用）：

```bash
python3 scripts/memory_gate.py init --root . --session SESSION
python3 scripts/memory_gate.py pack --root . --session SESSION --required 'docs/hil_quickstart.md#操作规则'
python3 scripts/memory_gate.py audit --root . --session SESSION
```

相同未变章节默认去重；宿主实际压缩后或所需片段已不在上下文时，仅对缺失章节加 `pack --reload`，重新校验证据与scope并计数，不批量重载历史。
Ledger位于被忽略的 `docs/cache/runtime/`，不提交Git，不通过重建或更换ID规避显式额度。

## 旧账本与技能快照迁移

本次用户已授权移除旧累计额度，对已有ledger原地执行：

```bash
python3 scripts/memory_gate.py resize --root . --session SESSION --no-total-limit --reason '2026-09-11用户要求按新版技能取消旧累计读取额度'
```

保留原 `used`、`seen`、账本身份和已有历史，在 `adjustments` 记录时间、原因及旧/新上限；不清零、不伪造压缩、不扩大模型窗口。
运行时旧技能快照保留为历史资料并标注已失效；当前方法以重新读取的已安装 `mlops-memory/SKILL.md` 为准，项目事实仍归本项目规范所有者。迁移记录见 [迁移验收](evidence/20260911-memory-migration.json)。

## 工程经验与问题检索

当前方法来源为用户指定的 `/home/wuyan-lyj/condapi/skills/mlops-memory/SKILL.md`；仓库工具及测试同步该版本（只作项目格式调整）。
优先当前数据集整理/离线转换主线，再补高价值故障经验。保留原目录和旧v1记录，按需补充有来源的正文或下列可选字段，不批量重写历史。

| 可选字段 | 必须表达的内容 | 判定边界 |
|---|---|---|
| `capability`（procedure） | entrypoint、invocation、config_paths、inputs、outputs、validation_command、acceptance、limitations | 入口/配置及影响行为的代码、验证器和样例纳入depends_on；实测证据通过才标verified，文件存在/退出成功不足以证明功能 |
| `attempt`（lesson） | symptom、hypothesis、intervention、observation、verdict、confounders、retry_when | verdict为supported/refuted/inconclusive，针对假设而非工具优劣；verified失败尝试只证明发生及有限结果，不推荐重复失败修复 |
| `assumptions` | name、expected、observed、unit、observed_at、check、result、recheck_when | 未测量时observed/observed_at为null、result为unknown；保留mismatch，不能将配置值抄成实测；易变条件使用前复查 |
| `retrieval` | terms及可选related关系（check/repair/attempt/prerequisite + source） | 指向现有规范章节/记录；按缺口展开，不递归加载全部关联；链接存在不证明目标结论有效 |

先查 [问题路由](cache/context_index.md#问题与行动路由)，再按需要读检查方法、历史尝试或修复工具。重复失败干预前核对范围、原因假设和重试条件；新证据/环境变化后可以重新试验并保存独立结果。多个变量同时变化时不夸大因果，未知项明确写出。
配置文件只证明预期设置；运行版本、端口、磁盘、性能和硬件状态需要相应测量。以能否恢复正确任务、减少重复尝试和遗漏条件评价记忆，不只看摘要长短。

可选发现命令（SESSION沿用已有检索账本，项目与平台等scope须全部匹配）：

```bash
python3 scripts/memory_gate.py search --root . --session SESSION --query '数据集 检查 合并' --scope project=YAM --scope platform=linux-x86_64 --top 3
```

`search`只返回有限数量的声明/来源/范围摘要，不返回执行命令或原始日志；结果也记入原ledger，不把完整记录标为已读。先加载选中的record，必要时再读related目标。旧记录额外scope键须补齐；历史/candidate使用 `--purpose review`，不提升为当前事实。词法排序不是置信度，空结果要看排除原因，不反复运行未变查询。

初始高价值记录为 [工作台工具](cache/records/dataset-workbench-capability-20260911.json) 和 [系统Python构建尝试](cache/records/system-python-attempt-20260907.json)。当前扩展验收见 [工程记忆检查](evidence/20260911-engineering-memory-audit.json)，上一轮迁移证据保留为历史，不表示当前文件仍有相同哈希。

## 更新任务结果

1. 新建证据，不改写旧日志使其“继续通过”；未知项明确说明。
2. 更新相应规范所有者，描述已实现/已验证/待现场验收，不保留相互冲突的顶部补丁。
3. 必要时新增record，字段为id、kind、claim、owner、scope、status、时间、evidence、recheck、valid_until、depends_on、supersedes。
4. 实测结论只在对应范围内verified；设计建议是candidate；失效结论为stale或superseded，旧证据保留。
5. 更新kernel的来源投影、checkpoint的下一步，不重复完整实现细节。
6. 运行下面的检查，审阅diff后提交。授权明确时按现有远端推送，不需要新建外部记忆服务。

```bash
python3 scripts/check_project_memory.py
python3 scripts/memory_gate.py validate-record --root . --record docs/cache/records/operator-workbench-20260908-offline.json
```

## 检查工具负责什么

`check_project_memory.py` 检查当前路由、中文入口和cache顶层文档的文件/章节链接、热记忆预算、record结构和可选工程字段、证据与依赖指纹、重复ID以及规范owner是否在路由中。verified记录失效返回非零，提示重新验证；非current的历史记录单独列出，不能当作通过当前验收。不会遍历改写历史证据；归档中带日期的旧说法不能覆盖现行owner。

它不修复内容、不自动提升candidate、不运行机械臂或模型，也不验证网页可达性、Markdown锚点、记录的语义真伪或完整上下文token。

复用历史记录时仍须使用所有scope键做匹配；`validate-record`只校验本地结构和指纹，不替代实际任务的scope判断。

## 训练与部署记录

训练/转换及Thor模型部署应在各自owner记录代码提交、未提交变更指纹、命令、数据划分、norm/模型身份、配置、产物与验收范围；3588推理控制会话按用户最新决定不记录模型名称、后端、指纹或服务URL，只记录本地动作间隔、请求/动作计时和控制安全结果。
condapi拥有训练和Thor模型来源事实，本仓库只保存接口依赖及读码范围；3588的HIL manifest不建立模型来源链。

# MP4＋HDF5 采集与 LeRobot 转换

## 当前默认流程

原始格式为 `yam_hil_v2`：独立编码进程通过固定容量共享 RGB 缓冲接收图像，主要数值字段分批追加到 HDF5；MP4 流式编码。控制线程不做 HDF5、视频编码或文件关闭。旧 `yam_hil_v1` JSONL 集继续可读。

```text
会话/
  session.json                         # 初始化及每集结束原子更新
  episode_000001/
    manifest.json                      # episode身份、分段索引、结果
    segment_000000/
      samples.h5                       # 状态、动作、时间、视频索引、有效性
      top.mp4 / left.mp4 / right.mp4
      segment.json                     # 已关闭分段的提交清单
    segment_000001/
```

默认60秒切一次存储文件，不结束episode、不重置DAgger事件。`--segment-seconds` 可调整；目前按帧数对应名义时长分段，未增加文件大小触发。HDF5每15行追加，数值无压缩、显式有效性掩码；复杂诊断字段保存在HDF5的details字符串列，未宣称所有嵌套诊断字段已数值化。相机接收/设备时间、帧号另有带掩码的数值列。视频是RGB转H264/yuv420p，不是无损原图，也未录深度。

`--min-free-gb` 默认0.5 GiB，每秒及开段前检查可用空间；不足时录制报错，由控制运行时进入故障保持。此阈值是保护下限，不是对某一小时所需空间的估计。

## 采集与转换完全独立

工作台和无界面CLI都只保存MP4＋HDF5及清单。结束一集就提交索引，退出不启动转换、不创建转换队列、不扫描旧任务。`--raw-only`仅保留旧命令兼容，已不影响行为。旧转换队列文件和旧导出数据保留在磁盘，但不再执行。

完成采集后可以把完整原始会话目录上传服务器，再显式运行一键脚本。保留`session.json`、各集`manifest.json`、所有分段中的HDF5/三路MP4/segment.json及相对目录结构；不需要工作站任务库、绝对路径或机械臂配置文件。不要复制仍在写入的分段后就开始转换。

```bash
uv run --locked --script scripts/convert_lerobot.py /data/raw/yam --output /data/lerobot/batch_001
```

输入支持单集、单会话或多会话父目录。脚本按会话/独立集分别输出数据集，不重复扫描会话内的集；它不是跨任务聚合工具。输出目录必须新建且与来源互不嵌套，不覆盖来源。一个来源失败后继续其余来源，最后退出码非零，`conversion_report.json`记录每项结果与失败原因。失败重跑使用新的输出目录，保留旧partial用于检查。

脚本的PEP723依赖及`scripts/convert_lerobot.py.lock`独立锁定NumPy、h5py、PyAV、Pandas、PyArrow。服务器有uv和本仓库代码即可运行，uv会准备Python3.12和轻量转换环境；不需要安装机器人SDK、相机驱动、Torch或本项目全部依赖，不需要初始化机器人子模块。首次运行可能需要下载依赖，依赖缓存后可以复用。当前锁文件使用镜像源。

专家模式可追加`--expert-only`；审核过的恢复数据可追加`--allow-recovered`。同样的字段语义与过滤规则适用于单个`yam-export`命令。

完整连续视频优先重新封装H264包，避免二次有损编码。转换仍会读HDF5、校验文件、解码视频计算统计，不能承诺任意数据量都瞬间完成；筛选/缺口使用重新编码路径。参数不兼容则明确报错。数值每256行写Parquet，图像逐帧读取，不加载整集视频。

目标固定 **LeRobot Dataset v3.0**，与Python包版本分开；官方读取兼容基线为 **LeRobot 0.5.1**。大规模跨会话聚合、上传和数据集版本发布以后单独讨论，脚本不会自动上传。

## 字段约定

DAgger使用上述`yam_hil_v2`原始格式，策略→人工→策略保存在同一集；当前录制侧删除介入待人工和人工锁定待交还两类等待帧，交还后的RESUME另行保留。`human_action`是原始Leader输入，HIL复用绝对1:1遥操作，不再用相对偏移；**专家训练action必须取`submitted_action`**，因为硬限位和夹爪软接管仍可能改变最终目标，不能把Leader角度直接当Follower标签。夹爪0关1开，关节绝对rad，顺序左6+左夹爪+右6+右夹爪。完整字段及等待规则归[数据合同](hil_dataset_fields.md)。

每集开始记录实际运行的`policy_fusion`、`rtc`、`rtc_delay_steps`、`action_dt`和`streaming`，并冻结该配置快照，避免异步写盘时被下一集覆盖。此前热切换RTC的旧集manifest可能仍写启动时TDA；须结合逐帧`details.policy_fusion`及`policy_reply`审计，不能仅凭旧清单筛选，原件不自动改写。

图像按主机接收时间配对，`observation_state`为该参考时间的插值反馈，`measured_state`为当前控制tick反馈，`submitted_action`为本tick提交目标。该合同保留真实软件时间差，不声称曝光与下发零延迟。观测无效帧保留审计但不作专家标签；冻结/交还等待也不作专家标签。

| 字段 | 语义 |
|---|---|
| observation.state | 左6关节+左夹爪+右6关节+右夹爪，共14维；相机配对时刻的Follower反馈 |
| action | 实际提交到Follower的14维绝对目标，不是Leader原始角度或下一帧实测状态 |
| observation.images.top_rgb/left_rgb/right_rgb | 三路RGB视频 |
| complementary_info.measured_state | 控制tick的原始Follower反馈 |
| complementary_info.action_source | 0人工、1策略、2保持 |
| complementary_info.is_intervention / expert_valid | HIL人工阶段 / 有完整观测的人工训练标签有效性 |
| complementary_info.policy_action | 对齐Evo-RL：本帧策略候选14D动作；无候选时导出零，原始HDF5保留null/有效性信息 |
| complementary_info.state | Evo-RL三态：0策略、1介入、2交还；本站冻结属于介入开始，交还等待直到新策略生效 |
| complementary_info.collector_policy_id | policy/human来源，不保存模型名称或指纹；本站额外HOLD帧为null，不能冒充专家或策略动作 |
| complementary_info.intervention_id | 累计干预编号，用阶段事件确定区间 |
| complementary_info.event | 位标志：1介入冻结生效、2人工开始、4交还请求、8策略开始、16人工结束锁定待交还 |
| complementary_info.source_tick / control_time | 原始控制帧号和RK单调时钟 |
| complementary_info.event_requested_at / event_applied_at | 请求入队/阶段转换提交时间；无对应事件为-1 |
| complementary_info.observation_valid | 本帧是否组成新的有效相机/状态配对 |

标准timestamp使用episode内的名义帧率时间轴；实际设备/接收时刻及同步偏差保存在原始HDF5（旧集为JSONL）。无有效观测时状态回退为当前Follower反馈并标无效，不把保持/等待阶段当专家示范。

`manifest.json`及LeRobot episode元数据补齐Evo-RL的`episode_success`，仅明确标注的`success`/`failure`有值；unknown/aborted不自动变成失败。上述字段按[已核对源码](reference_sources.md#evo-rl)映射，未增设奖励、干预原因、任务阶段或模型打分。原始记录已有相应来源、阶段、候选和结果，旧集可通过离线导出获得这些字段而不改原件。本站action仍取实际提交目标；Evo-RL该版写`action_values`而不是`_sent_action`，两者不能宣称完全相同。

HIL的epoch变化不直接拆episode，策略→人工→策略保留在同一集。普通导出使用连续帧序号时间；expert-only按有效人工片段及原始tick缺口分组。删等待后的连续timestamp不是物理时间无缝，原始tick/time和wait_boundary保留供追溯。aborted和discarded不进入正式数据；失败但正常保存的集保留failure标签，由训练流程审核使用。训练时不能不加筛选地把所有policy/hold帧当作人工示范。

## 离线重建与专家筛选

输入可以是会话目录，也可以是单个episode。输出必须是新目录；转换成功前使用 `.partial`，保留原始数据及来源文件SHA256。

```bash
uv run --no-sync yam-export data/episodes/实际会话 --output data/rebuilt/新目录
uv run --no-sync yam-export data/episodes/实际会话/episode_000001 --output data/expert/新目录 --expert-only
```

`--expert-only` 要求人工来源、expert_valid、observation_valid及完整三路图像。按实际tick缺口和epoch边界切片，不能把被过滤的policy间隔直接拼起来。默认完整轨迹导出保持HIL各阶段，训练端不能把所有行当专家。

`hil.export` 仍兼容旧canonical输出，支持两种原始格式；它是离线工具，专家片段的数组可能按片段加载。大规模训练请使用上面的流式LeRobot导出。

## 异常恢复

每个分段关闭后提交清单。HDF5采用SWMR和有效行提交计数；每段结束执行文件同步。两种文件没有跨文件事务，不承诺异常断电零丢失，最后一段仍可能不可恢复。

恢复必须在录制进程停止后执行，输出新目录，不覆盖来源：

```bash
uv run --no-sync python -m yam_abc_reproduce.hil.recovery data/episodes/实际会话/episode_000001 --output data/recovered/新目录
```

恢复扫描包括未登记的分段，检查HDF5已提交行与三路可解码视频的共同连续前缀；损坏段记录原因，保留可恢复段。结果标记recovered，默认不进入训练。审核后显式导出：

```bash
uv run --no-sync yam-export data/recovered/新目录 --output data/rebuilt/审核后目录 --allow-recovered
```

目前恢复是CLI操作，未提供界面自动修复或断电一致性保证。程序被终止测试与真实断电测试必须区分。

## 图形化整理入口

独立数据集工作台支持原始目录索引、逐集审阅、失败分类、回收站、集合合并与多 task LeRobot v3.0 导出，详见 [操作手册](dataset_workbench.md)。采集工作台不启动转换。

## 可复用工具与重试条件

| 工具与入口 | 配置、适用输入与输出 | 验证方法与边界 |
|---|---|---|
| [一键转换](../scripts/convert_lerobot.py) | 命令见“采集与转换完全独立”；PEP723配置及 [独立锁](../scripts/convert_lerobot.py.lock)，Python3.12/uv；已结束原始集→每来源独立v3.0及conversion_report.json | `uv run --no-sync pytest -q tests/test_offline_conversion.py tests/test_lerobot_convert.py tests/test_lerobot_export.py` 为基础导出回归；完整脚本验收另见 [验收](acceptance.md)。回读需核对14维/三路RGB、任务和帧边界，不能只看退出码 |
| [异常恢复](../yam_abc_reproduce/hil/recovery.py) | 本文“异常恢复”命令；项目pyproject.toml/uv.lock；停止录制后，原集→新recovered目录，保留共同可解码前缀 | [录制测试](../tests/test_segmented_storage.py)中的恢复案例（`uv run --no-sync pytest -q tests/test_segmented_storage.py -k recover`）及 [历史验收](acceptance.md)；恢复后人工审核、回读，再决定allow-recovered；没有真断电零丢失保证 |

帧率/分辨率不一致、媒体缺失或输出冲突属于待排查症状，不能在没有报告时断言具体成因。记录实际检查/干预和结果；重试条件是输入修复或重新筛选、必要兼容条件已确认、使用新输出目录。保留旧partial和失败报告，不覆盖原件，不对同一未变输入盲目重复转换。恢复工具不能修复所有异常；无法恢复的范围与原因未知时如实记录。
# DAgger 介入等待段（2026-09-18）

点击介入到右①解锁、实际进入 HUMAN 的等待阶段，不提交样本或图像；控制与姿态保持不停止。首个恢复样本的 `omitted_intervention_wait` 及 manifest 的 `omitted_intervention_waits` 记录省略区间、原 tick、时间和介入编号。保留原始 tick/time，因此有意产生 tick 间隙，不应伪装成连续控制轨迹；要求连续 tick 的回放工具仍应拒绝这种片段。人工解锁后的再次锁定不属于本次省略范围。旧数据不改写。

介入期间禁止“开始模型执行”，包括暂停后的 HOLD；须明确交还模型。关节调试不再限定采集模式，但仍要求 HOLD、无活动录制、无介入、无急停和维护占用。

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
    exports/attempt_1_唯一后缀/          # 工作台后台生成LeRobot v3.0
```

默认60秒切一次存储文件，不结束episode、不重置DAgger事件。`--segment-seconds` 可调整；目前按帧数对应名义时长分段，未增加文件大小触发。HDF5每15行追加，数值无压缩、显式有效性掩码；复杂诊断字段保存在HDF5的details字符串列，未宣称所有嵌套诊断字段已数值化。相机接收/设备时间、帧号另有带掩码的数值列。视频是RGB转H264/yuv420p，不是无损原图，也未录深度。

`--min-free-gb` 默认0.5 GiB，每秒及开段前检查可用空间；不足时录制报错，由控制运行时进入故障保持。此阈值是保护下限，不是对某一小时所需空间的估计。

## 保存与后台转换

结束一集后关闭并同步文件，原子提交集/会话清单，再入持久转换队列。工作台机械臂连接期间暂停转换进程；断开后自动恢复，不等待整批转换才允许重新连接。相机仍可预览。界面区分本集保存、待转换、转换完成和失败，并提供失败重试。

此进程隔离与转换暂停实现面向Linux工作站；父进程意外退出会终止其编码/转换子进程，避免孤儿进程继续写入。

队列在任务库下的 `conversion_mock` / `conversion_real` 中，文件锁确保同一个队列只有一个工作进程管理器。job记录来源清单哈希和转换器代码哈希，失败重试写新attempt目录，保留旧partial。转换版本变化后点击重试会创建新版本任务，旧失败任务标为superseded，不把旧结果标成新版本。`--raw-only` 不为新集入队；已有队列仍保留。

无界面CLI保持关闭设备后导出 `会话/lerobot/` 的兼容行为。工作台按集导出，尚未实现跨会话聚合、远端传输、数据集修订发布；这些属于后续大规模数据集讨论范围。

完整连续集优先直接重新封装H264包。转换仍解码图像计算统计，但不进行第二次有损编码；有帧筛选/缺口时走重新编码路径。参数不兼容的直接封装报错，保留来源，不能悄悄生成错误视频。数值每256行写Parquet，图像逐帧读取，不加载整集视频。

数据格式固定 **LeRobot Dataset v3.0**，与Python包版本分开。官方读取兼容基线为 **LeRobot 0.5.1**；RK录制环境不安装Torch。每次新转换器的实际回读结果以 [验收](acceptance.md) 为准。

## 字段约定

| 字段 | 语义 |
|---|---|
| observation.state | 左6关节+左夹爪+右6关节+右夹爪，共14维；相机配对时刻的Follower反馈 |
| action | 实际提交到Follower的14维绝对目标，不是Leader原始角度或下一帧实测状态 |
| observation.images.top_rgb/left_rgb/right_rgb | 三路RGB视频 |
| complementary_info.measured_state | 控制tick的原始Follower反馈 |
| complementary_info.action_source | 0人工、1策略、2保持 |
| complementary_info.is_intervention / expert_valid | HIL人工阶段 / 有完整观测的人工训练标签有效性 |
| complementary_info.intervention_id | 累计干预编号，用阶段事件确定区间 |
| complementary_info.event | 位标志：1介入冻结生效、2人工开始、4交还请求、8策略开始 |
| complementary_info.source_tick / control_time | 原始控制帧号和RK单调时钟 |
| complementary_info.event_requested_at / event_applied_at | 请求入队/阶段转换提交时间；无对应事件为-1 |
| complementary_info.observation_valid | 本帧是否组成新的有效相机/状态配对 |

标准timestamp使用episode内的名义帧率时间轴；实际设备/接收时刻及同步偏差保存在原始HDF5（旧集为JSONL）。无有效观测时状态回退为当前Follower反馈并标无效，不把保持/等待阶段当专家示范。

HIL的epoch变化不会拆episode，策略→人工→策略完整保留。真实帧号缺口会分段，避免伪造连续性。aborted和discarded不进入正式数据；失败但正常保存的集保留failure标签，由训练流程审核使用。训练时不能不加筛选地把所有policy/hold帧当作人工示范。

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

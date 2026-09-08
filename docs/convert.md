# LeRobot数据与导出

## 默认流程

当前运行入口在关闭设备和原始录制后，自动把有效片段整理为 **LeRobot v3.0**，无需手动转换。原始会话仍保留，用于故障追溯和重新处理。

```text
会话/
  session.json
  episode_000001/steps.jsonl + 三路MP4 + manifest.json
  lerobot/
    data/chunk-000/file-000.parquet
    videos/observation.images.top_rgb/chunk-000/file-000.mp4
    videos/observation.images.left_rgb/chunk-000/file-000.mp4
    videos/observation.images.right_rgb/chunk-000/file-000.mp4
    meta/info.json + stats.json + tasks.parquet + episodes/
    provenance.json
```

写入使用PyArrow/Pandas/PyAV，RK3588不需要PyTorch。格式结构对照官方LeRobot0.5.1源码，并使用其真实读取器验证；训练端仍使用自己的环境。

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

标准timestamp使用episode内的名义帧率时间轴；实际设备/接收时刻及同步偏差保存在原始JSONL。无有效观测时状态回退为当前Follower反馈并标无效，不把保持/等待阶段当专家示范。

HIL的epoch变化不会拆episode，策略→人工→策略完整保留。真实帧号缺口会分段，避免伪造连续性。aborted和discarded不进入正式数据；失败但正常保存的集保留failure标签，由训练流程审核使用。训练时不能不加筛选地把所有policy/hold帧当作人工示范。

## 故障恢复和离线重建

生成期间写入 `lerobot.partial/`，全部成功后改名为 `lerobot/`。失败不暴露半成品为正式数据集；保留原始记录和partial供诊断。没有有效片段时仅生成包含零帧说明的provenance，不声称得到可训练的数据集。

重新处理须使用不存在的新目录：

```bash
.venv/bin/python -m yam_abc_reproduce.hil.lerobot_export data/episodes/实际会话 --output data/rebuilt/新目录
```

`--raw-only`可用于只测录制性能、暂缓格式整理；默认不启用。整理过程批量256行写Parquet，逐帧解码/编码，不把整段图像加载进内存。

保留的 `hil.export` 是额外的专家筛选工具，输出旧canonical格式，不是默认LeRobot路径：

```bash
.venv/bin/python -m yam_abc_reproduce.hil.export data/episodes/实际会话/episode_000001 --output data/expert/新目录
```

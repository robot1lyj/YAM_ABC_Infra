# HIL 原始数据字段（按当前写入代码，2026-09-21）

原始格式为 `yam_hil_v2`：每集 manifest.json，每段 samples.h5 和 top/left/right.mp4；不是LeRobot导出格式。三路RGB视频独立存储，不在HDF5中存像素。下列字段可空，不能把空值当作0。

## 每帧逻辑字段

| 字段 | 含义 |
|---|---|
| tick、time | 原始控制tick、IPC单调时钟秒；删等待段后不重排 |
| epoch | 控制代次，用于使旧推理结果失效 |
| mode、phase、source | 产品模式、内部阶段、目标来源 |
| event、event_requested_at、event_applied_at、transitions | 输入事件、请求/生效时间及阶段转换；未发生时可空 |
| intervention_id、intervention_pending、is_intervention | 介入编号、介入尚未交还、当前是否人工介入 |
| expert_valid、observation_valid、policy_valid | 有效人工示范、有效同步观测、当前有效策略目标；不是任务成功标签 |
| measured_state | 下发前Follower反馈，14D |
| leader_state | Leader反馈/手柄开度，14D |
| observation_state | 与观测图像配对的状态，14D；可能空 |
| policy_action | 本tick选中的模型目标，14D；非策略帧可空 |
| human_action | 人工阶段的原始Leader输入，14D；不是补偿后的Follower目标 |
| selected_action | 仲裁选中目标，包含人工相对位移映射，14D |
| bounded_action、bounded_at | 下发接口前目标与准备完成时刻 |
| submitted_action、submitted_at、apply_returned_at | 实际提交SDK的目标、按臂写入时间戳、写调用返回时间；不代表电机到位时间 |
| constraint_mask | 14维，提交目标与选中目标是否不同 |
| gripper_owned | 左/右夹爪是否完成软接管 |
| maintenance、home_group、stop_latched | 维护状态、回位臂组、软件停止锁存 |
| policy_fusion、action_index | 动作规划模式、动作索引（RTC为目标tick，不能一律当块内索引） |
| request | 当前动作来源的请求标识，结构见下 |
| policy_reply | 本tick收到的模型回复及计时，不是每帧都有 |
| policy_selection | 规划器的选择来源信息，结构随模式变化，可空 |
| policy_write_trace | 可选高频写入追踪；当前30Hz直发可空，不能宣称每次电机写入均有独立追踪 |
| policy_url | 当前代码仍写入策略来源地址；无模型名称/检查点指纹字段 |
| obs_id、sync、sdk_state_age_s | 观测编号、同步质量详情、各SDK反馈年龄 |
| video_indices | top/left/right在本segment视频内的索引；新segment从0开始 |
| record_event | 采集模式的录制按键事件，仅该模式有 |
| omitted_intervention_wait | 被删等待段之后第一条保留帧附带的区间摘要 |

所有14D向量顺序：左J1–J6、左夹爪、右J1–J6、右夹爪。关节rad，夹爪名义0闭1开。submitted_action是训练动作候选；measured_state是反馈，两者不可混淆。

### 请求与回复

request及policy_reply.token：`epoch, request_id, observation_id, created_at, observed_at, queue_size_at_request, observation_policy_tick, rtc_delay_steps`；时间为IPC单调秒，TDA/RTC专属值可空。

policy_reply：`token, received_at, discarded, error, worker_elapsed_ms, server_timing, client_timing, actions`；RTC额外`rtc_takeover_tick, rtc_reply_tick, rtc_slack_ticks`。actions为原始返回动作块（通常50×14），异常可空。server_timing按服务返回保留，非固定训练字段。client_timing可含`pack_ms, send_ms, wait_response_ms, unpack_ms, payload_bytes`。

sync：`age_s, arrival_skew_s, sync_warning, reference_time, state_bracket, cameras`。cameras按top/left/right含`device_timestamp_ms, timestamp_domain, device_frame_number, host_received_at, color_space, sequence`。无有效观测时sync可为空；沿用上一帧图像时observation_valid=false，应按标志过滤。

### HDF5物理结构

七个14D向量：measured_state、observation_state、leader_state、submitted_action、selected_action、policy_action、human_action，float64 N×14。

标量列：tick/epoch/intervention_id为int64；time/event_requested_at/event_applied_at为float64；is_intervention/expert_valid/observation_valid为bool。上述各列均有同名`__valid`标志，空向量的物理占位0不代表真实零位。

video_indices为int64 N×3。camera_host_received_at、camera_device_timestamp_ms、camera_sequence、camera_device_frame_number为float64 N×3，各有N×3的`__valid`。相机顺序top、left、right。其余逻辑字段放在UTF-8 JSON `details`列，read_rows会还原；`_segment`是读取器添加的来源路径，不是原始录制列。

## 每集manifest

固定存储字段：`schema, episode_id, fps, steps, segments, outcome, episode_success, error, clock, action_semantics`。outcome为recording/unknown/success/failure/discarded/aborted等；episode_success在明确成功/失败时保存对应字符串，否则null，非逐帧reward。

配置/来源元数据：`station, mock, rtc, streaming, action_dt, policy_fusion, expected_policy_latency, prefetch_margin, rtc_delay_steps, operator_task, task, collection_task, collection_mode, video_encoder`。station含robot/cameras/control_hz/save_root/task_name/data_format/deploy_home_pose等配置快照；collection_task含id/created_at/name/instruction/task。

`omitted_intervention_waits`：仅本集的区间列表，每项含`intervention_id, reason, first_tick, last_tick, start_time, end_time, event_requested_at, event_applied_at, frames`。reason区分takeover_wait（介入到人工）与handback_wait（手柄锁定到交还）；旧数据可能没有reason。区间时间是首尾采样时刻，帧数/30才是30Hz名义时长。可选terminal_status、close_errors为退出/收尾诊断，不保证每集存在。

segments每项：`path, steps, start_frame, video_frames, files, state`；files按文件名给出bytes，video_frames按视角记录帧数。session.json是任务会话索引，不能当成逐帧标注。

## 等待段及训练边界

新逻辑同时剔除介入待人工与人工锁定待交还段，视频和样本一起不写入；保留原始tick/time及区间摘要。交还后等待有效模型动作的RESUME不在本次删除范围内。FAULT不被等待过滤隐藏。

每集录制器独立持有区间清单，在消费完本集FIFO后写入manifest，不再从设备进程复制跨集摘要。旧第02集只修正清单，未重写视频/样本删除旧HOLD帧。

新录制规则（待首次部署）：保留帧新增`frame_index`（从0连续）、`timestamp=frame_index/fps`（秒）、`wait_boundary`（切除等待后的第一帧为true），存于HDF5 details。原始`tick/time`及相机时间不改；视频和样本整帧一起筛选，不插造动作、不自动拆集。普通LeRobot导出原已按帧序号生成连续时间；expert-only导出仍按其显式人工片段规则分组。连续时间不代表切点两边物理轨迹必然连续，切点标记用于追溯。

参考Evo-RL的同集策略/人工标注与默认帧序号时间：[recording_loop](https://github.com/MINT-SJTU/Evo-RL/blob/c735d69d098cdefd0fdaf8d2063af06d22dab130/src/lerobot/scripts/recording_loop.py)、[lerobot_dataset](https://github.com/MINT-SJTU/Evo-RL/blob/c735d69d098cdefd0fdaf8d2063af06d22dab130/src/lerobot/datasets/lerobot_dataset.py)。Evo-RL没有本站两段锁定等待，删除这些等待是本站规则，不宣称上游有同样处理。

当前没有逐帧reward、discount、done、terminated/truncated，未记录完整电机电流/力矩/温度，也没有人工抓取成功的自动真值。用于RL时这些不能凭expert_valid或phase臆造。

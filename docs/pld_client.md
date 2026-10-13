# PLD-Grasp 抓取修正专家客户端

状态：2026-10-13，客户端已增量部署IPC；10月12日probe第01/02集已核对：逐tick来源、前缀、完整回复和三路视频无错位，边界说明见[验收](acceptance.md)。完整替换旧PARTS模块，不兼容旧配置、接口或原始包；TDA/raw代码也已删除，仅保留完整50步同步和原生RTC。当前RTC d=9、30 Hz直达SDK。10月13日只读状态显示Thor已声明五模式、actor_loaded/replay_contract_ready=true，合同/快照与旧probe启动配置不同，客户端因此拒绝开始；新专家仍须显式应用并验收，不能沿用昨日诊断probe的结果作为正式collect验收。

## 职责和模式

客户端只负责模式、请求身份、原生 RTC 执行和完整录制。Thor 负责介入窗口、活动臂、修正、抓取切段、编号和奖励关联；客户端没有 SAC、抓取状态机或逐抓取成功/失败按钮。当前目标是接近、对准、下降、闭爪、抬起持住；容器调整、运输、放置仍由 Pi 负责。是否误介入容器须用 probe 实查，不能只凭目标说明认定已隔离。

| RL 模式 | 动作与记录 |
|---|---|
| off | 原 Pi；不请求专家修正 |
| shadow | Thor 计算，客户端执行原 Pi，保存完整回复 |
| probe | 执行原 Pi，记录专家链路，先验收来源/时序/完整性 |
| collect | 执行 Thor 返回的有界随机修正并采集 |
| eval | 执行无探索噪声的专家输出作对比 |

这是 RL 模式，不是工作台人工采集的 collect 控制模式。五项是协议全集，页面按握手 `pld.modes` 开放能力。Thor 的 `probe_diagnostics_only` 部署仅支持 off/probe，`actor_loaded=false/replay_contract_ready=false`；其记录 `replay_eligible=false`，不可当作 SAC 训练经验。没有专家、身份/任务不匹配时明确拒绝，不静默显示“已启用修正”。

模板：[pld_client.json](../configs/pld_client.json)，默认 off。HOLD、无介入、录制及保存结束时应用模式；每轮固定 mode/run/contract/snapshot。预设 SHA 可锁定预期身份，留空则使用本轮 Thor 握手值；运行中变化不自动接受。重连新 Runtime 按启动配置恢复，不承诺保留页面未写入配置的设置。

## 请求与 RTC 时间合同

保留原 `{type:"infer", obs, rtc}`，non-off 增加外层：

```json
{
  "pld": {
    "schema": "pld_sac_block_v2", "mode": "probe",
    "contract_sha256": "握手SHA", "snapshot_sha256": "握手SHA",
    "run_id": "整轮批次", "episode_id": "原录制UUID",
    "epoch": 8, "request_id": 123, "observation_id": 456,
    "previous_command": ["14D上一tick实际提交SDK的目标"]
  }
}
```

epoch/request_id/observation_id 直接复制原 token；不新建第二套计数。录制器在 start 入队时分配 UUID，manifest 与请求使用同一个 episode_id，换任务清空。previous_command 取观测对应 k−1 tick 且已在观测锚点之前成功提交的命令；缺少精确历史则暂不请求，不用 measured_state 或新目标补齐。

图像、state、token 来自原同步观测快照。返回仍为有限 50×14 actions，继续由原 RTC 队列消费，沿用原请求周期和延迟预算；8 tick 只是 Thor 修正包络，不是客户端固定重规划周期。已承诺前缀保留原 request/model_index/target_tick，不重标成最新请求。

shadow/probe 顶层 actions 必须与回复 base 完全相同；核对身份、原观测 state、起始 tick、delay_steps、previous_command 和 trace/actions 一致性。前缀核对、限位、急停、操作台失联及反馈超时保护均保留。拒收后 HOLD，须核对并重载推理通信后手动开始，不自动重连机械臂或恢复运动。

## 完整记录

沿用 [HIL 数据合同](hil_dataset_fields.md)，不新建一份训练格式：

- 正常帧 HDF5 `details.policy_reply.pld` 原样保存服务字典，包括未知新增字段、base、门控、身份、state、特征、残差、修正计划。
- `pld_request` 保存外层请求；`request_context` 保存 previous_command_tick/submitted_at、原观测同步信息、committed_actions 及 prefix_sources。
- 每 tick 保留 `policy_selection.request/model_index/target_tick`、`submitted_action`、有效位、float64 时间和三路 video_indices。
- session 级 `policy_events.jsonl` 不受 HIL 等待段过滤或停录影响，保存 request/reply/cancelled；完整回复仍放 `details.policy_reply.pld`。晚到响应写回原 session，不混入下一任务。停止时仍未收到的响应只记取消，不伪造服务输出。
- manifest 记录 pld 模式/身份、action_limits、policy_events 相对引用。整轮 C/N 在暂停保存后按原 episode_id 写入 evaluation 事件，purpose=evaluation_only，不是逐帧 reward。

必须复制整个 session（含 policy_events.jsonl），不能只拷 episode。Thor 数据管家当前主要从 HDF5 消费已到达正常帧的回复；取消、晚到或等待期间仅在 sidecar 的响应需要同时读 sidecar 复核，不能用伪造视频行补齐。录制队列/序列化失败明确暂停，不悄悄丢掉 trace。原始 tick/time 不重排，连续训练 timestamp 及等待边界沿用原机制。

### 操作员页面与整轮评估

RL页复用三路相机、开始/暂停、当前任务总集数和原“删除上一集”流程，不另造一套计数或删除接口。技术字段收在默认关闭的诊断详情；修改页面不改设备控制。C/N在暂停并保存后填写，关联当前刚结束的原episode_id；进入新集清空输入，防止上一集数字被误带入。

RL页与推理/DAgger页共用同一个推理设置表单：动作块模式、RTC前缀步数、当前生效值和“重载Thor推理”。只在HOLD、介入/维护/保存结束且无待处理设置时开放；PLD非off时不允许同步模式。重载只重连客户端推理通信子进程、刷新握手，不重启Thor模型服务或设备SDK，不开始运动。若专家版本变化，先重载，再选择RL模式点击“应用”，显式建立绑定当前握手的新run；旧run不会随重载悄悄换身份。页面应用是本次Runtime配置，重连Runtime仍按启动文件恢复，不能声称已永久更新启动SHA。

本地架构候选已将“修改设置”和“独立重载”分开：重载允许在HOLD的介入等待或后台保存期间使用，模式/前缀修改仍需等待结束；尚未部署，见[本地验收](acceptance.md#本地架构候选2026-10-13未部署)。

点击“记录本轮评估”后，写入**本次session根目录的`policy_events.jsonl`**：`event=evaluation`、`episode_id=原UUID`、`time=IPC单调秒`、`details.evaluation={C,N,purpose:"evaluation_only"}`。不是每帧HDF5字段，不会自动改manifest的outcome，也不是SAC奖励。重复提交是追加事件，不覆盖旧行，不能把点击次数当独立实验次数。页面成功提示指写入录制队列已接受，不是新的逐帧标签。

## 实际动作边界与单位

14D 顺序：左 J1–J6、左夹爪、右 J1–J6、右夹爪。关节单位 rad；夹爪无量纲，0闭/1开。

以固定 i2rt `4b3d6b5`、YAM MJCF 和 SDK 工厂既有 ±0.15 rad 扩展为来源，每臂六关节边界如下；现场以连接后 `/status.action_limits` 和本集 manifest 的实际加载值为准：

| 关节 | 下限 rad | 上限 rad |
|---|---:|---:|
| J1 | -2.76799 | 3.29159 |
| J2 | -0.15000 | 3.81519 |
| J3 | -0.15000 | 3.29159 |
| J4 | -1.84297 | 1.72080 |
| J5 | -1.72080 | 1.72080 |
| J6 | -2.24440 | 2.24440 |

夹爪 SDK 入口已有端点余量，下发范围为 [0.02,0.98]；现在 RTC 承诺和记录复用同一限幅，不改变原物理行程。本站保存的电机端点 [closed,open]：左 [6.9,1.2289234760051873] rad，右 [6.9,1.2434195468070488] rad；映射 motor_rad=closed+u×(open−closed)。动态读取 `gripper_motor_limits_rad`，不把此处历史配置当作每台站点的标定。

**30 Hz RTC 直达路径没有独立的每 tick 目标增量/速度上限。** 状态报告 `max_target_step=null,target_slew_limited=false`。仅由位置范围推得的极端相邻目标差，每臂为 [6.05958,3.96519,3.44159,3.56377,3.44160,4.48880,0.96]；这不是推荐的安全步长。备用 100 Hz 二阶路径的 3 rad/s 不在当前 RTC 路径生效，不能回报为 0.1 rad/tick。Thor 正式合同需要正数 max_target_step，须双方明确设置专家边界，不得凭空把它说成客户端已有限制。

submitted_action/previous_command 表示归一化 SDK 入口目标，不是 SDK 内部软限力后电机 PD 最终目标，也不是反馈。本站仓库配置改为两侧3 N官方受阻软限力，现场生效状态另查验收；它不保证接触瞬间峰值。没有关闭硬限位或底层保护，见 [硬件](hardware.md#夹爪受阻软限力)。

## 接口、检查和部署

- `POST /pld/settings`：mode，以及可选 run_id/contract_sha256/snapshot_sha256。
- `POST /pld/evaluation`：episode_id/correct/total，0≤C≤N 且 N>0，只记整轮评估。
- 旧 `/parts/grasp`、`--parts-config`、`hil.parts` 已移除；旧启动参数须显式迁移，不能带着旧 RL 同时运行。
- 新 CLI：`--pld-config configs/pld_client.json`。non-off 仅支持训练式 RTC、30 Hz 直达 SDK；TDA已移除，不启用本站100 Hz二阶。

离线测试覆盖原 token、真正上一提交目标、五模式、probe-only 能力、缺专家/错身份、未知字段、取消/拒收、真实子进程 WebSocket、原 UUID/HDF5/视频/晚到审计和 mock Runtime。mock 结果不等于硬件复验成功，也不代表现在状态。

上线须按 [部署边界](deploy.md) 核对当前 unit/drop-in（移除旧 --parts-config，换成新配置），集中部署一次；用户已确认支撑并授权本次重启，不授权自动连接或运动。双方先两轮 probe，核对三路视频/有效位、原输入与 token、完整 trace、前缀来源、提交值、取消与误介入；正式 actor 加载并支持 shadow 后再测全链路尾延迟。正式 actor/物理合同确认后才由用户开启 collect。不得从诊断 probe 推导修正效果或训练就绪。

服务端依据：用户提供的 condapi `docs/reports/rl/pld-sac-block-v2/README.md`、`packages/pld-rl/src/yam_pld_rl/{runtime,probe_runtime}.py`，本轮只读，未修改或部署 Thor。

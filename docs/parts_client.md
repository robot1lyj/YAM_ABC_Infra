# PARTS RL 客户端

本页是 YAM 侧 RL 运行、协议与原始记录的唯一 owner。实现是可选扩展，默认 `off`；本轮只做模拟与离线验收，未部署 IPC、未运行真实残差动作、未验收 Thor 的 actor/learner。普通推理和 HIL 的现有合同不变。

## 页面与职责

工作台新增同级 **RL** 入口，不新增机械臂控制模式。打开页面只切换显示，不断开机械臂、不自动开始、不发送残差。RL 执行使用 `inference` 的控制路径；HIL 仍走既有人工接管，不在本轮将残差训练混入 HIL。

页面展示双臂高度、目标高度、夹爪实际开度、新鲜夹爪绝对力矩、实际关节残差、自动阶段及未满足条件。资格由 `rules_auto_v2` 纯规则选择器生成，不再依赖人工“抓取接近”按钮，也不调用 VLM。缺少桌面标定时显示无效，不能拿基座 Z 冒充离桌面高度。页面仍保留暂停与软件停止；这两项不替代实体急停。

四个 RL 运行状态是启动配置，不是页面可随意切换的按钮：

| 状态 | 执行动作 | 记录 |
|---|---|---|
| `off` | 原基础策略 | 无 PARTS sidecar |
| `shadow` | 原基础策略完全不变，零物理残差 | 候选、资格、观测、力矩和协议缺口 |
| `collect` | 仅当前活动臂的6个关节加残差 | 包括探索声明，供服务端审计 |
| `eval` | 同样的合成路径，但回复必须声明不探索 | 独立评估，不自动转训练集 |

控制 owner 负责资格与最终目标；唯一 SDK 写入者仍为现有设备层。网络复用单在途 policy worker；磁盘 sidecar 有界非阻塞入队；停录后的整理、哈希和上传在离线进程。队列溢出不丢掉问题冒充完整，而是显式不完整并暂停策略，设备保持控制权。

## 固定配置与尝试状态机

模板：[parts_client.json](../configs/parts_client.json)。入口高度左右均为50 mm；用户确认两侧释放判据为实测开度≥0.8、持续≥0.3秒且至少10次新鲜反馈，其余未确认的研究参数保留 `null`。`collect/eval` 缺参数时启动拒绝，当前只支持30 Hz训练式RTC直接目标通道；不开本站二阶或TDA处理。

现场仍需明确：每臂 `h_goal_m / minimum_height_m / budget_s / B_rad[6]`、桌面高度标定，以及闭合变化量、张开命令阈值、抓到的力矩连续确认时间与新鲜度、最大确认间隔、目标连续性边界和固定奖励配方/行为合同。不配置抓取区或放置区，不做 XY 空间门禁。

桌面标定字段：`base_to_table` 有限刚体4×4矩阵、桌面坐标系内单位 `normal[3]` 和 `point[3]`、`frame`、`calibration_id`。使用官方 `linear_4310/grasp_site` FK，并保存位置、姿态、反馈时间、模型与标定引用。高度为变换后末端点到桌面平面的有符号距离，单位m。左右使用各自 Follower 的 SDK 年龄，不新增 CAN 读写者。

自动规则的一次性配置：

| 字段 | 含义 |
|---|---|
| `selector.schema` | 固定 `rules_auto_v2`，无手动资格或空间区域模式 |
| `open_position_min / open_confirm_s / open_confirm_samples` | 0.8 / 0.3秒 / 10次；仅用于确认释放，不是下探前门禁 |
| `close_delta / release_position` | 实际提交目标的闭合变化量、张开命令阈值；命令只启动判断，不替代反馈 |

未确认值保留 `null`。无完整规则的 shadow 只记录缺口，不产生抓取资格；collect/eval 在启动时拒绝。

左右各自维护持物历史，不把“低力矩”直接视为空手：

| 自动阶段 | 进入/退出依据 | RL 行为 |
|---|---|---|
| `UNKNOWN` 等待同步 | 新会话或反馈失效；等待新鲜高度/力矩 | 不启用 |
| `EMPTY_READY` 可抓取 | 未持物锁定、低力矩、已回入口上方；之后下降穿越50 mm | 准备候选，允许夹爪已部分闭合 |
| 抓取尝试 | 有效空手下降越界；检测实际提交目标开始闭合 | 单活动臂下降残差、等待力矩 |
| `HOLDING` 持物中 | 提交闭合后，新鲜力矩持续满足 `abs(effort_nm)>0.65` | 连续交还基础策略，不再进入抓取残差 |
| `RELEASE_WAIT` 等待释放 | 提交新的张开命令，实际尚未确认张开 | 保留持物锁定，不启用 |
| `REARM_WAIT` 等待回撤 | 未持物，但仍低位/旧尝试或承诺残差未结束 | 不启用；回撤后准备下一次 |

持物后力矩降低仅记录 `post_grasp_loss` 线索，不能清除持物锁定。只有**新的张开命令＋实测开度≥0.8持续≥0.3秒、至少10次推进的SDK反馈**才解除；重复旧快照不计次数，低于阈值或确认间隔过大重新计时。30 Hz下第1至第10次反馈跨0.3秒，不能仅凭循环次数或相机帧数判断。命令重复下发不会重复触发闭合。

张开确认只属于“已经抓到→确认释放”，不是“开始抓取”的前置条件。未持物锁定、当前新鲜 `abs(effort_nm)≤0.65`，并从入口上方下降穿过0.05 m即可进入；提前逐渐闭合不阻止进入。闭合历史早于进入时仍保存真实命令的tick/time，随后力矩确认抓到能交还基础策略，不要求再次闭合。没有RL尝试时的基础策略闭合也会锁定持物，但不生成RL成功奖励。

持物锁定期间，搬运、低位放置均不进入RL，不必等抬高到0.5 m才退出。由于取消了空间区域判断，**抓空后未持物的再次低位下降无法仅凭高度/力矩区分抓取区与放置区**，符合进入条件时会启用；不声称规则能识别任务语义。

暂停、接管、epoch变化或反馈中断会清除旧确认窗口和命令证据，已确认的持物锁定保留。恢复须重新同步新鲜反馈，不恢复旧残差。旧尝试结束、未持物且力矩低、回到入口上方、未来已承诺残差耗尽后重新准备；抓空后的重试不强制张开。

```text
READY → ACTIVE_DESCENT → ACTIVE_CLOSURE → EXIT_PENDING → WAIT_REARM
  ↑                                                        |
  └──── 未持物＋低力矩＋回撤＋旧承诺残差结束 ─────────────────┘
```

进入条件是有效反馈从入口上方下降穿越入口，自动生成 `eligible / empty_hand`；无需实际张开或XY区域条件，但仅低力矩不能覆盖历史持物锁定。入口迟滞带内的缓慢下降不会丢掉先前的上方证据。启动时已经在入口下方记 `entry_missed`，不补造穿越；双臂同时进入时按轮换优先级选一臂并记录竞争结果。同一时刻最多一个活动尝试。

闭合按**实际提交目标的累计下降量**识别，不要求单帧跳变。实际闭合以后才确认力矩：SDK有符号 `effort_nm` 原样保存，判断用 `abs(effort_nm) > 0.65`，不是 `>=`。必须是新鲜且 SDK 时间戳推进的反馈，重复旧快照不延长确认。0.65 Nm 是电机反馈阈值，不是夹指接触力。

力矩满足持续条件只提出成功；真正交还新基础策略的那一tick仍须满足有效条件，才记录一次成功+1。超预算、最低高度或未成功又重新张开为失败0；暂停、接管、epoch改变、无效反馈或记录故障为取消，奖励 `null`。EXIT停止采用新探索，取得以实际状态和最终承诺前缀为条件的新基础块，并在连续性检查后交还。

支持的显式奖励配方为 `negative_absolute_height_error_v1`，必须提供非空 `schema`、有限 `height_weight / grasp_weight`。下降阶段每个实际policy tick记录 `-abs(height-h_goal)`，闭合/等待段不重复加高度项，交还成功/失败只计一次抓取项；attempt摘要保存高度项累计与加权总和。取消不生成失败奖励；客户端奖励不等于服务端已经审核可训练。

## Thor 协议扩展

**需模型端配合，未在本轮改动或验证 condapi。** 普通 `off` 的 OpenPI 请求不变；RTC保留 `{type:"infer", obs, rtc}`，可另加 `parts`。

metadata 的 `parts` 声明：

- `protocol="yam-parts-v1"`、`contract_sha`、`supported_modes`；
- `residual_space="joint_delta_rad"`、`horizon=50`、`state_dim=14`、`action_dt=1/30`；
- `feature_schema_id / behavior_manifest_ref`；
- `selector_schema="rules_auto_v2" / selector_config_sha`，必须与本次运行配置一致；
- `per_arm.left/right.indices` 分别0–5、7–12，各自固定 `B_rad[6]`。

请求包含 `protocol / contract_sha / mode`，`context` 中的 `run_id / session_id / epoch / request_id / observation_id / observation_policy_tick`，`active_arm`、每臂阶段/自动资格/持物锁定/原因/标定/新鲜度，以及 `elapsed_s / confirmation_s / force_valid / effort_nm / pose_valid`。无效力矩是 `null`，非活动臂elapsed为0。

自动资格是行为合同，Thor 需重新确认 `contract_sha / behavior_manifest_ref`；v2去掉空间门禁与下探前张开门禁，旧v1合同不能静默沿用。`selector_config_sha` 是实际规则参数的 SHA256（排序键、紧凑 JSON），包含桌面高度、入口/张开/闭合/反馈确认参数和0.65阈值，**不是模型或检查点指纹**。Thor仅确认接收的规则合同，资格仍由客户端负责；本轮未改Thor，也未验证实际服务支持这些字段。

`scheduler.targets[50,14] / valid_mask[50] / committed_mask[50]` 只表示本客户端已知的最终目标；未最终确定的未来预测不能伪装成最终队列，填零且valid=false。committed是valid子集。另保留已承诺前缀每项的原来源。Thor须按掩码处理，不能将零填充当真实目标。

回复仍返回逆变换后有限的绝对 `(50,14)` `actions`，并回显 `parts` 合同/上下文、固定 `behavior_snapshot_id`、左右 `candidates`：`u[50,6]` 范围[-1,1]、固定 `B_rad[6]`、bool `editable_mask[50]`、非空 `actor_snapshot_id`、bool `exploration_applied`。同一run的actor快照不允许静默变化，eval禁止探索。

features提供 `feature_schema_id`，以及原生 `z` 和其 `shape/dtype`，或可校验的 `feature_ref/sha256`。客户端不进行降维补造。reply上下文、单位、边界或时效不符：collect/eval清计划并HOLD，shadow只记录缺口、基础动作不变。

合成只在**未承诺**目标上执行 `base + B*u`，只改活动臂6个关节，不改另一臂或夹爪。复用现有硬限位，并检查合成高度和连续性。RTC已承诺前缀直接复用最终目标，不再编辑；新回复保留旧前缀的原来源，迟到不平移时间轴。

## 原始包与发布边界

```text
parts_<run_id>/
├── run.json / recording_status.json
├── requests.jsonl / requests.h5
├── events.jsonl / attempts.jsonl
├── episodes/<episode_id>/manifest.json + segments/MP4/HDF5
└── publication.json
```

`requests.jsonl` 是请求、时间和数组引用；HDF5保存原生50步、最终承诺前缀、scheduler掩码、双臂候选与features，不重复写整套候选到每个视频帧。未收到结果的请求也保留并明确说明取消原因。原生前缀的基础预测不可逆恢复时，`prefix_base_available=false`，不得反推出不存在的base。

逐帧 `parts` 保存高度/反馈、当前attempt/阶段、reward与event_refs、来源、实际物理残差、约束及候选引用；另保存 `selector_schema / selector_config_sha / selector_context`。每臂包含 `eligible / empty_hand / selector_state / holding_locked / actual_open_confirmed / rearm_ready / reason_codes` 和原始反馈，不能用低力矩覆盖已锁定的持物历史；`closing_detected / closure_tick / closure_time`保留提前收爪的真实来源。

`selector_context` 保存控制时间、epoch、是否策略执行、判断前的尝试阶段、活动臂、未来承诺残差臂和本tick进入的臂；每次录制开始另存一次规则状态 checkpoint，避免录制从搬运中途开始时丢掉持物历史。状态变化、资格变化、闭合、张开请求、实际释放、持物锁定及力矩下降线索均保存为事件，并关联反馈、FK和先前实际提交命令。

attempt包含开始/闭合/成功提议/最终交还时间与tick、终态、奖励和视频区间。`adopted_sources` 是最近64项摘要，超出计数说明；完整来源依逐帧记录，不把摘要当全集。

停录/断开后运行finalize，将已关闭的episode复制到run包、按 `(epoch, observation_id)` 关联三路视频，再检查数组/前缀/成员/文件SHA256。原数据不改写。换任务或恢复录制在现有HOLD数据事务内生成新run，替换记录对象，不重连SDK，不混任务身份。

`client_complete` 仅表示客户端包闭合完整，**永远不授予 `training_ready`**。服务端还要重建奖励、时间与行级可训练资格，查快照/特征/分组泄漏；mock与eval都不能当训练数据。不会自动把RL字段塞进现有LeRobot转换器或直接训练。

outbox是独立持久队列，故障可重试，重复ACK幂等，发布后源文件不可变。本轮提供已挂载目录的transport，不猜测Thor上传端口或HTTP接口；ACK只说明接收，仍不是训练READY。

## 启动与离线验收

无硬件预览（打开RL入口，默认关闭）：

```bash
uv run --no-sync yam-workstation --mock --mode inference --web-port 8886 \
  --parts-config configs/parts_client.json
```

真实RL启动沿用设备服务入口并传 `--parts-config <已确认配置.json>`；启用新设备代码依现有部署边界处理，不在页面浏览时更新设备或释放力矩。模拟配方仅在mock模块内，不能复制当现场标定。

```bash
# 各output必须是尚不存在的新目录。全程不连接SDK/Thor。
uv run --no-sync python -m yam_abc_reproduce.hil.parts mock /tmp/parts-shadow --mode shadow
uv run --no-sync python -m yam_abc_reproduce.hil.parts mock /tmp/parts-collect --mode collect
uv run --no-sync python -m yam_abc_reproduce.hil.parts mock /tmp/parts-eval --mode eval
uv run --no-sync python -m yam_abc_reproduce.hil.parts validate /tmp/parts-shadow
uv run --no-sync python -m yam_abc_reproduce.hil.parts verify-selector /tmp/parts-shadow

# 使用已结束录制的实际episode；不会恢复运动。
uv run --no-sync python -m yam_abc_reproduce.hil.parts finalize <run目录> --episode <episode目录>

# 此示例只向本机目录传输；实际接收地址/挂载需另行明确。
uv run --no-sync python -m yam_abc_reproduce.hil.parts publish /tmp/parts-shadow \
  --outbox /tmp/parts-outbox --destination /tmp/parts-receiver
```

`verify-selector` 使用录制中的 FK、SDK反馈、提交动作及调度上下文重算自动规则；校验资格、空手、阶段、持物、张开确认、闭合历史和原因，发现缺帧或配置不符即失败。它不重新证明桌面标定、物体接触或服务端训练资格。`validate` 对新自动规则包同时执行该复核；旧v1自动规则包提示使用原版本复核，不用新规则重写旧证据。

验收覆盖完整抓取→搬运→低位放置→释放→回撤→再次抓取，包含成功/失败/取消、提前收爪不挡进入、部分闭合无张开证明仍可进入、持物后力矩下降、高力矩不给空手资格、0.8/0.3秒/10次释放边界、旧反馈/重复采样、XY不设门禁、慢速穿越、双臂竞争及承诺残差阻止重入；并保留shadow零残差、collect/eval只改活动关节、RTC前缀不变、记录故障、哈希篡改与上传幂等的回归。未覆盖真实接触力、物理奖励有效性、实际网络服务端实现与长时资源性能；正式采集前先补齐高度及研究参数并对接新合同，再在获准场景运行shadow，最后进入collect。

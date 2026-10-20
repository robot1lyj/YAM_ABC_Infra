# Thor / condapi 对接合同

本页负责 YAM 控制侧契约；模型训练、检查点转换及 Thor 服务由 condapi 管理。现场服务版本和性能须按[验收](acceptance.md)单独核对；历史快照和测量留在 [历史接口记录](archive/condapi_interface_20260922.md)。

可选 PLD-Grasp 扩展保留原 `{type,obs,rtc}`，外层增加 `pld`，返回仍为50×14；五模式、身份与完整响应记录归 [客户端合同](pld_client.md)。旧 PARTS 已移除，不兼容旧配置。默认off，先probe；握手仅支持off/probe时不能开启其他模式，本地mock不当Thor验收。

## v1 接口验收合同

| 内容 | 当前合同 |
|---|---|
| 输入状态 | `observation.state`：有限14D，左6关节/左夹爪/右6关节/右夹爪 |
| 输入图像 | `observation.images.top_rgb/left_rgb/right_rgb`，RGB HWC uint8，角色对应 station 序列号 |
| 指令 | `prompt` 为任务英文 task；中文显示名不替代它 |
| 输出 | `actions` 有限 (50,14) 绝对目标；关节 rad，夹爪名义0闭1开 |
| 输出逆变换 | Thor 负责反归一化、关节delta还原、32D填充裁到14D；IPC不再次加状态或反归一化 |
| 控制侧边界 | 有限夹爪越界值先裁到[0,1]，关节按 SDK 硬限位；NaN/Inf/错形状拒绝 |
| 时间 | 动作间隔1/30秒；第0步的执行对齐由下述三种模式分别定义 |
| 来源 | 不要求模型名称、后端或检查点指纹；换模型仍需保证动作语义和相机视角一致 |

IPC 使用相机配对帧并打包；模型训练一致的 resize/padding/normalize 由 Thor 的 policy 处理。摄像头分辨率不等于模型输入分辨率。配置值和形状检查不能单独证明训练/现场开度标定一致。

### 普通 OpenPI WebSocket

连接后消费 msgpack metadata；发送扁平 observation 字典，使用 msgpack-numpy 语义；读取 actions 及可用计时。单连接最多一个在途请求，本地 token 关联 epoch/request_id/obs_id，无需服务器回显 IPC 单调时间。

固定现场常用地址为 `ws://192.168.250.1:8000`，当前可达性需现场检查；端口不是普通/RTC的可靠识别方式。同地址可以换服务，RTC必须通过握手合同。IPC只需轻量 deploy 依赖，不运行 Torch/JAX/TensorRT；安装见 [环境](environment.md)。

## 推理周期与产物

- `sync_hold`：完整执行50步，之后保持并请求下一块，从第0步开始；不融合动作块。
- `rtc`：训练式前缀条件推理，30 Hz 明确 tick 与承诺动作，见下。
- 默认30 Hz直接SDK，本站100 Hz二阶关闭。普通模型可使用用户指定的夹爪低值实验变换 `x<0.3 → 0.1`；RTC与已记录动作回放不使用该变换。
- H50是预测长度，不是去噪次数、控制Hz或所有模式的固定执行长度。

不要把旧 `raw/smooth/ensemble` 当作页面可选方案；历史记录仍可分析，不重新启用已移除的产品模式。

## 训练式 RTC

客户端适配见 [rtc_protocol.py](../yam_abc_reproduce/hil/rtc_protocol.py)。请求包含 `type=infer`、`obs`、`rtc`，调用方明确给出 `target_start_tick` / `observation_policy_tick=k` 和绝对 `(d,14)` 已承诺前缀。

握手要求训练式 RTC、H50/14D、30 Hz及服务端声明的最大 d；回复需 `server_timing.rtc_used=true` 且前缀不变。新块第0步对应k，接入从k+d开始。前缀必须是经当前硬限位处理、随后实际执行的目标，不是还会被滤波改写的预测。

`RtcTimeline` 使用主机到达时间近似映射 policy tick，**不是已标定的相机曝光时刻**。相机预热不消除逐帧延迟。默认d=9是本地配置，需根据当前服务全链路计时、离散tick与迟到率复测；不能超过服务支持上限。迟到丢弃并按安全策略保持，不改慢动作周期、延后承诺时刻或静默退回普通推理。

每tick核对实际提交与承诺；HOLD、介入、急停、重置和切模式清旧时间轴。RTC不得叠加动作块融合、普通夹爪trick或本站二阶滤波。本仓库不负责检查点转换、JAX→TensorRT数值核对或启动Thor。

## 故障与独立更新

### Human RTC 人工参考扩展（控制侧实现，Thor待对接）

参考 VAM `1b32062` 的 [human_rtc.py](https://github.com/Virlus/VaM/blob/1b32062cae0aca2edcb60cead74f81145cbfdbd9/deployment/utils/human_rtc.py)、[policy_connector.py](https://github.com/Virlus/VaM/blob/1b32062cae0aca2edcb60cead74f81145cbfdbd9/deployment/consumer/policy_connector.py)。IPC保持30 Hz和H50，不照搬100 Hz时间轴。本站默认配置请求启用，只有Leader HIL、原生RTC、PLD关闭、Leader在线且握手声明兼容能力时实际启用；旧Thor继续原有人工/交还行为，状态明确未支持。键盘HIL、普通推理、回放、遥操作和采集不使用本扩展。

握手增加 `human_rtc={schema:"yam_human_rtc_v1", modes:["shadow","handover"], max_prefix_attention_horizon:49, max_velocity_decay_steps:49}`；原有RTC的H50/14D/30 Hz要求不变。**这是新增对接合同，不代表现有Thor已经实现。**

请求保留 `{type:"infer",obs}`，增加外层 `human_rtc`：

- `schema`、`mode`（shadow/handover），以及原本地token的`epoch/request_id/observation_id/observation_policy_tick`，不另造一套计数。
- `action_dt_s=1/30`、有限绝对`reference_actions(50,14)`、`reference_start_tick`。参考维度与状态相同；只作模型生成引导，**不是committed_actions，不能据此直接驱动机器人**。
- `prefix_attention_horizon`和`velocity_decay_steps`默认各20，对应本站约0.667秒；两者分别控制模型引导范围和人工参考的速度衰减，不能解释为同一种延迟。
- `source_tick`、`reference_source`、`source_request`、`source_model_index`保留参考来源和位移。shadow的参考来自最近两条成功提交SDK的人工目标（含硬限位及夹爪软接管），按实际提交时间差估速度；关节默认上限2 rad/s、夹爪2归一化单位/s，按三次曲线降速并沿用SDK输入限位。`reference_start_tick`是最新人工提交tick，可能晚于观测tick；Thor须据此对齐，不能将其冒充观测时刻的目标。

shadow请求**不带rtc**，真实动作仍由人工控制；Thor基于观测和参考运行前缀引导，返回以`observation_policy_tick`为第0步的候选H50。后台与正常策略共用同一个单在途worker/传输进程，按原重规划周期请求，不等待、不积压。明确人工结束锁定的等待期继续后台推理（实际保持目标使速度逐渐估为零）；普通暂停、急停、换模式/任务、切输入/来源、故障清缓存，迟到回复不可恢复计划。

handover只在明确交还后使用新epoch、新观测、新承诺RTC请求，额外携带最近有效shadow块作为模型参考，按原观测tick差裁掉已过去的步并补齐末值。`reference_start_tick`为本次观测tick，`source_request/source_model_index`保留原候选来源。默认候选从请求时刻起最多0.5秒；过期或无候选则走原有普通RTC请求。Thor须将旧块用作新一轮推理参考，优先满足新`rtc.committed_actions`，返回的新块仍须通过现有前缀不变/目标tick/迟到检查。IPC不把旧shadow块直接改签为新epoch执行，因此不承诺零等待交还。

所有扩展回复增加`human_rtc`，准确回显`schema/mode/epoch/request_id/observation_id/observation_policy_tick/source_tick/reference_start_tick`，并声明`server_timing.human_rtc_used=true`；仅handover还必须声明`rtc_used=true`且保留真实承诺前缀。缺少确认、身份不符、非法形状或改前缀拒收。`human_rtc_used`应代表实际模型使用参考，不能只回显字段。模型侧归condapi；不得仅在IPC加入字段就宣称真机接力完成。

控制侧原始请求、完整候选/确认、取消与晚到回复写现有`policy_events.jsonl`，正常录制帧仍保存`policy_reply`。shadow标记`execution_eligible=false`，不能作为实际执行动作、专家示范或SAC经验；介入等待删帧规则不变，sidecar不受删段影响。测试范围和部署状态见[验收](acceptance.md)。

Thor通信、动作规划不拥有SDK。HOLD且无活动录制时可切模式/来源、独立重载对应子进程；新配置不会自动运动。网络/协议/规划故障清计划并HOLD，SDK故障仍是独立FAULT，不能用策略重载清除。新版故障分域已离线验证，待IPC部署；生命周期见 [架构](dagger_architecture.md#故障等级与恢复合同)。

## 计时和记录边界

IPC记录本地观测、请求、回复和提交时间；Thor的 `infer_ms` 是服务器处理时长。跨机monotonic不能直接相减；往返减infer不等于纯网线延迟，还含打包/调度/服务等待等。

不记录模型名称/检查点/后端指纹。**当前逐帧诊断仍有 policy_url**，与早期“地址也不记录”的要求不一致；本轮如实登记，未悄悄修改数据字段。地址不用于模型身份或推理门禁，分享数据前应检查其网络信息。完整字段归 [数据合同](hil_dataset_fields.md)。

历史无电机探针、RTC迟到和现场试验说明协议曾运行，不代表当前检查点性能或抓取成功率；不再把旧“尚未联调”或单次时延当作当前状态。

## 训练数据交接

原始MP4/HDF5/JSON由IPC记录，LeRobot显式离线转换。必须区分模型目标、最终提交目标和实测反馈；人工专家训练使用显式专家导出或经测试的 expert_valid 筛选，不能把整集策略/等待当人工示范。

交接包含数据版本/split、14D顺序/单位/方向、fps/action_dt、相机角色、任务文本、等待删段规则、专家筛选方式和转换报告。norm由condapi按训练split管理，YAM不另造训练规范。本轮不验证condapi加载器或更改模型端协议。

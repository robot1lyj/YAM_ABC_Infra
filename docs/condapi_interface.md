# Thor / condapi 对接合同

本页负责 YAM 控制侧契约；模型训练、检查点转换及 Thor 服务由 condapi 管理。当前源码基线为 `0bc1be5`；不代表现场服务版本或性能。历史服务快照和测量保留在 [历史接口记录](archive/condapi_interface_20260922.md) 与 [验收](acceptance.md)。

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

- `sync_hold`：完整执行50步，之后保持并请求下一块，从第0步开始；不做 TDA。
- `tda_smooth`：按请求期间已消费队列步数对齐，复用 OpenArm-vr 的重叠队列融合，含夹爪；不是 RTC。
- `rtc`：训练式前缀条件推理，30 Hz 明确 tick 与承诺动作，见下。
- 默认30 Hz直接SDK，本站100 Hz二阶关闭。普通模型可使用用户指定的夹爪低值实验变换 `x<0.3 → 0.1`；RTC与已记录动作回放不使用该变换。
- H50是预测长度，不是去噪次数、控制Hz或所有模式的固定执行长度。

不要把旧 `raw/smooth/ensemble` 当作页面可选方案；历史记录仍可分析，不重新启用已移除的产品模式。

## 训练式 RTC

客户端适配见 [rtc_protocol.py](../yam_abc_reproduce/hil/rtc_protocol.py)。请求包含 `type=infer`、`obs`、`rtc`，调用方明确给出 `target_start_tick` / `observation_policy_tick=k` 和绝对 `(d,14)` 已承诺前缀。

握手要求训练式 RTC、H50/14D、30 Hz及服务端声明的最大 d；回复需 `server_timing.rtc_used=true` 且前缀不变。新块第0步对应k，接入从k+d开始。前缀必须是经当前硬限位处理、随后实际执行的目标，不是还会被滤波改写的预测。

`RtcTimeline` 使用主机到达时间近似映射 policy tick，**不是已标定的相机曝光时刻**。相机预热不消除逐帧延迟。默认d=9是本地配置，需根据当前服务全链路计时、离散tick与迟到率复测；不能超过服务支持上限。迟到丢弃并按安全策略保持，不改慢动作周期、延后承诺时刻或静默退回普通推理。

每tick核对实际提交与承诺；HOLD、介入、急停、重置和切模式清旧时间轴。RTC不得叠加TDA、普通夹爪trick或本站二阶滤波。本仓库不负责检查点转换、JAX→TensorRT数值核对或启动Thor。

## 故障与独立更新

Thor通信、动作规划不拥有SDK。HOLD且无活动录制时可切模式/来源、独立重载对应子进程；新配置不会自动运动。网络/协议/规划故障清计划并HOLD，SDK故障仍是独立FAULT，不能用策略重载清除。新版故障分域已离线验证，待IPC部署；生命周期见 [架构](dagger_architecture.md#故障等级与恢复合同)。

## 计时和记录边界

IPC记录本地观测、请求、回复和提交时间；Thor的 `infer_ms` 是服务器处理时长。跨机monotonic不能直接相减；往返减infer不等于纯网线延迟，还含打包/调度/服务等待等。

不记录模型名称/检查点/后端指纹。**当前逐帧诊断仍有 policy_url**，与早期“地址也不记录”的要求不一致；本轮如实登记，未悄悄修改数据字段。地址不用于模型身份或推理门禁，分享数据前应检查其网络信息。完整字段归 [数据合同](hil_dataset_fields.md)。

历史无电机探针、RTC迟到和现场试验说明协议曾运行，不代表当前检查点性能或抓取成功率；不再把旧“尚未联调”或单次时延当作当前状态。

## 训练数据交接

原始MP4/HDF5/JSON由IPC记录，LeRobot显式离线转换。必须区分模型目标、最终提交目标和实测反馈；人工专家训练使用显式专家导出或经测试的 expert_valid 筛选，不能把整集策略/等待当人工示范。

交接包含数据版本/split、14D顺序/单位/方向、fps/action_dt、相机角色、任务文本、等待删段规则、专家筛选方式和转换报告。norm由condapi按训练split管理，YAM不另造训练规范。本轮不验证condapi加载器或更改模型端协议。

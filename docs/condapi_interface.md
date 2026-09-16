# condapi 对接约束（2026-09-14 架构基线 v1）

这是本项目的适配约束，不取代 condapi 的模型/训练事实所有者。
源仓库 `/home/wuyan-lyj/condapi`，本次读取 HEAD `1077699987cd66d5b95ba0402d4163250f8bc0cb`；源文件哈希、检查范围和已有工作区变更见 [本轮核查](evidence/20260914-architecture-audit.json)。2026-09-08 快照 `925d2ed3de37660c94694cc4bff292d721783108` 的 [原证据](evidence/20260908-condapi-sources.json)保留为历史。
2026-09-14架构审计只读 condapi 本地文档和代码，没有连接或操作 Thor、RK3588、训练服务器，也没有修改 condapi。系统部署和工作包归 [架构基线](dagger_architecture.md#2026-09-14-架构基线-v1)。
2026-09-15本轮新增源协议核查使用condapi本地HEAD`0717105d844dc27926a567b2a8d7976fde05a0a4`；只读`src/openpi/serving/websocket_policy_server.py`、`src/openpi/policies/yam_policy.py`、`src/openpi/training/config.py`和`packages/openpi-client`。condapi工作区另有与本次无关的`training_dashboard.service`未提交改动，本轮未触碰。本轮IPC无电机相机测量另见[验收](acceptance.md#2026-09-15非rtc异步推理缓冲仅离线模拟)，不属于2026-09-14的架构审计范围。

## 职责及数据边界

- condapi：模型训练/转换/端侧推理。AGENTS、kernel、08 当前决策及 checkpoint 交接手册默认 Pi0.5 全量微调 `pi05_yam`。deployment mode、05 旧示例中的 `pi05_yam_lora` 不覆盖当前默认，condapi 实施 agent 需按本次模型核对和更新其操作文档。
- RK3588：三相机采集、机械臂状态、prompt、控制周期与真机约束。
- Thor：Pi 系列容器中的已核验 policy，处理模型预处理/归一化/推理/输出变换。
- 网线直连；首版使用真实 openpi-client WebSocket + msgpack-numpy 语义。当前 YAM `PlainPolicyClient` 已使用以下扁平键，历史 `state/images/prompt` 包装不能替代它；仍须以选定的真实模型服务完成联调。

输入：observation.state 为 14D；图像键为 observation.images.top_rgb、
observation.images.left_rgb、observation.images.right_rgb；prompt 为字符串。
14D 顺序固定为 [左6关节, 左夹爪, 右6关节, 右夹爪]。图像正式联调使用 RGB HWC uint8；
模型端 resize/padding 与训练一致，不擅自把相机采集分辨率等同 engine 输入分辨率。
输出 actions 为有限的 (50,14)。内部 32D 不能直接发给机械臂。
训练关节 delta、夹爪 absolute；生产 policy 输出经 inverse transform 回到 absolute。
RK3588 不再次加状态或二次反归一化；真机前须通过无电机回放核实这层动作语义。
物理单位由训练数据和硬件审计绑定；不能仅因两边都14D就认为兼容。

## v1 接口验收合同

3588只负责观测输入、动作输出和控制安全，不记录模型名称、后端或指纹，也不要求Thor在握手新增这些字段。模型频繁切换时，Thor自行保证所运行policy的正确性；3588仍需核实与硬件直接相关的形状、单位和时间语义。

| 边界 | 约定与验收 |
|---|---|
| 状态 | `observation.state` 为 14D 有限浮点；顺序固定。当前 YAM adapter 设计为关节弧度、夹爪 0闭/1开，但驱动与实物仍需核验；condapi 历史数据单位仍待审计，不能直接宣称一致 |
| 视觉 | `top_rgb/left_rgb/right_rgb` 为 RGB HWC uint8；top/left/right 的序列号及安装视角由 station 绑定。RK 负责有效配对，Thor 负责训练一致的 resize/padding/normalize；不得漏相机或复用错侧画面 |
| 任务 | 工作台中文 name/instruction 供操作员使用，英文 task 作为 `prompt`；数据保留 task UUID/版本。服务不能静默改用无关默认 prompt |
| 动作 | Thor本轮反馈：`actions`为有限`(50,14)`绝对目标；0–5/7–12为左右6关节rad，6/13为左右夹爪0关/1开。Thor负责反归一化、关节delta还原、32D裁到14D；3588不二次反归一化或再加当前关节位置。关节按SDK硬限位，有限夹爪越界值在控制侧裁到[0,1] |
| 时间 | 3588本地配置`action_dt`（秒），第0步直接绑定本机observation参考时刻；收到回复后按已经过去的动作周期裁掉前缀。30Hz控制、H50、10步去噪、动态预取是不同参数；不配置额外第0步偏移 |
| 会话 | RK 本地保存session/epoch/request_id/obs_id与观测、发送、接收、提交时刻。普通协议按单连接单在途请求关联，不要求Thor回显这些字段；超时关闭旧连接并保持，明确开始后才执行 |
| 时钟 | 跨机 monotonic 不直接相减。RK 测本地往返和观测年龄，Thor 测本地处理时长；两者分别报告，不能把往返减 server timing 后简单称为纯网络时延 |
| 错误 | 缺少观测/动作字段、单位或动作周期不适用、NaN/Inf、错误形状、过期或错epoch结果均不得进入执行；有限夹爪越界值按控制侧限幅，不把它误判为协议故障；不通过补零、截断真实token或延长动作有效期掩盖错误 |

真机前先做无电机回放，核实50×14有限绝对动作、物理单位和3588本地`action_dt`。换模型不因名称、后端或指纹而自动阻挡；若新模型的动作单位/时间语义改变，须更新3588配置并重新回放，不能把不同语义直接交给同一控制映射。
2026-09-15 Thor侧提供上述动作合同和输出逆变换核对结论；2026-09-16 IPC已收到真实Thor一次有限50×14返回，但使用合成黑图/零状态，不能由数值本身独立证明rad/绝对目标语义或真机回放。夹爪输出变换不保证数值裁剪，本次最小值约-0.000089，因此控制侧在验证50×14有限值后裁到[0,1]，并保留原来的关节SDK硬限位与非有限值拒绝。协议测量范围见[验收](acceptance.md#2026-09-16ipc部署与真实thor无电机协议探针)。

## Thor 服务的实际缺口

本地核查发现普通 `scripts/serve_policy.py` 从配置/checkpoint 创建 policy，再交给 `WebsocketPolicyServer`；默认监听 `0.0.0.0`，部署时需限定容器端口暴露的网卡范围。该入口不提供直接选择 W 的 TensorRT engine 参数。现有 W 证据来自 `scripts/thor/benchmark_suite.py` 的离线调用；没有本次 RK→W 网络服务证据。

因此 P4 必须明确交付实际后端：沿用普通已核验 policy 完成协议回放，或在 condapi 将 W sampler 包装为普通 `infer(observation)` policy，保持完整预处理/反归一化/14D 输出，再做同输入的直接调用与 WebSocket 输出对照。不得把 benchmark 命令当常驻服务命令，也不得把不同后端的测量互相替代。新微调 checkpoint 的转换/缓存/engine 独立重建与回放步骤只由 condapi `docs/reference/thor/12_checkpoint_handoff.md` 持有。

## 推理周期与产物

H50 是预测长度，num_steps=10 是去噪次数，都不等于控制Hz或执行全部50步。
Thor自行管理模型、norm、后端及切换后的预热；3588不保存这些模型信息。
先保留普通推理路径；RTC off 起步，异步请求不等于算法RTC。
启用 prefix-conditioned RTC 需要服务端明确支持，且保留关闭/回退路径。

2026-09-15控制侧新增非RTC时间戳动作缓冲及可关闭的同目标时刻融合，仍消费普通`infer(observation) -> {"actions": (50,14)}`；**Thor无需为此改传输协议，也不要求模型RTC支持**。`epoch/request_id/observed_at`由RK单在途本地关联，跨机monotonic不直接比较。第0步按本机observation参考时刻对齐，`action_dt`由3588本地配置；不再增加额外时间偏移参数。这比KAI0基于请求前控制步号、返回后跳过已过去步数的方式更直接使用本机观测时间，非模型侧RTC。

本次直接检查condapi `WebsocketPolicyServer._handler`和`YamInputs/YamOutputs`：普通请求可直接发送扁平observation字典的msgpack-numpy字节帧，不必增加RTC envelope；连接后第一条服务消息为msgpack metadata，回复含`actions`及`server_timing.infer_ms`。YAM客户端保持同一连接上单在途请求，封包/解包与`openpi-client`一致；[本地无模型协议测试](../tests/test_condapi_wire.py)已用condapi真实handler对三图/状态/prompt往返验证。IPC当前安装`openpi-client 0.1.0`、`websockets 16.1.1`、`msgpack 1.1.2`，无需Torch/JAX；缺少时按YAM `pyproject.toml` 的`deploy` extra安装。Thor端的模型、norm、tokenizer、TensorRT运行依赖仍只属于condapi系列容器，不能装到IPC来代替服务。

普通condapi握手先发送metadata，3588客户端只消费该协议消息、不写入采集会话；推理回复仍取`actions`，不要求服务回显RK的monotonic时刻。`serve_policy.py`尚无直接选择W TensorRT engine的生产入口；如果Thor要用W，须由condapi将已核验后端包装为同一普通`infer(observation)` policy，完成预处理、inverse transform和14D输出。无模型源协议往返通过不等于真实Thor推理或硬件动作语义通过。

## 已报告的性能范围

来源：condapi/docs/reference/thor/11_pi05_candidate_test_plan.md 当前 W 小节。
W 是未量化 TensorRT BF16/FP32 + FP32时间条件缓存 + CUDA Graph + text80桶。
报告为9个输入×20次，P50 104.25ms/P95 104.94ms；H50、三相机、10步去噪。
这是 condapi 的历史测试报告，本任务没有复跑，不是以太网闭环/任务成功率验收。
报告对 JAX A 有非零动作差异；基础模型性能不能当作未来微调模型验收。
tokenizer 接口仍200，text80不允许截断有效token；长输入显式使用V的200桶，
自动路由未实现。不能把100ms目标写成已满足，不能把本地推理时延当接管时延。

## 当前控制侧实现与缺口

当前已实现本地 control epoch、request_id、obs_id、观测/接收/提交时间、动作有效期和有界队列；
切入人工/恢复/故障使旧epoch失效，网络/编码不在控制线程执行。
实际封装已使用本页扁平图像键和14D状态，IPC到真实Thor的无电机黑图及D405预览图协议探针已通过；尚未完成原始帧/真实关节状态及真机运动联调。

服务握手metadata只为完成现有协议接收，不保存模型名称、后端、指纹或服务URL；3588保存本地`action_dt`和控制请求/动作计时。字段形状检查不替代动作单位与时间语义的无电机回放。
跨机单调时钟不能直接相减；当前用RK本地请求年龄/往返统计，不宣称PTP或曝光时钟已校准。

## 采集数据的训练边界

本项目实时保存 MP4＋HDF5＋JSON 原始集，工作站/服务器显式运行独立转换，目标 LeRobot v3.0；采集控制进程不自动转换。完整 HIL 轨迹包含 policy/hold/human 来源、干预编号和阶段事件。人工纠正训练应使用显式专家导出，或由经过测试的 condapi 加载器读取 `complementary_info.expert_valid` 等标记，不能默认整集均为专家动作。本轮未修改 condapi 训练加载器，也未验证其对新增筛选字段的使用。

交接须包含数据版本与split、14D单位/方向/夹爪范围、动作来自最终提交目标而非把反馈冒充目标、实际fps/action_dt、任务文本与三相机视角、专家筛选方式及转换报告。训练norm按同一训练split和模型transform计算并由condapi随模型管理；3588的采集episode不记录模型身份或后端。

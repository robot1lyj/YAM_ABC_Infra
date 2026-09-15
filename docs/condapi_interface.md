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
RK3588 不再次加状态或二次反归一化；服务握手必须明确这层语义。
物理单位由训练数据和硬件审计绑定；不能仅因两边都14D就认为兼容。

## v1 接口验收合同

下表冻结双方交付内容；**自动 metadata 比较、完整模型来源写入以及影子执行路径尚待实现**，不是现有服务已经发出全部字段的声明。优先在现有握手 metadata 和会话 manifest 上补充，不改成第二种传输协议；具体字段结构由 P4/P6 在双方合同测试中固定。

| 边界 | 约定与验收 |
|---|---|
| 身份 | 握手声明 `contract_version=yam-thor-v1`、`model_id`、本次 checkpoint 身份/内容指纹、config/norm/tokenizer 指纹、实际 backend/engine 指纹；引用文件需可定位，不能仅用路径或模型名称代替身份 |
| 状态 | `observation.state` 为 14D 有限浮点；顺序固定。当前 YAM adapter 设计为关节弧度、夹爪 0闭/1开，但驱动与实物仍需核验；condapi 历史数据单位仍待审计，不能直接宣称一致 |
| 视觉 | `top_rgb/left_rgb/right_rgb` 为 RGB HWC uint8；top/left/right 的序列号及安装视角由 station 绑定。RK 负责有效配对，Thor 负责训练一致的 resize/padding/normalize；不得漏相机或复用错侧画面 |
| 任务 | 工作台中文 name/instruction 供操作员使用，英文 task 作为 `prompt`；数据保留 task UUID/版本。服务不能静默改用无关默认 prompt |
| 动作 | 返回键 `actions`，有限 `(50,14)`，物理空间 absolute target；明确各维单位、关节顺序、方向、夹爪端点与范围。模型端完成 inverse transform，RK 只按已审计硬件映射与限幅执行，不重复 delta/反归一化 |
| 时间 | metadata 与本地配置匹配 `action_dt`（秒）、`action_horizon=50`、动作第0步的观测/目标对齐语义；v1 暂以第0步对应观测参考时刻，必须用训练样本审计确认。30Hz 控制、H50、10步去噪、0.2s 重规划周期是不同参数 |
| 会话 | RK 本地保存 session/epoch/request_id/obs_id 与观测、发送、接收、提交时刻。普通协议目前按单连接单在途请求关联，不假定 Thor 已回显这些字段；超时关闭旧连接，重建需重新验证身份并保持，明确开始后才执行 |
| 时钟 | 跨机 monotonic 不直接相减。RK 测本地往返和观测年龄，Thor 测本地处理时长；两者分别报告，不能把往返减 server timing 后简单称为纯网络时延 |
| 错误 | 缺字段、版本/单位/norm/时间步不匹配、NaN/Inf、错误形状、过期或错 epoch 结果均不得进入执行；不通过补零、截断真实 token、复用旧 norm 或延长动作有效期掩盖错误 |

元数据不完整时允许无电机冻结回放做诊断，禁止将结果晋级真机推理。正式执行前 P4/P6 应做到自动合同 gate，而非靠操作者记住字段；换 checkpoint、norm、模型预处理、引擎或 station 映射后重新验证。物理单位或时间语义不同必须形成显式、可测试的新映射，重新回放；不能填一份 metadata 就认为差异消失。

## Thor 服务的实际缺口

本地核查发现普通 `scripts/serve_policy.py` 从配置/checkpoint 创建 policy，再交给 `WebsocketPolicyServer`；默认监听 `0.0.0.0`，部署时需限定容器端口暴露的网卡范围。该入口不提供直接选择 W 的 TensorRT engine 参数。现有 W 证据来自 `scripts/thor/benchmark_suite.py` 的离线调用；没有本次 RK→W 网络服务证据。

因此 P4 必须明确交付实际后端：沿用普通已核验 policy 完成协议回放，或在 condapi 将 W sampler 包装为普通 `infer(observation)` policy，保持完整预处理/反归一化/14D 输出，再做同输入的直接调用与 WebSocket 输出对照。不得把 benchmark 命令当常驻服务命令，也不得把不同后端的测量互相替代。新微调 checkpoint 的转换/缓存/engine 独立重建与回放步骤只由 condapi `docs/reference/thor/12_checkpoint_handoff.md` 持有。

## 推理周期与产物

H50 是预测长度，num_steps=10 是去噪次数，都不等于控制Hz或执行全部50步。
服务绑定 config/checkpoint/norm/模型版本；变更后重新预热并验收。
先保留普通推理路径；RTC off 起步，异步请求不等于算法RTC。
启用 prefix-conditioned RTC 需要服务端明确支持，且保留关闭/回退路径。

2026-09-15控制侧新增非RTC时间戳动作缓冲及可关闭的同目标时刻融合，仍消费普通`infer(observation) -> {"actions": (50,14)}`；**本功能无必需Thor传输协议变更，也不要求模型RTC支持**。`epoch/request_id/observed_at`由RK单在途本地关联，跨机monotonic不直接比较。真机前仍须由condapi核对握手元数据中的`action_dt`、动作索引0与观测参考时刻的关系、absolute单位及checkpoint/norm；若索引0实际对应另一偏移，应在原握手中明确动作起点偏移并做双方回放合同测试，不能由RK猜测或靠融合掩盖。

本次直接检查condapi `WebsocketPolicyServer._handler`和`YamInputs/YamOutputs`：普通请求可直接发送扁平observation字典的msgpack-numpy字节帧，不必增加RTC envelope；连接后第一条服务消息为msgpack metadata，回复含`actions`及`server_timing.infer_ms`。YAM客户端保持同一连接上单在途请求，封包/解包与`openpi-client`一致；[本地无模型协议测试](../tests/test_condapi_wire.py)已用condapi真实handler对三图/状态/prompt往返验证。IPC当前安装`openpi-client 0.1.0`、`websockets 16.1.1`、`msgpack 1.1.2`，无需Torch/JAX；缺少时按YAM `pyproject.toml` 的`deploy` extra安装。Thor端的模型、norm、tokenizer、TensorRT运行依赖仍只属于condapi系列容器，不能装到IPC来代替服务。

普通condapi握手当前`_yam_policy_metadata()`有`robot_action_dim=14`、`model_action_dim=32`、`action_horizon=50`、image_keys/layout，但**没有**checkpoint/norm身份、`action_dt_s`、动作索引0的目标偏移、完整输出物理单位；`serve_policy.py`也没有直接选择W TensorRT engine的生产入口。普通infer回复仅回`actions`，没有服务端回显RK的monotonic时刻；当前不需要回显，因为RK用本机请求token和观测时刻匹配。真机执行前需要condapi在现有metadata中补足上述模型/时间/单位合同，且先做无电机回放；W后端若要使用，另需包装完整预处理、inverse transform和14D输出为同一普通policy接口。不能把现有形式上的协议互通认作checkpoint适配或真机就绪。

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
实际封装已使用本页扁平图像键和14D状态，但尚未完成真实Thor联调。

服务元数据会被保存，checkpoint/norm/单位/action_dt的自动契约比较尚未实现。
不能把字段形状检查当作模型语义验证。
跨机单调时钟不能直接相减；当前用RK本地请求年龄/往返统计，不宣称PTP或曝光时钟已校准。

## 采集数据的训练边界

本项目实时保存 MP4＋HDF5＋JSON 原始集，工作站/服务器显式运行独立转换，目标 LeRobot v3.0；采集控制进程不自动转换。完整 HIL 轨迹包含 policy/hold/human 来源、干预编号和阶段事件。人工纠正训练应使用显式专家导出，或由经过测试的 condapi 加载器读取 `complementary_info.expert_valid` 等标记，不能默认整集均为专家动作。本轮未修改 condapi 训练加载器，也未验证其对新增筛选字段的使用。

交接须包含数据版本与 split、14D 单位/方向/夹爪范围、动作来自最终提交目标而非把反馈冒充目标、实际 fps/action_dt、任务文本与三相机视角、专家筛选方式及转换报告。训练 norm 按同一训练 split 和模型 transform 计算并随 checkpoint 固定；不得复用基础模型 benchmark-only norm。每个 episode 需关联实际 station 与模型会话身份；现有 HIL manifest 的模型来源链尚不完整，P6 补齐后才能称为全链可追溯。

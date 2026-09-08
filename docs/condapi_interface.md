# condapi 对接约束（2026-09-08 读取快照）

这是本项目的适配约束，不取代 condapi 的模型/训练事实所有者。
源仓库 /home/wuyan-lyj/condapi，读取时 HEAD 925d2ed3de37660c94694cc4bff292d721783108。
源文件哈希及阅读范围见 docs/evidence/20260908-condapi-sources.json。
本次只读本地文档和代码，没有连接或操作 Thor、RK3588、训练服务器。

## 职责及数据边界

- condapi：模型训练/转换/端侧推理。最新 AGENTS/kernel 默认 Pi0.5 全量微调 pi05_yam。
  deployment mode、05/08 文档部分段落仍写 LoRA；视为旧配置说明，不能覆盖最新默认。
- RK3588：三相机采集、机械臂状态、prompt、控制周期与真机约束。
- Thor：Pi 系列容器中的已核验 policy，处理模型预处理/归一化/推理/输出变换。
- 网线直连；首版对接真实 openpi-client WebSocket 语义，不假定当前 YAM-ABC 的
  state/images/prompt 包装与 condapi 扁平键直接兼容，需要明确 adapter 和联调。

输入：observation.state 为 14D；图像键为 observation.images.top_rgb、
observation.images.left_rgb、observation.images.right_rgb；prompt 为字符串。
14D 顺序固定为 [左6关节, 左夹爪, 右6关节, 右夹爪]。图像正式联调使用 RGB HWC uint8；
模型端 resize/padding 与训练一致，不擅自把相机采集分辨率等同 engine 输入分辨率。
输出 actions 为有限的 (50,14)。内部 32D 不能直接发给机械臂。
训练关节 delta、夹爪 absolute；生产 policy 输出经 inverse transform 回到 absolute。
RK3588 不再次加状态或二次反归一化；服务握手必须明确这层语义。
物理单位由训练数据和硬件审计绑定；不能仅因两边都14D就认为兼容。

## 推理周期与产物

H50 是预测长度，num_steps=10 是去噪次数，都不等于控制Hz或执行全部50步。
服务绑定 config/checkpoint/norm/模型版本；变更后重新预热并验收。
先保留普通推理路径；RTC off 起步，异步请求不等于算法RTC。
启用 prefix-conditioned RTC 需要服务端明确支持，且保留关闭/回退路径。

## 已报告的性能范围

来源：condapi/docs/reference/thor/11_pi05_candidate_test_plan.md 当前 W 小节。
W 是未量化 TensorRT BF16/FP32 + FP32时间条件缓存 + CUDA Graph + text80桶。
报告为9个输入×20次，P50 104.25ms/P95 104.94ms；H50、三相机、10步去噪。
这是 condapi 的历史测试报告，本任务没有复跑，不是以太网闭环/任务成功率验收。
报告对 JAX A 有非零动作差异；基础模型性能不能当作未来微调模型验收。
tokenizer 接口仍200，text80不允许截断有效token；长输入显式使用V的200桶，
自动路由未实现。不能把100ms目标写成已满足，不能把本地推理时延当接管时延。

## 控制侧待补的协议（设计，未实现）

在适配层加入 session_id、control_epoch、request_id、obs_id、模型/合同版本、
采集/发送/接收的时间记录与动作有效期。跨机单调时钟不能直接相减；
先用本地RTT和请求年龄，跨机分段时延须测量时钟偏移/同步误差。
切入人工/恢复/故障时使旧epoch失效。新动作必须匹配当前请求、模型和合同。
控制器消费有界动作缓冲；网络推理和视频编码不能阻塞手柄采样/控制执行。

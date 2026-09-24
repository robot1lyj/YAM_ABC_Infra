# XR-1 末端动作与 YAM 正逆解接口

状态：2026-09-24 完成控制侧**离线接口和录制回放数值验收**。未接入正在运行的推理服务、页面或真机控制。Xiaomi-Robotics-1 源码位于本机 `/home/wuyan-lyj/Xiaomi-Robotics-1/xr1/`；动作语义以 `mibot/utils/io.py` 的 `ACTION_PARTS`、`recover_action` 和 `mibot/data/datasets/json_dataset.py::_arm_action` 为准。不能把先前 OpenWAM 14D 关节模型合同用于 XR-1。

## 合同和坐标系

XR-1 原生动作每块 `(30,60)`，须先在服务侧按其训练统计**反归一化**。每步与 YAM 有关的槽位：左 `0:3` 局部位移、`3:6` 局部 axis-angle、`6` 夹爪增量；右 `8:11`、`11:14`、`14`。第 7、15 槽和腰部、底盘等 YAM 没有的维度不参与机械臂目标；训练 mask 也须排除这些槽。XR-1 状态 `(1,60)` 的左 J1–J6、夹爪在 `0:6,7`，右在 `8:14,15`，第七关节槽 `6,14` 填零。

YAM 采用官方 YAM MJCF 和标准 `linear_4310` 的 `grasp_site`，左右各以**各自机械臂底座**为参考，位置为米、关节为弧度、夹爪仍用本站 0 闭 1 开。该变换只声明软件模型坐标系；底座间、相机到世界的外参及真机 TCP 误差尚未实测标定。

同一个观测姿态 `(p₀,R₀,g₀)` 对整块 30 步固定：

```text
训练编码: Δpᵢ = R₀ᵀ (pᵢ - p₀),  ΔRᵢ = log(R₀ᵀ Rᵢ),  Δgᵢ = gᵢ - g₀
推理解码: pᵢ = p₀ + R₀ Δpᵢ,   Rᵢ = R₀ exp(ΔRᵢ),   gᵢ = g₀ + Δgᵢ
```

后一步**不**在前一步目标上再次叠加 delta。逆解以配对的观测关节为首个 seed，后续用前一个解作 seed，保持同一分支；不可达或不满足关节范围的目标明确失败。夹爪数值转换与腕部 IK 分开；实际下发前仍由现有控制侧做夹爪 `[0,1]` 限幅和 SDK 关节硬限位。

## 可复用接口

- `yam_abc_reproduce.hil.kinematics.DualArmEefConverter`：`forward(14D)`、`inverse(EEF, seed14)`、`forward_batch(N,14)`、`inverse_batch(N,2,4,4; N,2; seed14)`。它复用官方 `Kinematics` 的 FK/IK；以同一个官方带夹爪 MJCF 求固定法兰→抓取点偏移。没有 SDK/CAN 副作用。
- `yam_abc_reproduce.hil.xr1_actions.XR1YamCodec`：`state60`、`robot_state` 用于模型输入；`encode(observation14, absolute_targets[N,14])` 用于录制数据反向生成 XR-1 训练动作；`decode(observation14, denormalized_deltas[N,60])` 和 `decode_targets(observation14, Xiaomi action_targets)` 用于回译绝对关节目标。`action_mask(length)` 标记有效 YAM 动作槽。
- `scripts/audit_xr1_kinematics.py EPISODE_DIR`：只读抽样审计 FK→XR-1 编码→IK，不修改原集，不把策略帧冒充人工专家帧。正式训练清洗须按 `expert_valid`、`observation_valid`、片段与 `wait_boundary` 选择样本，并确认 action 对齐；末端标签取 `submitted_action`，反馈取 `measured_state/observation_state`，不能互换。

## 最新录制集的数值验证

IPC 最新保存的 `session_20260923_153122_e6b692/episode_000006`，manifest 23,512 帧、14 段；本次只读拷贝其 `samples.h5`，未处理视频，也未改录制数据。每段从起点按 60 帧间隔抽 30 步窗口，有效观测及提交目标共 **383** 窗口。**382** 个通过编码/逆解；成功窗口 TCP 位置最大误差 **0.000103 m**、姿态最大误差 **0.000100 rad**，左右夹爪最大差 **3×10⁻⁸**。这验证数值变换，不验证真机执行精度或模型行为。

1 个失败窗口在首段第 0 帧起的第 15 步，官方受限 IK 未收敛，末端位置误差约 **0.000245 m**。该处记录的 J2 目标约 **-0.0039 rad**，越过官方模型 J2 下限 0。全本集共有 **14/23,512** 行提交目标超出官方 MJCF 关节范围（容差 `1e-5 rad`），最大超出 **0.01625 rad**。清洗时应单独标记或剔除这些行及跨越它们的窗口；不能静默钳成另一条轨迹，也不能把受限 IK 失败记成成功。

## 接入顺序

下一步由 condapi/Thor 明确提供 XR-1 服务合同：输入图像视角与预处理、当前状态、反归一化责任、回复是 `(30,60)` 原生 delta 还是 `action_targets`、动作周期与时效。控制侧将 XR-1 作为独立**动作表示**接入已有异步工作线程，先使用完整 30 步的同步等待作为时间策略；解析并 IK 转成 `(30,14)` 后才交给现有安全仲裁。现有 Pi 的 50 步、RTC 承诺前缀及 TDA 规则不能直接套到 30 步 XR-1。需完成 30 步计划/日志合同、模拟延迟/不可达目标测试和在线协议无电机测试，之后再申请现场真机短测。

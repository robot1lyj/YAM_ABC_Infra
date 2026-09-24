# XR-1 末端动作与 YAM 正逆解接口

状态：2026-09-24 完成控制侧**离线接口、录制回放数值验收和独立末端回放入口**。末端回放尚不等于 XR-1 模型在线推理；在线服务协议和真机验收需单独确认。Xiaomi-Robotics-1 源码位于本机 `/home/wuyan-lyj/Xiaomi-Robotics-1/xr1/`；动作语义以 `mibot/utils/io.py` 的 `ACTION_PARTS`、`recover_action` 和 `mibot/data/datasets/json_dataset.py::_arm_action` 为准。不能把先前 OpenWAM 14D 关节模型合同用于 XR-1。

## 合同和坐标系

XR-1 原生动作每块 `(30,60)`，须先在服务侧按其训练统计**反归一化**。每步与 YAM 有关的槽位：左 `0:3` 局部位移、`3:6` 局部 axis-angle、`6` 夹爪增量；右 `8:11`、`11:14`、`14`。第 7、15 槽和腰部、底盘等 YAM 没有的维度不参与机械臂目标；训练 mask 也须排除这些槽。XR-1 状态 `(1,60)` 的左 J1–J6、夹爪在 `0:6,7`，右在 `8:14,15`，第七关节槽 `6,14` 填零。

YAM 采用官方 YAM MJCF 和标准 `linear_4310` 的 `grasp_site`，左右各以**各自机械臂底座**为参考，位置为米、关节为弧度、夹爪仍用本站 0 闭 1 开。该变换只声明软件模型坐标系；底座间、相机到世界的外参及真机 TCP 误差尚未实测标定。

同一个观测姿态 `(p₀,R₀,g₀)` 对整块 30 步固定：

```text
训练编码: Δpᵢ = R₀ᵀ (pᵢ - p₀),  ΔRᵢ = log(R₀ᵀ Rᵢ),  Δgᵢ = gᵢ - g₀
推理解码: pᵢ = p₀ + R₀ Δpᵢ,   Rᵢ = R₀ exp(ΔRᵢ),   gᵢ = g₀ + Δgᵢ
```

后一步**不**在前一步目标上再次叠加 delta。逆解以配对的观测关节为首个 seed，后续用前一个解作 seed，保持同一分支；不可达或不满足关节范围的目标明确失败。夹爪数值转换与腕部 IK 分开；实际下发前仍由现有控制侧做夹爪 `[0,1]` 限幅和 SDK 关节目标限幅。

官方 `yam.urdf` 和 `yam.xml` 的六关节原始范围一致；但官方 `i2rt/robots/get_robot.py` 构造 `MotorChainRobot` 前，把 XML 范围的两端各扩展 **0.15 rad**，实际关节目标以该 SDK 范围裁剪。本站 IK 现在使用与 SDK 相同的有效范围，不再把已录下的 J2 `-0.0067 rad` 等 SDK 可接受目标误判为超出原始 MJCF 下限。这只是与官方**软件命令范围**对齐，不宣称电机机械极限比 URDF 大，也不允许越过 SDK 范围。

## 可复用接口

- `yam_abc_reproduce.hil.kinematics.DualArmEefConverter`：`forward(14D)`、`inverse(EEF, seed14)`、`forward_batch(N,14)`、`inverse_batch(N,2,4,4; N,2; seed14)`。它复用官方 `Kinematics` 的 FK/IK；以同一个官方带夹爪 MJCF 求固定法兰→抓取点偏移。没有 SDK/CAN 副作用。
- `yam_abc_reproduce.hil.xr1_actions.XR1YamCodec`：`state60`、`robot_state` 用于模型输入；`encode(observation14, absolute_targets[N,14])` 用于录制数据反向生成 XR-1 训练动作；`decode(observation14, denormalized_deltas[N,60])` 和 `decode_targets(observation14, Xiaomi action_targets)` 用于回译绝对关节目标。`action_mask(length)` 标记有效 YAM 动作槽。
- `scripts/audit_xr1_kinematics.py EPISODE_DIR`：只读抽样审计 FK→XR-1 编码→IK，不修改原集，不把策略帧冒充人工专家帧。正式训练清洗须按 `expert_valid`、`observation_valid`、片段与 `wait_boundary` 选择样本，并确认 action 对齐；末端标签取 `submitted_action`，反馈取 `measured_state/observation_state`，不能互换。

## 最新录制集的数值验证

只读审计 `session_20260923_153122_e6b692/episode_000006`（23,512 帧、14 段）：每段按 60 帧间隔抽 30 步，有效窗口 **383/383** 通过编码/IK。另审计 `session_20260924_134225_ded02c/episode_000001`（15,634 帧、9 段）：有效窗口 **256/256** 通过。最新一集成功窗口 TCP 位置最大误差 **0.000104 m**、姿态最大误差 **0.000100 rad**、关节最大差 **0.00186 rad**、左右夹爪最大差 **3×10⁻⁸**。这验证数值变换，不验证真机执行精度或模型行为。

修正前旧集有 14 行目标超过**原始 MJCF** 范围，最新集起始 600 帧有 14 行，最大超过原始范围约 **0.048 rad**；它们均在官方 SDK 扩展后的有效范围内。修正后两集没有超出 SDK 范围的提交目标。最新集第 0–599 帧作为连续 20 秒候选段已离线完成 XR-1 编码、官方范围内 IK 与关节分支检查；本阶段未真机回放。

## 独立末端回放

页面的“XR-1 末端回放”只预设本机 `ws://127.0.0.1:8003` 来源，配合现有“同步推理 · 完整 50 步”时序；它不会自行启动回放服务、连接设备或运动。服务启动例：

```bash
uv run --no-sync python -m yam_abc_reproduce.hil.replay_policy \
  data/episodes/<task>/<session>/<episode> --start 0 --steps 600 \
  --xr1-eef-roundtrip --port 8003
```

服务在启动时从录制的 `observation_state` 和 `submitted_action` 每 30 帧形成同一观测下的 XR-1 末端增量，再用官方范围内 IK 恢复关节目标；IK 失败或关节分支与录制目标相差超过 0.05 rad 时**整段拒绝启动**。其对外仍使用现有 `recorded_replay` 的 50×14 关节目标协议和执行游标确认，复用控制线程、安全仲裁、HOLD 与暂停恢复。这是“末端转换后回放”验收，不是直接把笛卡尔坐标发给 SDK，也不宣称 XR-1 模型已部署。最新集 600 帧候选段中，回译相对录制关节最大差 0.00211 rad、最大单帧关节步幅 0.06114 rad；两者的单帧最大步幅基本相同。

## 接入顺序

下一步由 condapi/Thor 明确提供 XR-1 服务合同：输入图像视角与预处理、当前状态、反归一化责任、回复是 `(30,60)` 原生 delta 还是 `action_targets`、动作周期与时效。控制侧将 XR-1 作为独立**动作表示**接入已有异步工作线程，先使用完整 30 步的同步等待作为时间策略；解析并 IK 转成 `(30,14)` 后才交给现有安全仲裁。现有 Pi 的 50 步、RTC 承诺前缀及 TDA 规则不能直接套到 30 步 XR-1。需完成 30 步计划/日志合同、模拟延迟/不可达目标测试和在线协议无电机测试，之后再申请现场真机短测。

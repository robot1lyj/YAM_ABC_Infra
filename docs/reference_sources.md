# 外部源码参考与适用范围

以下为 2026-09-08 的阅读快照，不代表参考仓库现在的最新状态。再次依赖其变化时重新核对，不用历史记忆推断最新代码。

| 参考 | 固定版本 | 本项目采用范围 |
|---|---|---|
| Evo-RL | `6f2db449a21e1bac750b996f2e27cac6739aa63f` | 按键干预、策略/人工来源和 episode 结果记录 |
| Kai0 | `9d93078c757840f50e75248c5c5a94ab7b41e13a` | 普通推理、非 RTC 异步动作块与时间裁剪 |
| condapi | `925d2ed3de37660c94694cc4bff292d721783108` | YAM 输入输出、训练与 Thor 推理范围 |

源路径及指纹：[Evo/Kai0 审核](evidence/20260908-hil-v1.json)、[condapi 审核](evidence/20260908-condapi-sources.json)。本机 Kai0 在 `/home/wuyan-lyj/kai0`，condapi 在 `/home/wuyan-lyj/condapi`；Evo 的 `/tmp/yam-evo-rl-reference` 克隆属于临时来源，不能假定永久存在。

## Evo-RL

审查 `src/lerobot/utils/control_utils.py`、`scripts/recording_loop.py` 和 `scripts/recording_hil.py`。

- 默认干预键为 `i`，不是握持触发。
- 循环边界切入人工，没有独立的“四臂停住等待第二次开始”状态；交还时 reset 后重新推理。
- 人工阶段跳过策略推理。部分记录用零填充无效策略；本项目用 null 和有效性标记。
- follower 与 leader feedback 可并发发送，但不是硬件同时执行。
- 其记录路径使用 `action_values`，不总是机器人处理器返回的 `_sent_action`；本项目区分策略、选择、提交和反馈。
- SO Leader 的手动撤力矩方式不移植到 YAM，YAM 使用重力补偿。

## Kai0

审查 `train_deploy_alignment/inference/arx/inference/` 下的 sync、temporal_smooth、temporal_ensembling 路径，并核对 Agilex 普通推理。

- 普通路径按块推理、顺序执行；异步路径无需 RTC 模型即可与执行重叠。
- temporal_smooth 的实际块间权重为线性过渡，裁剪用消费计数及上限，不是严格的观测时间对齐。
- temporal_ensembling 的正指数系数在该实现中偏重较老预测，不能称为优先最新预测。
- ARX `get_frame()` 依次等相机和取最新关节，没有严格时间戳配对。
- 其 RGB/BGR 处理、嵌套 CHW 键、夹爪二值化、自动回零和不足块补零不照搬到 YAM。

2026-09-15复核固定Kai0`9d93078`的[Agilex temporal smoothing](https://github.com/OpenDriveLab/kai0/blob/9d93078c757840f50e75248c5c5a94ab7b41e13a/train_deploy_alignment/inference/agilex/inference/agilex_inference_openpi_temporal_smoothing.py)与[temporal ensembling](https://github.com/OpenDriveLab/kai0/blob/9d93078c757840f50e75248c5c5a94ab7b41e13a/train_deploy_alignment/inference/agilex/inference/agilex_inference_openpi_temporal_ensembling.py)。YAM现提供可关闭的短窗口线性平滑和同目标时刻集合；与Kai0的消费计数裁剪、旧预测优先权重不同，YAM按本机观测时间裁剪、设`action_dt/4`匹配容差、新预测优先且夹爪不平均。参考算法形态不移植其ROS、动作单位或自动回位。

## condapi

接口以 [condapi 对接规范](condapi_interface.md)为本项目所有者。源项目快照的最新规范采用 Pi0.5 全量微调 `pi05_yam`；旧 LoRA 段落不能覆盖它。

历史 Thor 性能只属于对应后端、样本和测试条件，不是本工作站闭环测量，也不保证未来微调模型效果。

## RK3588 相机与编码（2026-09-14 快照）

- Intel RealSense `librealsense` tag `v2.58.3`（`dfd6aa91250f5c31521d72d627865417989bb4e7`）：用于在IPC按官方Python binding构建方式生成与GLIBC 2.35兼容的模块。
- Rockchip官方`mpp` develop（`c1ce7e1a612644e6684481afe8045d2bfa680aba`）：确认MPP层支持RK3588、Linux 6.1及rkvenc编码器，且依赖对应内核设备驱动。
- `nyanmisaka/ffmpeg-rockchip` master（`d90e3a1c18d7929383cf88c1b3da2e2d1c966cbf`）：非Rockchip官方FFmpeg分支；以最小功能构建安装到RK3588的`/opt/yam-rkmpp`，提供`h264_rkmpp`录制子进程。软件回退不依赖它。

名称枚举不作为可用证据；当前IPC硬编能力以实际开帧探针为准，见[P2证据](evidence/20260914-rk3588-p2-camera-debug.json)。

## i2rt YAM初始化（2026-09-14快照）

- i2rt官方[YAM手册](https://doc.i2rt.com/products/yam)：1 Mbit/s CAN、逐臂零重力测试、
  `linear_4310`启动标定、真实夹爪模型与重力补偿参数。
- i2rt官方[YAM Cell手册](https://doc.i2rt.com/products/yam-cell)：四臂独立CAN、逐臂漂浮
  测试、同步前leader/follower姿态匹配及0.1–0.2双边增益起点。
- i2rt官方[Leader手册](https://doc.i2rt.com/products/yam-leader)：先读teaching handle，只有
  磁铁移位或维修后才重置编码器零点。
- 固定子模块由v1.2.4 `5d47b35`快进到官方未合并PR #81 `4b3d6b5`，解决控制线程尚未
  退出便关闭CAN socket的竞态；其未合并状态是后续升级时必须复核的适用条件。现场结果见
  [P3左臂证据](evidence/20260914-rk3588-p3-left-init.json)。

### i2rt未合并PR风险审计（2026-09-14）

审计范围为当日官方仓库全部12个open PR；不整体合并第三方分支，只提取与固定基线兼容、
差异最小且可本地验证的提交。

| PR | 判定 | 本项目处理 |
|---|---|---|
| [#82](https://github.com/i2rt-robotics/i2rt/pull/82) | 必须：旧启动逻辑会把多圈线性夹爪也做±2π arm wrap修正，反馈与标定端点落入不同坐标系 | vendoring官方`e599e9d`为`0002-exclude-gripper-wrap.patch`，硬件构造前另加fail-closed检查 |
| [#61](https://github.com/i2rt-robotics/i2rt/pull/61) | 适用RK3588：重力补偿把同一次逆动力学计算重复执行两遍 | vendoring官方`3916586`为`0003-single-inverse-dynamics.patch`；数学输出不变 |
| [#81](https://github.com/i2rt-robotics/i2rt/pull/81)、[#86](https://github.com/i2rt-robotics/i2rt/pull/86)、[#73](https://github.com/i2rt-robotics/i2rt/pull/73)、[#51](https://github.com/i2rt-robotics/i2rt/pull/51) | 同类CAN socket关闭竞态的不同实现 | 已固定#81并完成反复connect/close验证，不叠加重复补丁 |
| [#31](https://github.com/i2rt-robotics/i2rt/pull/31) | CAN channel初始化及控制线程重复启动保护 | 当前固定基线已经具备两项保护，不移植该PR中其余初始化改写 |
| [#79](https://github.com/i2rt-robotics/i2rt/pull/79) | 仅官方`minimum_gello`经portal RPC同步时的信号管道泄漏 | 本平台直接持有本地i2rt对象，不走该portal路径；记录观察，不移植 |
| [#77](https://github.com/i2rt-robotics/i2rt/pull/77) | ruckig冷安装兼容；唯一消费者为flow base | 当前IPC安装已成功且机械臂采集不使用flow base；下次重建依赖时复核 |
| [#37](https://github.com/i2rt-robotics/i2rt/pull/37) | 可选Coulomb摩擦前馈 | 当前基线已有YAML参数和实现且默认关闭；未做本机辨识前不启用 |
| [#75](https://github.com/i2rt-robotics/i2rt/pull/75)、[#53](https://github.com/i2rt-robotics/i2rt/pull/53) | CI与特定LeWM部署文档 | 不改变本平台运行时 |

DM-J4310控制器状态码`C`按[达妙官方文档仓库](https://github.com/dmBots/damiao-document)
及其J4310手册定义为电机线圈过温。当前日志没有保留原始MOS/rotor温度字节，因此本项目把它
作为控制器报告的过温故障处理，但不反推具体温升曲线。

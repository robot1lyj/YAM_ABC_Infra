# 本地边缘推理与同步架构（2026-09-08）

## 决策与实现状态

用户明确以实用为先，允许放宽同步容差。当前实现见 [第一版教程](hil_quickstart.md)。
已接入统一四臂控制、官方leader镜像/重力补偿、非RTC异步重规划、连续录制和专家段导出。
现场 Thor 与 RK3588 通过以太网连接：Thor做本地模型推理，RK3588采集和控制。
第一版保留线程架构，增加有界历史与时间配对；当前采用主机接收时间，曝光时间映射尚未标定。
以下多进程/共享内存与完整时钟方案保留为有实测需求后的升级方向，不阻挡当前迭代。

## Kai0 非RTC优化审核

本地提交 9d93078c757840f50e75248c5c5a94ab7b41e13a。
目录 train_deploy_alignment/inference/arx/inference：

| 路径 | 实际优化 | 采用方式 |
|---|---|---|
| arx_openpi_inference_sync.py | 每块同步推理/顺序执行，启动预热 | 保留为基准回归路径 |
| arx_openpi_inference_temporal_smooth.py | 独立推理线程；新块裁剪；旧新块线性权重融合 | 作为首选非RTC调度参考，但重新实现时间锚点和epoch |
| arx_openpi_inference_temporal_ensembling.py | naive_async按推理起始tick裁剪；或同一tick多预测加权 | 时间对齐采用，ensemble先可选关闭 |
| arx_openpi_inference_rtc.py | 开关开启时发送prefix/推理延迟并使用RTC模型 | 用户明确暂不采用 |

不能把“不要RTC”等同于“只能等待整块执行完再推理”。目标路径是普通模型的异步重规划：
执行有效旧块时请求新块，按观测时刻对齐，切入人工立即使在途请求与动作队列失效。
当前已实现并测试非RTC异步调度，保留无预取基准作比较。

源码审计细节：
- temporal_smooth 的实际重叠权重为 linspace(1,0)，不是所有名称/参数暗示的指数平滑。
- 裁剪用自上次整合以来的消费计数k，且受latency_k上限约束；不是严格以每次观测时间为原点。
- ensembling 中较老预测排序在前，正exp_weight_m给较老预测更大权重；不能称为偏重新预测。
- ARX get_frame 取最新左右关节再逐台wait_for_frames，无时间戳配对；不能证明“同步”。
- 当前相机配置采集rgb8，而payload又BGR→RGB；本项目必须明确颜色契约并用色卡验收，不能照抄。

YAM改进：每块保存obs_time、request_id、epoch、action_dt。动作i的目标时间为
obs_time + (i + offset_steps) * action_dt，其中offset_steps必须从训练样本的obs/action对齐核验，不能猜。
过期前缀按本地执行时间丢弃，无剩余则HOLD并重新请求。新块只替换尚未执行的未来区间；
可选短时关节融合需先做任务评估，夹爪不默认平滑；完整保存raw/selected/submitted。
只保留一个在途请求和有界未来动作，禁止无限累积。模型采样周期、上层重规划周期、
执行器插值周期分离；更快下发不能把训练动作时间轴加速。

## 成熟参考与适配边界

- [Diffusion Policy 实机结构](https://github.com/real-stanford/diffusion_policy#sharedmemoryringbuffer)：
  每相机独立进程、共享内存环形缓冲；get_obs返回观测与时间戳；exec_actions提交带时间的动作，执行器异步运行。
- [UMI 双臂环境源码](https://github.com/real-stanford/universal_manipulation_interface/blob/main/umi/real_world/bimanual_umi_env.py)：
  相机历史匹配，低维状态插值，对未来时间点调度多机器人与夹爪，支持经测量的延迟补偿。
  其末端位姿/UR RTDE接口不能直接作为YAM关节/CAN接口。
- [ros2_control Controller Manager](https://control.ros.org/jazzy/doc/ros2_control/controller_manager/doc/userdoc.html)：
  read/update/write控制周期、资源所有权、生命周期与周期诊断。参考机制不等于已经移植该框架。
- [ROS2 message_filters](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html)：
  Exact/Approximate时间戳配对用于数据匹配，不会使相机同时曝光；arrival time受传输与调度影响。
- [RealSense 多相机官方文档](https://dev.realsenseai.com/docs/multiple-depth-cameras-configuration/)：
  硬件同步、带宽/USB拓扑与验证分别处理。具体RGB/深度触发能力按型号、固件与启用流核验。
- [linuxptp](https://www.linuxptp.org/documentation/ptp4l/)：硬件时间戳与PHC需网卡支持。
  PTP帮助跨机日志对齐，不能令USB相机曝光或CAN伺服自动同步。

## 四层时间契约

1. 时钟：RK3588 monotonic作为本机调度时间轴；设备曝光时钟通过测量映射，保存映射版本、偏差与不确定度。
   同时存设备原始时间、主机接收时间；时间跳变/重连使同步状态失效，重新初始化。
   Thor请求往返先由RK本地计时；只有核验网卡支持和同步误差后才启用跨机PTP分段统计。
2. 观测：相机进程发布有限历史，选择公共参考时刻t_obs与各相机最近有效帧；
   高频左右关节历史在t_obs前后插值。禁止无界外推；缺乏包围样本或误差超限则拒绝新观测。
   插值状态只给模型/记录使用；本地碰撞/跟随误差检查使用最新真实状态，不能用旧图像时刻的状态。
3. 执行：同一个双臂轨迹携带target_time；本地执行器在固定周期给四臂生成同一时间轴目标。
   四臂分别记录实际提交时间、反馈时间和跟踪误差。SDK目前未证明支持硬件定时触发/跨总线原子执行，
   因而先提供可测的软件协调；不得承诺微秒级或硬同步。任一关键臂故障，全站退出策略。
4. 数据：保存原始观测/时间、配对索引、插值状态和原始样本索引、各臂目标与提交/反馈时间、
   干预事件、同步质量；在线模型快照和离线训练对齐必须使用相同规则。

## 进程划分与验收

RK3588：控制/手柄进程；三路相机采集进程；观测封装与网络进程；记录编码进程；GUI。
同机大图像走共享内存，命令/事件走有界小消息；共享内存需序列号/读者快照协议，防止覆盖时读到半帧。
不能把当前线程内deque称为进程共享或无锁实现。先做性能基准再决定控制插值是否移到C++。

验收分别统计：
- 三路曝光差（可测时）、映射不确定度、接收差、帧龄、丢帧率；不把它们合成一个sync值。
- 左右臂状态差、四臂发布时间差、目标时间误差、跟踪误差、控制周期p99与deadline miss。
- 观测到首次有效动作的端到端延迟；按钮到最后策略/第一人工命令的时延。
- 注入延迟/乱序/丢帧/时间跳变/编码变慢/一臂异常，验证无旧epoch执行、无无界队列增长。
- 持续30分钟四臂三相机录制，报告RAM、CPU、USB吞吐、磁盘与网络吞吐。

具体容差需要结合任务速度和测量确定：例如关节误差近似速度×时间差，可用允许误差反推同步预算。
相机型号现已确认三台D405；序列号/USB拓扑待核验，没有硬件性能验收数字。

## 三台D405的最终边界（用户补充后更新）

用户确认三路均为D405。官方2025年8月D400数据手册第7.13节（文档第114页）明确：
D405不支持多相机硬件同步信号。因此本配置采用自由运行采集 + 校准时间戳软件对齐，
不配置外部master/slave同步线，不把global_time_enabled或同帧率宣称为同时曝光。
单台D405的RGB来自左成像器经ISP处理，单机RGB/深度匹配不等于三台同步。

来源：[D400官方数据手册](https://realsenseai.com/wp-content/uploads/2025/08/Intel-RealSense-D400-Series-Datasheet-August-2025.pdf)，
[D405官方规格](https://www.realsenseai.com/products/stereo-depth-camera-d405/)。

落地步骤：识别三台序列号/USB控制器 → 相同受支持stream profile与稳定曝光 →
独立采集并记录设备/主机时间与frame_number → 确认时间域和校准漂移 → 有界历史配对 →
关节历史插值 → 报告每组帧的实际时间差和质量 → 阈值超限不生成新模型观测。
30fps周期约33.3ms，自由运行相位差不会因CPU优化归零；更高帧率仅在支持的profile、
曝光与USB预算实测后选用，也不构成硬同步。若任务要求严格同时曝光，现有三台D405无法
仅靠软件达成，届时需按明确精度需求评估硬件；当前先完成软件对齐与误差验收。

# 工作记忆摘要

- **自动提交（用户2026-09-14确认）**：每次完成代码更新，执行适用检查后自动提交本次代码及配套配置、文档和记忆，无需再次询问；不混入其他agent未完成修改。随后先同步Gitea、再同步GitHub，核对同一SHA。规则来源：[AGENTS.md](../../AGENTS.md)。

本页只投影规范所有者；硬件在线、服务、网络和模型版本在使用前复查。旧摘要完整保存在 [2026-09-14核查前归档](../archive/kernel-before-architecture-20260914.md)，其中已被后续决定覆盖的状态不能恢复为当前事实。

- **架构基线 v1（2026-09-14）**：用户确认硬件到齐，要求规划与记忆先固定、其他agent实施。RK3588就是已登记的IPC，负责四臂/三相机、观测、控制、仲裁和原始记录；Thor由condapi提供模型服务。开发机/训练服务器负责管理与离线数据/训练。工作包P0–P8与依赖归 [架构](../dagger_architecture.md#2026-09-14-架构基线-v1)，未实现项不能读成现场通过。
- **硬件**：2标准YAM follower、2官方电动leader、3台D405。不是被动GELLO；夹爪精确型号、四臂接线/端接仍待核验，不要求机械臂本体S/N。三台D405的物理标签与RealSense序列号已确认：`right=260422271123`、`top=260522275397`、`left=260522271298`，并已建立IPC稳定入口；相机流兼容性和标定仍待验收。用户确认四臂各自USB-CAN接口经USB Hub接IPC；`gs_usb` 已使四个 CANable 2.5 按固定USB下联口固化为 `can_lead_l`、`can_lead_r`、`can_left`、`can_right`，仍需完成四臂接线验收；Hub型号待核，板载bcan0～bcan3仅是已枚举资源，不默认用于机械臂。来源：[硬件事实](../workstation.md)。
- **IPC现场快照**：2026-09-14 Ubuntu22.04.3、6.1.118 PREEMPT_RT、arm64；lan1=.250.2与开发机.250.1的无网关调试链路SSH已验证，Wi-Fi上网保留。Gitea专用密钥认证成功，不再待登记；当前 YAM checkout 为 `/data/YAM`，uv `0.12.13` 与 Python3.12 环境已在新路径验证。NVMe `nvme0n1p1` 已挂载 `/data`。地址全值、指纹与证据归 [硬件](../workstation.md)、[环境](../environment.md)。
- **网络与盘**：生产RK↔Thor独立网线，Thor 192.168.250.3只是预留计划，当前lan1接开发机；改变接线后必须重新核验。NVMe 已按 UUID 挂载到 `/data`，代码在 `/data/YAM`；原始数据的绝对save_root仍需在运行配置中明确，不回落eMMC。来源：[架构](../dagger_architecture.md)、[环境](../environment.md)。
- **代码和环境**：主项目yam-abc-reproduce，i2rt固定子模块，同级旧i2rt已删除；uv/Python3.12，入口uv run --no-sync yam-workstation。四模式用configs/station_hil.yaml，旧station_yam.yaml是GELLO示例；提交pyproject.toml/uv.lock，不提交.venv/数据/模型/runtime ledger。来源：AGENTS.md、[环境](../environment.md)。
- **托管**：内网优先、双端同步。origin=ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git；github=git@github.com:robot1lyj/YAM_ABC_Infra.git；main跟踪origin/main，提交后先内网再GitHub，核对同一SHA，单端成功不称同步完成。来源：[环境](../environment.md)。
- **控制**：teleop/inference/hil/collect四产品模式不变；HOLD/POLICY/HUMAN/TAKEOVER/RESUME/FAULT是内部状态。单一Runtime写四臂；回准备位/重力补偿/点动为独占维护状态。跨进程设备锁尚待补齐。来源：[架构](../dagger_architecture.md)。
- **接管与按钮**：HIL键盘i冻结、下一周期相对遥操作，手柄①交还、②无功能；采集①开始/结束录制、②放弃，不停止遥操作；遥操作/推理手柄无功能，空格独立暂停。HIL完整阶段同一episode，来源与干预标记保留。来源：[采集](../collect.md)。
- **运行边界**：--web-port启动未连接；连接真实设备可能施力矩/校准夹爪。软件暂停解除后仍保持、旧策略无效；回位用示教准备位并绑定station哈希，mock/real分开。不例行清零/写零位，不显示零点标定与底层速度调参；运动前核验现场，软件停止不替代硬件急停。来源：AGENTS.md、[运行手册](../hil_quickstart.md)。
- **时序与性能**：非RTC、单在途异步重规划；RK本地epoch/观测时效裁剪，跨机单调时钟不相减。30Hz、action_dt=1/30s等是当前初始配置，需与数据匹配和真机定标。三D405软件时间配对不等于曝光同步；CAN同tick提交不等于同时执行。来源：[同步设计](../synchronization_design.md)。
- **模型合同**：扁平三路RGB HWC uint8、14D state、英文prompt；actions=(50,14) absolute，[左6关节/左夹爪/右6关节/右夹爪]。condapi拥有Pi0.5全量pi05_yam、norm/训练/转换/Thor；W为既有非量化BF16/FP32候选。基础模型离线约104ms不能替代本次checkpoint、网络服务或任务效果；自动身份/单位/action_dt比较及完整来源链待补。来源：[接口及本轮condapi快照](../condapi_interface.md)。
- **任务/界面**：白色悟演智能工作台先建/选任务，相机与四臂独立连接；任务固定到机械臂会话，UUID隔离。中文name/instruction供操作员、英文task供模型与LeRobot；历史数据不改写。三路视觉优先、急停固定，详情按需展开。来源：[采集](../collect.md)。
- **原始数据**：MP4＋HDF5＋JSON清单；默认60秒物理段不拆逻辑集，逐集保存。控制线程有界提交，录制/预览独立进程；预览≤5Hz且可丢帧，不允许静默丢训练记录。独立进程不等于RK无资源竞争。来源：[转换](../convert.md)、[验收](../acceptance.md)。
- **离线数据**：采集不自动转换；工作站/服务器显式运行独立脚本，目标LeRobot v3.0。8767数据集工作台支持YAM原始/LeRobot v3视频导入、检查、审核/集合、按需预览、TAR迁移；转换只接收YAM原始。SQLite/游标/增量扫描/后台spawn已实现；聚合分片、训练版本发布、Lance/分布式仍未实现。用户约100GB为陈述，100小时为目标，未作全量实测。来源：[数据集工作台](../dataset_workbench.md)。
- **验收与记忆**：已有开发机mock/离线读取证据，无真实RK一小时四臂/三D405/Thor闭环通过结论。完整资料按owner保存，按任务检索、无默认累计读取额度；新工具/失败尝试按问题路由检索。指纹/断链检查不判断语义或现场状态；原历史记录失效不靠刷新哈希重新晋级。来源：[验收](../acceptance.md)、[记忆规则](../memory.md)、[问题路由](../cache/context_index.md#问题与行动路由)。

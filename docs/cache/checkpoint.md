# 当前续作：RK3588 IPC + Thor 架构基线与实施交接

## 2026-09-14 本轮完成

用户要求担任规划师，核查架构与记忆、先固定方案，再由其他agent实施；本轮仅本地文档及源码审计，没有编写业务代码、修改运行配置、远端部署、训练或运动。

- 架构v1及P0–P8工作包已写入 [规范所有者](../dagger_architecture.md#2026-09-14-架构基线-v1)：两节点职责、CAN/USB、生产/管理网、NVMe原始落盘、四模式单写入、失败行为、离线训练数据路径。
- [模型契约](../condapi_interface.md)已核对condapi本地HEAD与源文件指纹：全量pi05_yam、14D absolute、时间与单位验收、实际WebSocket服务及W离线引擎的区别、必须补齐的自动合同gate与来源链。
- 修正“等待Gitea密钥登记”“自动转换LeRobot”等残留状态；接臂路径以用户最新确认的USB Hub/USB-CAN为准，板载bcan只保留枚举事实。condapi旧mode/05中LoRA示例已在接口快照明确为旧口径；本轮未改condapi文件。
- 原kernel/checkpoint完整归档为 [旧摘要](../archive/kernel-before-architecture-20260914.md) / [旧检查点](../archive/checkpoint-before-architecture-20260914.md)，不删除历史证据，不刷新旧record哈希。
- 审计范围与来源：[20260914-architecture-audit.json](../evidence/20260914-architecture-audit.json)；本轮检查结果见 [验收](../acceptance.md)。架构候选record仅代表规划，不能检索为真机已通过。

## 下一步执行入口

1. **P0，YAM agent，用户指定第一步**：按用户确认的USB Hub→USB-CAN接法，先识别Hub型号/拓扑、四臂本体编号/适配器序列号、三D405序列号与角色，填写 [身份表](../workstation.md#第一步设备身份登记-p0)。两follower、两leader，各自USB-CAN经USB Hub接IPC；不猜bcan映射，不启动电机。
2. **P1，YAM agent**：复核IPC管理链路、Gitea目标仓库读取、安装uv/Python3.12与锁定依赖；核查NVMe已有内容并配置落盘，mock/实际模块导入通过。无需再等待公钥登记。先不构造电机。
3. **P4，condapi agent**：独立准备本次checkpoint/norm/回放身份与真实普通policy服务，明确实际backend。W离线基准不等于已有可用网络服务；按condapi自己的AGENTS和owner工作。
4. P0/P1后完成P2相机与P3四臂，先得到P5可靠原始示范采集；P6先无电机冻结回放与合同测试，P3/P5/P6门槛通过再进入P7推理/HIL、P8真机一小时。

详细输入、交付和依赖以架构工作包表为准。未派发agent；用户可直接按包分配任务。物理联调共用IPC时安排独占时段，不与安装/转换/压测互相干扰。

## 当前已知与阻断范围

- IPC曾实测Ubuntu22.04.3/6.1.118 PREEMPT_RT/arm64；开发机.250.1↔IPC lan1 .250.2网线SSH及Gitea认证已通过，Wi-Fi自动连接已核对。Thor .250.3为预留；现在的网线不是已验收的RK↔Thor链路。
- IPC Python3.12/uv/YAM环境仍待部署，NVMe未挂载，机械臂各自USB-CAN经用户USB Hub接入、具体适配器身份待识别；四臂本体/适配器映射、夹爪型号、相机序列号与视角未知。
- Thor的系统和基础模型W有condapi历史离线实测；本次微调checkpoint、norm、服务、物理单位/action_dt和闭环效果未验证。它们阻断真机模型动作，不阻断软件准备、相机和采集工作。
- 设备连接、厂商服务占用、网络、磁盘、进程与远端状态在下一动作前复查；本轮未重新SSH，不沿用历史快照宣称现在在线。

## 保留的数据集工作台进度

上轮已实现双格式导入/预览/检查、SQLite WAL/摘要与游标、增量指纹扫描、按需HDF/视频读取、后台spawn、保留ID与审核映射的TAR迁移；独立入口scripts/dataset_workbench.py及锁文件，无SDK/Torch。原始采集与离线转换分离仍有效。
本地100000模拟索引和1小时32×32视频测试不等于真实100GB/100小时D405验收；聚合分片、不可变训练版本发布、Lance、分布式与断点续传未完成。完整行为、版本、历史测试和边界见 [数据集工作台](../dataset_workbench.md)、[验收](../acceptance.md)，此前更早的“不支持LeRobot导入”状态已被双格式实现覆盖。

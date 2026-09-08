# 四模式工作站第一版

本版入口为 `uv run --no-sync yam-workstation`，使用独立的四模式界面与统一执行循环。
旧 `yam-abc-gui` 保留上游采集/转换等功能；不要同时让旧会话和新工作站打开相同CAN设备。
本次完成离线模拟与协议/驱动替身测试，尚未在四臂、D405、Thor/RK3588上验证。

## 环境与无硬件试用

在本项目目录执行，使用现有 `.venv`，不安装模型训练后端：

```bash
cd /home/wuyan-lyj/YAM/yam-abc-reproduce
uv sync --locked --extra camera --extra gui --extra deploy
uv run --no-sync yam-workstation --mock --web-port 8766
```

浏览器打开 http://127.0.0.1:8766 。启动后先保持，点击“开始”执行；不需要真实机械臂或Thor。
界面监听本机；从其他电脑访问RK3588时可以使用SSH端口转发。

自动模拟策略→人工→恢复，并录制一个短episode：

```bash
uv run --no-sync yam-workstation --mock --demo --duration 4
```

终端末尾显示状态，输出默认在 `data/episodes/hil_年月日_时分秒/`；目录已存在会拒绝覆盖。
`--demo`只允许mock。`--baseline`切回不预取的普通分块基准；默认是非RTC异步重规划。

## 操作规则

| 操作 | 终端 | 界面/手柄 |
|---|---|---|
| 选择遥操作/推理/HIL/采集 | 1 / 2 / 3 / 4 | 四模式按钮；选择后先保持 |
| 开始或从保持恢复 | s | 开始/恢复 |
| HIL冻结并介入 | i（单击） | 只能键盘/界面触发；手柄不介入 |
| HIL人工阶段交还模型 | 界面 | 手柄①；其他阶段不生效 |
| 暂停运动 | 空格 | 界面暂停；不占手柄按钮 |
| 采集开关录制 | r | 手柄①（只管理录制，不启动机械臂） |
| 放弃当前采集集 | x | 手柄②；不暂停遥操作 |
| 标记成功/失败 | g / f | 成功/失败按钮，标记当前整个episode |
| 结束并关闭设备 | q | 结束会话 |

遥操作与HIL人工阶段leader为重力补偿。HIL策略阶段镜像follower当前六关节姿态；
扳机只作输入，接管时先保持夹爪，扳机到达/跨过当前夹爪目标才获得控制权。
两臂一起接管。HIL先冻结当前目标，再用相对接管姿态遥操作；普通遥操作初次启动仍检查姿态差，不自动拉动follower到leader旧位置。
恢复时受限对齐leader，并取新观测推理；旧请求和旧块无效。
纯推理模式leader不镜像、不决定follower动作。模式切换不会重建四臂，会关闭旧片段；其他模式继续录制新片段，采集模式等待明确开始录制。

保持是软件停止目标更新，不是硬件急停。硬件故障时尝试全站保持并锁定故障，
等待明确退出，不自动继续；SDK自身的超时/电机保护仍有效。
退出会关闭SDK控制，可能撤掉力矩，必须先支撑机械臂；启动构造可能施力矩并校准夹爪。

## 真机配置与启动

专用文件 `configs/station_hil.yaml` 已使用官方电动leader类型。以下值必须用实物填入：

- 左右平行夹爪电机类型：`linear_4310` 或 `linear_3507`，不凭“标准平行夹爪”猜测。
- 三台D405的实际序列号与top/left/right角色；不是上游示例序列号。
- CAN设备映射、模型任务prompt、condapi模型action_dt（动作样本间隔）。

保留现有编码器零点；不运行GELLO清零或自动机械臂回零。
必要时按已核验值填写每臂 `gripper_limits: [closed, open]`，未填写会由SDK测行程。

完成现场固定、行程清空和人员照看后，先遥操作：

```bash
uv run --no-sync yam-workstation --mode teleop --web-port 8766
```

Thor已经启动condapi兼容的本地模型服务后，使用其现场IP：

```bash
uv run --no-sync yam-workstation --mode hil --url ws://THOR_IP:8000 --web-port 8766
```

`THOR_IP`替换为实际值。纯推理使用 `--mode inference`。不提供URL的真实遥操作会话不能
切入推理/HIL，需带URL重新启动。无自动重连执行；网络错误会锁定故障。
模型接口为扁平RGB HWC uint8三图、14D状态、prompt，输出50×14绝对动作；
关节弧度、夹爪0–1，需与condapi训练/输出变换一致，RK不再次反归一化或加状态。
当前接收并保存服务元数据，不代表已经验证checkpoint/norm或任务效果。

## 实用同步与性能默认值

第一版沿用采集线程，增加8帧历史；状态历史128条；编码与网络各自独立线程。
这是经过模拟验证的轻量实现，不称为共享内存多进程或硬实时框架。

| 配置 | 默认 | 行为 |
|---|---|---|
| 上层control_hz | 30Hz | 四臂同tick；SDK继续负责底层循环 |
| warn_frame_skew | 40ms | 三图接收时间差超过只记警告 |
| max_frame_skew | 120ms | 超过不生成本次新观测 |
| max_frame_age | 500ms | 短暂缺图继续有效块；持续过期转保持 |
| max_state_age | 250ms | SDK状态循环停止更新则故障；不是每个CAN电机的独立接收时间 |
| request/action_timeout | 1.5s / 1.5s | 拒绝明显过期请求/动作 |
| tick_timeout | 500ms | 严重控制停顿锁故障，普通miss只统计；不追赶补发旧周期 |
| replan_period | 200ms | 最多一个在途请求，执行旧块时请求新块 |
| handover/mirror_error | 0.2 / 0.5rad | 交接门限/运行中的leader大偏差保持 |

这些是可调的开发默认值，不是现场性能证明或最终控制参数。
D405无三机外部硬同步；本版按**主机接收时间**配对与关节历史插值，
保留设备时间/时间域/帧号，但尚未标定曝光时钟偏移。观测插值仅用于模型/记录；
控制约束使用最新真实状态。以后实测需要再加时钟校准、多进程或硬件优化。

推理用obs时间与action_dt定位当前动作，裁掉过期前缀，保留全部50步未来块；
不会因为控制Hz变化而把模型时间轴加速。新块替换尚未执行的计划，约束后的命令另存；
首版不做RTC、时间集成或块间加权平滑，避免先改变模型动作语义。

## 记录与训练导出

每次运行的会话目录中，`episode_000001/` 等子目录各保存一段。会话结束写入 `session.json` 索引；未开始采集不创建 episode。原始episode包含 `steps.jsonl`、三路分片MP4、结束时生成的 `manifest.json`。
JSON逐步保存人工/策略/实际提交动作、最新状态、对齐状态、epoch/request/obs、原始策略块、
夹爪拾取状态、限幅掩码、四臂提交时间、图像帧索引与同步指标。没有策略预测时为null加无效掩码。
视频按控制帧索引对应；重复选中的相机帧可以重复写入，缺图不伪造新帧。
会话队列最多32个事件/帧，单段编码队列最多8个tick，满或写盘失败明确aborted；不无限积累整个episode。
断电恢复只能保证部分已写内容可能可读，不承诺未完成MP4/manifest完整。

导出连续人工有效段到现有canonical格式，可继续用原转换链处理：

```bash
uv run --no-sync python -m yam_abc_reproduce.hil.export data/episodes/实际会话/episode_000001 --output data/expert/新目录
```

策略/保持段不作为专家标签；遇到非人工段或epoch变化就拆成新episode，不把过滤后的
间断动作拼成连续轨迹。故障episode默认拒绝导出，需要先审核数据。
保留 `hil_provenance.jsonl`；原LeRobot转换器未自动映射这些额外HIL字段，
只通过canonical接口转换专家状态/动作/图像；模型微调仍归condapi。

## 现场下一步

先用模拟界面确认流程，再填真实设备映射，逐对验证方向/夹爪和leader增益，
最后运行四臂三相机与Thor联调。先观察控制miss、帧龄、接收偏差、跟踪误差和录制队列，
根据实际任务调整阈值；不以三台D405无法达到的同时曝光为上线前提。

## 数据采集与双按钮

数据采集使用 `--mode collect`，不需要 Thor URL。完整操作、按钮优先级、结果标记和目录布局见 [采集手册](collect.md)。其他模式连续记录；模式切换结束上一段，进入采集后等待明确录制操作。

## 默认LeRobot输出

会话结束、设备关闭后自动生成 `会话/lerobot/`，见 [字段和格式](convert.md)。运行时只异步录制原始数据；`--raw-only`用于暂缓格式整理和性能测量。遥操作/推理模式两个手柄按钮均无功能。

## uv启动与离线检查

首次安装或依赖变化后，先 `uv sync --locked --extra camera --extra gui --extra deploy`。日常命令的 `--no-sync` 使用现有环境，不重新安装；无需激活 `.venv`。

```bash
uv run --no-sync yam-workstation --mock --mode collect --check
```

检查不连接设备、不创建录制目录。真机检查去掉 `--mock`，HIL/推理提供 `--url`；配置占位会拒绝通过。模块可发现不等于驱动可加载或设备可用。详见 [环境说明](environment.md)。

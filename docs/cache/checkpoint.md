# 续作：RTC与DAgger接管验收

## 最近结果

- 用户确定使用RTC，先验证同步、介入/交还和专家落盘。本站默认RTC、d=9、30Hz直接SDK控制。
- 2026-09-18最新`session_20260918_093240_c22760/episode_000004`：9504帧约317秒、全部RTC、tick无缺号；900回复2个迟到丢弃，RTT中位233/P95 242ms；9430有效观测，无人工段。细节见[验收](../acceptance.md#2026-09-18-rtc与dagger准备验证)。纯推理集不能证明DAgger真机通过。
- 本地修复manifest沿用启动TDA配置：开始录制取实际模式与d，异步写盘冻结每集配置。补测RTC接管/交还、旧回复拒绝、单集连续记录及元数据快照；旧集未改写。本轮未重启或运动，代码尚未在设备进程生效。
- DAgger沿用MP4+HDF5；训练action取实际Follower提交目标，human_action是原始Leader输入。专家导出筛有效人工段，不跨策略间隔拼接；见[格式合同](../convert.md)。
- 只读现场状态为connected/HOLD、RTC d=9、未录制、无错误，操作前再核验。IPC账号`linux@192.168.110.140`，仓库`/data/YAM`。Web重启不释放力矩；设备重启会释放。NVMe/USB-CAN/SDK反馈过期未定根因。

## 下一步

1. 部署修复前确认四臂支撑；现场短测HIL启动RTC→介入→相对Leader动作→夹爪拾取→交还→正常保存，核验专家导出和完整轨迹。不将离线模拟当真机验收。
2. 若有`SDK state update stale`或CAN/NVMe异常，按[硬件事实](../workstation.md)与[验收记录](../acceptance.md)独立诊断，不放宽保护来掩盖故障。

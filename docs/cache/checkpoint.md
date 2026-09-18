# 续作：RTC与DAgger接管验收

## 最近结果

- 用户确定使用RTC，先验证同步、介入/交还和专家落盘。本站默认RTC、d=9、30Hz直接SDK控制。
- 2026-09-18最新`session_20260918_093240_c22760/episode_000004`：9504帧约317秒、全部RTC、tick无缺号；900回复2个迟到丢弃，RTT中位233/P95 242ms；9430有效观测，无人工段。细节见[验收](../acceptance.md#2026-09-18-rtc与dagger准备验证)。纯推理集不能证明DAgger真机通过。
- manifest快照修复已提交17b7a7c，尚未在设备进程生效，旧集未改写。新增Evo-RL实际字段映射与本地固定回放源，见convert.md、hil_quickstart.md。
- DAgger沿用MP4+HDF5；训练action取实际Follower提交目标，human_action是原始Leader输入。专家导出筛有效人工段，不跨策略间隔拼接；见[格式合同](../convert.md)。
- 获准HIL短测episode_000005仅38帧后镜像误差0.509rad自动HOLD，Leader几乎未动，四臂同步未通过。页面急停静止锁存及离线双臂保持通过，动态制动未验收。用户改要求不用Thor、固定回放；Follower已页面回零，最大残差0.051rad，Leader未回零，四臂仍连接HOLD。详见acceptance.md首节。
- IPC账号`linux@192.168.110.140`，仓库`/data/YAM`。Web重启不释放力矩；设备重启/断开会释放。NVMe/USB-CAN/SDK反馈过期未定根因。

## 下一步

最新部署6393067：Leader回零Kp40%（跟随仍20%，Kd/保护不变）；回放游标按实际提交确认、暂停保留、回零重置。推理面板可HOLD下热切换地址，实测设备PID不变、通信子进程替换，当前本地8002长回放sync_hold/HOLD。未启动新运动，回零效果与真机暂停恢复待用户验收；源切换不需再断臂。详见acceptance.md末节。

最新现场：c115794获准部署重启，先相机后四臂连接，HOLD无错误。Leader独立回零按钮已上线；loopback:8002改为乐高分拣3最长RTC集093240_c22760/000004 --all，9504帧约5分17秒，完整无电机wire核验一致。sync_hold，不调用Thor。未执行新回零或长回放，由用户点击。Leader短回放用户反馈会动，精度仍待验收。10:52:56历史SDK/USB同秒故障根因未定。

1. 固定回放会话103824_c92b5b/000001复现不跟随：Follower动0.544rad、Leader仅0.0023rad。当前急停锁存/HOLD、loopback:8002、sync_hold；回放服务由SSH进程运行，退出后按hil_quickstart重启。用户授权共用策略关节目标，已本地修改StationIO，PD与主从保护不变，待受控重启支撑确认及真机验收；运动前明确通知，未获确认不释放力矩。RESUME路径未改。详见acceptance.md末节。
2. 若有`SDK state update stale`或CAN/NVMe异常，按[硬件事实](../workstation.md)与[验收记录](../acceptance.md)独立诊断，不放宽保护来掩盖故障。

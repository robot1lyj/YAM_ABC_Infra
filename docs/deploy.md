# 部署与更新边界

部署前读 [当前验收状态](acceptance.md)、[环境与unit配置](environment.md) 和 [工作站](workstation.md)。IPC SSH 用户为 `linux`，2026-09-24 最近实测 Wi-Fi 地址为 `10.18.10.39`；地址由 DHCP 分配，连接前重新核对。不是开发机用户名；密码不入库，挂载与进程也必须重新检查。

## 改什么，重启什么

| 修改 | 正常更新范围 | 是否可能掉使能 |
|---|---|---|
| 静态页面 / Web API | 更新资源、刷新浏览器或独立重启yam-workstation | 不应关闭SDK；重新附着可能请求HOLD |
| Thor通信 / 动作块规划 | HOLD无活动录制时独立重载对应子进程 | 不关闭SDK，不自动开始 |
| 每集录制筛选规则 | 按录制侧模块加载边界，在新集生效 | 不需要为筛选规则重启机械臂 |
| 录制owner故障 | 新版后台恢复新数据session，保留旧原件 | 不重连SDK；新版需先完成部署 |
| Runtime仲裁 / SDK代码 | 当前默认需受控更新yam-device | 可能释放力矩，需重新取得现场许可 |
| 首次迁移独立yam-executor | 一次受控移交SDK/CAN所有权 | 迁移时需支撑；之后上层detach保持而非release |

**可选executor尚未迁移IPC**。不能因源码已有解耦接口就承诺当前设备重启不掉力矩。系统盘程序、NVMe数据分离模板亦待现场迁移；旧程序仍可能依赖/data。模板不是自动迁移脚本。

## 启动与检查

无硬件检查：

```bash
uv run --no-sync yam-workstation --mock --mode collect --check
uv run --no-sync yam-workstation --mock --mode collect --web-port 8766
```

真实设备使用服务部署和 [运行手册](hil_quickstart.md#真机配置与启动) 的配置，不能把mock命令简单改完就无人值守启动。CLI无界面会构造设备；Web入口先启动页面，连接按钮才构造。构造可能施加力矩。

Thor不由本仓库启动；服务使用 [接口合同](condapi_interface.md)，模式须匹配训练式RTC或普通协议。冻结观测探针用 `scripts/probe_thor_policy.py --help`，先无电机验证再获准运动。

部署顺序：确认现场与保存完成 → 核对挂载/版本/所有权 → 合并需要的核心更新，减少重复重启 → 离线/只读验证 → HOLD验证 → 用户明确开始。出现错误不连续重试连接或软件USB重新枚举，先保留日志和检查硬件。

旧yam-abc-deploy/GUI是另一生命周期，不作为HIL工作台入口，见 [旧工具](legacy_tools.md)。历史部署说明见 [快照](archive/deploy_20260922.md)。

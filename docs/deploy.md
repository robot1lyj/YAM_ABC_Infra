# 本地边缘推理与 HIL 部署

当前部署是现场 Thor + RK3588 网线连接。Thor 的模型服务由 condapi 管理，本项目负责设备、观测、动作执行和记录。

## 运行前

- 先完成 [硬件核验](workstation.md)和 [环境安装](environment.md)。
- 使用 [station_hil.yaml](../configs/station_hil.yaml)，不要使用上游 GELLO station。
- 按 [condapi 接口](condapi_interface.md)确认图像、动作单位、归一化、模型版本和 `action_dt`。
- 使用 `scripts/probe_thor_policy.py --help` 查看冻结观测探针参数，运行入口见 [中文手册](hil_quickstart.md)；本仓库不替你启动或验证真实模型服务器。

## 当前命令

```bash
.venv/bin/python -m yam_abc_reproduce.hil.run --mode inference --url ws://THOR_IP:8000 --web-port 8766
```

`THOR_IP`换为现场地址；`--mode hil`启用 Leader 接管。启动后先保持，点击开始才执行选定策略；但设备构造可能已上电/校准夹爪。

默认非 RTC 异步重规划；`--baseline`为普通分块比较路径。网络故障锁定状态，不自动重连后执行。主机接收时间配对不等于曝光同步。

旧 `yam-abc-deploy`/旧 Deploy 页面是另一生命周期，可能只构造 Follower 或释放 Leader，不能作为本项目的 HIL 入口。详细历史信息见 [英文归档](archive/deploy-1c04c83.md)。其中 LoRA 和单 GPU 默认仅属于旧工具，不覆盖 condapi 当前规范。

# 续作检查点（2026-09-24）

## 当前可复核状态

- IPC SSH 用户名为 `linux`，代码目录为 `/data/YAM`。Wi-Fi 地址由 DHCP 分配；最近实测 `wlan0=10.18.10.39/23`，面板 `http://10.18.10.39:8766/` 返回 HTTP 200。使用前重新核对地址与主机身份，不沿用历史 IP。入口和启动方式归[环境](../environment.md#日常操作)。
- 2026-09-24 只读查询：IPC 仓库 HEAD `76b6834`；`yam-device`、`yam-workstation` 均 active。设备和相机均处于 disconnected、无报错。Web 现场安装了按 `wlan0` 当前地址启动的脚本；本地仓库另有未部署到 IPC 的提交和改动，不能把本地 HEAD 当成现场代码版本。
- 生产网线仍为 IPC `lan1=192.168.250.2/24`、Thor `192.168.250.1/24`；这与 DHCP Wi-Fi 地址是两条不同链路。模型、相机、机械臂的实时状态须重新查询，不从本检查点继承。
- 当前四模式、RTC/HIL/录制合同归[架构](../dagger_architecture.md)、[运行手册](../hil_quickstart.md)、[数据字段](../hil_dataset_fields.md)；9月22日的 gs_usb 修复和离线恢复加固留在[验收证据](../evidence/20260922-architecture-recovery.json)与[现场报告](../evidence/20260922-ipc-usb-nvme.md)，不作为今天运行状态。

## 下一步与边界

- 部署任何本地代码前先对照 IPC 实际 HEAD 与未提交文件，按[部署边界](../deploy.md)确认会更新哪个进程；设备进程重启可能释放四臂力矩，不能沿用旧许可。
- 网络重启后先验证 SSH 主机身份、`wlan0` 地址、面板监听地址与服务状态；独立 Web 重启不等于设备会话恢复，也不会自动开始运动。

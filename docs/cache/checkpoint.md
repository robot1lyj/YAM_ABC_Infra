# 续作检查点（2026-10-09）

- 双臂键盘HIL源码33ec2a8已增量部署：先暂停模型，再微调XYZ/夹爪，Leader保持；默认Leader模式保留。模拟两集读回及IPC离线130项通过，真机未运动，下一步由用户测试，见[手册](../hil_quickstart.md)。
- IPC Git基线76b6834＋部署快照，保留站点/SDK补丁；设备/相机断开，旧SDK故障清空不等于硬件复验成功。验收与备份归[验收](../acceptance.md)。用户linux，DHCP地址查[环境](../environment.md)，新重启须现场许可。
- 既有PARTS shadow启动配置未改，不施加RL残差；不能以本轮键盘部署宣称RL规则/训练资格已验收。后续隔离开发规则已写入AGENTS，流程归[部署](../deploy.md)。

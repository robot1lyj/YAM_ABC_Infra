# 续作检查点（2026-09-30）

- PARTS自动规则已实现：实际张开确认、持物锁定、抓取区排除、等待回撤和承诺残差耗尽；不需人工标记/VLM。635项离线测试通过，默认off，未部署IPC。唯一owner：[RL客户端](../parts_client.md)，验收归[验收](../acceptance.md)。
- 待现场一次性标定抓取区、张开阈值/确认时间，Thor确认新行为合同及自动规则metadata后，先获准shadow再collect；mock包不代表真实训练就绪。
- IPC本轮未访问，用户`linux`；DHCP地址、主机身份与实际服务版本使用前重查，见[环境](../environment.md)。设备重启可能释放力矩，历史许可不可沿用，见[部署](../deploy.md)。

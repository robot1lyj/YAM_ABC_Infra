# IPC USB-CAN / NVMe 核查与 gs_usb 修复（2026-09-22）

对象：`linux@192.168.110.140`，RK3588、定制 `6.1.118 #64 PREEMPT_RT`。
时间均为 Asia/Shanghai。用户报告多次激活后先提示左臂电机失联，插拔/重启 USB 后
NVMe 不见，最近一次记为 9 月 21 日傍晚约 17:30；随后明确授权修复自装 `gs_usb`。

结论：**现场旧 gs_usb 确认缺少三类请求生命周期修复，已回移五项上游提交并加载
`6.1.118-yam1`。USB-CAN 无运动短测通过；NVMe 启动枚举故障仍未确诊或修复。**
缺陷存在不等于已经证明昨晚全部现象的唯一因果链。

## 日志对齐

| 2026-09-21 时间 | 观察 |
| --- | --- |
| 17:29:40–43、17:39:32–35 | 设备服务先后停止/启动 |
| 17:40:02–09 | 先左 Follower 电机 3，随后左 Leader、右 Follower、右 Leader 通信失败；不是仅一台左臂 |
| 17:40:29、31、39 | CAN 清理/停启附近，Renesas `0004:41:00.0` 三批 xHCI `Unknown event condition 0 ... HC probably busted`，各 90 条；涉及各 slot/ep 的 30 条分组 |
| 17:40:57–58 | USB Hub `5-2` 及下游断开，`clear tt ... error -71`；随后重新枚举 |
| 17:41:31 起的新启动 | NVMe 所在 `fe150000.pcie` 两次 `Link Fail, LTSSM 0x3`，初始化失败；没有 NVMe，`/data` 缺失，位于盘上的 YAM 执行文件启动失败 |
| 18:20:55 起的新启动 | NVMe 恢复，18:21 挂载及服务恢复 |

9 月 16 日也有多次启动枚举失败与成功交替。没有找到足以确认 17:41 是人工重启、
掉电、watchdog 或 panic 的证据；pstore 为空。尚未取得“热运行期间 NVMe 被拔除”的
日志，已确认的失败点在**下一次启动 PCIe 链路训练**。用户是否彻底断电后才恢复仍未知。
跨文件重复的 journal 采样没有重复计数。应用按左侧优先构造，也会让左臂最先暴露共同故障。

## 硬件与内核

- USB-CAN：`fe190000.pcie` → `0004:41:00.0` Renesas uPD720201 `1912:0014 rev03`
  → Realtek RTS5411 `0bda:5411` Hub `5-2` → 四个 CANable 2.5 Candlelight。
  同一 Renesas 的 USB3 根总线上另有一台 D405（`6-4`）。四臂固定口映射保持不变。
- NVMe：`fe150000.pcie` → `0000:01:00.0`，TIMAR S97M8-PY 1TB，FW `SSPV632N`。
  与 USB 主控制器属于不同 PCI domain、PHY 和设备树复位路径；不能据此排除板级共用电源。
- `CONFIG_CAN_GS_USB` 未启用，实际模块来自手动编译的官方 `v6.1.118` 单文件。
  xHCI/PCIe 驱动是内建；替换 gs_usb 不会更新它们。
- `CONFIG_PCIEAER` 未启用，缺少 AER 日志不能用来排除 PCIe 错误。
  USB autosuspend 参数为 -1；采样时 NVMe、Renesas 均 active/control=on，ASPM performance。
  NVMe 当时为 PCIe 8 GT/s ×4。没有据此改动 APST/ASPM 或将省电认定为根因。

## 已修复的源码缺陷

1. RX 完成回调重新提交时未重新 anchor，后续关闭可能漏收请求。
2. CAN open 失败路径错误地清理 TX，而实际已分配的是 RX 请求。
3. USB 发送完成报错时只打印日志，未释放 echo/context 和发送槽位，累积错误可耗尽发送资源。

具体五项提交、适配范围、指纹和操作步骤见
[驱动包说明](../../scripts/gs_usb/README.md)。RX re-anchor 与提交失败 unanchor 成组回移，
包含后续日志修复，避免引入上游曾发生的关闭死循环回归。
原源码 SHA256 与官方 `v6.1.118` 一致，故这不是仅根据内核版本号推测缺陷。
上游发送失败缺陷原始说明：[已合入提交](https://github.com/torvalds/linux/commit/516a0cd1c03fa266bb67dd87940a209fd4e53ce7)。

## 现场实施与验收

14:46:18 卸载旧 gs_usb，14:46:25 加载新模块，四口原名称及路径恢复。
仅替换 `/lib/modules/6.1.118/extra/gs_usb.ko` 并 depmod；开机模块加载配置保留。
补丁后模块版本 `6.1.118-yam1`、srcversion `521DE9F01422C84B930CC90`。
系统盘发布目录 `/home/linux/gs_usb-repair-20260922`，root 备份位于
`/var/lib/yam-gs-usb/20260922/gs_usb.ko.original`。本地另存构建/回滚压缩包。

| 检查 | 结果与范围 |
| --- | --- |
| 原生构建 | IPC GCC 11.4、现场 headers、W=1、MODPOST 通过；仅有 headers/GCC 版本差异提示；模块成功加载 |
| 14:47:44–49 四口仅接收停启 | 每口 20 次，共 80 次；RX/TX 均为 0，所以这项只覆盖打开/关闭，不冒充收包回调验证 |
| 14:50:17–40 两侧手柄读取后重开 | 每侧 20 次重开，每次 100 个 `0x50E#FF02`；左/右均 2,000/2,000 回包，RX/TX error/drop 均为 0 |
| 手柄往返时间 | 左均值 0.357 ms、最大 0.431 ms；右均值 0.358 ms、最大 0.456 ms；低负载短测，不是运动实时性指标 |
| 收尾 | 四口 DOWN、listen-only 已关闭、无 CAN 订阅、模块引用为 0；NVMe live、`/data` 正常挂载 |
| 内核日志 | 从切换至 14:54 采样，没有新的 xHCI/USB/PCIe/NVMe 错误；看到预期驱动重枚举及 CAN link-ready 日志 |

首版维护脚本最后的 journalctl 读取因旧 systemd 不接受带时区 ISO 时间而退出；模块
已经成功加载、空闲检查通过。随后修正时间格式并单独读取日志确认，没有再次卸载/加载。
这个脚本尾部错误不作为驱动失败或一次额外安装计数。

14:51:19 恢复 device/Web 服务，保持机械臂 disconnected，无 SDK 构造、使能或运动。
维护前相机 connected；恢复相机的正常 API 返回 HTTP 409（已有浏览器会话持有控制权），
未抢占页面，相机留在 disconnected，需当前操作页面重新连接相机。
未执行整机重启、USB/PCIe reset、拔盘、发送错误注入、长时运动或三相机并发验收。
原模块已备份且回滚路径已准备；未为验证而实际回退。

结构化结果：[20260922-gs-usb-repair.json](20260922-gs-usb-repair.json)。
完整原始材料在开发机 `/home/wuyan-lyj/YAM/ipc-diagnostics-20260922/`：
`observations.json`、`kernel-audit.json`、`repair-observations.json`、分窗 journal、
官方补丁及源码、构建/回滚包。摘要保存原始修复采样的 SHA256。

## 未解决项与下一步

- **Renesas xHCI**：2026-06 上游针对相同 uPD720201 型号/revision 的补丁加入
  `XHCI_NO_SOFT_RETRY`，处理错误恢复后的 Stop Endpoint 卡死。
  现场 quirk 值没有公开 Rockchip 6.1 定义的该位，但现场主症状是 completion code 0，
  没有完整同签名。需取得准确 BSP 源码后评估，不能把型号匹配写成已确诊。
  [原始补丁](https://lists.openwall.net/linux-kernel/2026/06/17/723)、
  [维护者接收回复](https://lists.openwall.net/linux-kernel/2026/06/18/1145)。
- **NVMe 暖重启**：Rockchip issue #382 报告暖重启后的 PCIe 状态清理问题，
  但其平台/内核/症状与现场不完全相同。需要板厂核对 PERST#、3.3V、参考时钟及
  shutdown/probe 时序，安排受控软重启/彻底断电对照；不直接套用帖子中的补丁。
  [Rockchip 原始报告](https://github.com/rockchip-linux/kernel/issues/382)。
- 在现场确认支撑及有人照看后，补做实际四臂多轮连接/断开和相机并发长测；
  继续收集 xHCI/CAN 计数与启动日志。当前通过的是新驱动的无运动短测，
  不是确认原故障在完整负载下消失，更不是 NVMe 已治愈。

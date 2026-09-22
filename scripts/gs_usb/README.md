# RK3588 gs_usb 6.1.118 修复包

此包针对现场 RK3588 IPC 的独立 `gs_usb.ko`，版本 `6.1.118-yam1`。
2026-09-22 已构建、安装并加载；核查结果见
[现场证据](../../docs/evidence/20260922-gs-usb-repair.json)和
[诊断报告](../../docs/evidence/20260922-ipc-usb-nvme.md)。
它不包含 xHCI、PCIe、NVMe 或机器人应用升级。

## 补丁来源与范围

基线为 Linux stable `v6.1.118` 的
[gs_usb.c](https://github.com/gregkh/linux/blob/v6.1.118/drivers/net/can/usb/gs_usb.c)，
与 IPC `/home/linux/gs_usb-build/gs_usb.c` 的 SHA256 完全一致：
`4617d004d03b8cedc03d1bc4d1c7a03b3e890404e40fd69922b0cf9aba076eb5`。
原始代码许可证为 GPL v2，以下上游修复作者均为 Marc Kleine-Budde。

| 上游提交 | 回移内容 |
| --- | --- |
| [3e54d3b4a843](https://github.com/torvalds/linux/commit/3e54d3b4a8437b6783d4145c86962a2aa51022f3) | CAN 打开失败时清理 RX anchor，而不是 TX anchor |
| [7352e1d5932a](https://github.com/torvalds/linux/commit/7352e1d5932a0e777e39fa4b619801191f57e603) | RX 回调重新提交前重新 anchor，避免关闭时漏收请求 |
| [79a6d1bfe114](https://github.com/torvalds/linux/commit/79a6d1bfe1148bc921b8d7f3371a7fbce44e30f7) | 提交失败必须 unanchor，避免关闭无限循环 |
| [494fc029f662](https://github.com/torvalds/linux/commit/494fc029f662c331e06b7c2031deff3c64200eed) | 初始化日志指针并打印正确的提交错误码 |
| [516a0cd1c03f](https://github.com/torvalds/linux/commit/516a0cd1c03fa266bb67dd87940a209fd4e53ce7) | USB 发送完成回调报错时释放 echo/context、更新计数、唤醒队列 |

`urb-lifecycle.patch` 将上述关联逻辑适配到旧版的 `usbcan` 变量名及原有分支结构，
并增加 `MODULE_VERSION("6.1.118-yam1")`，便于区分磁盘文件与已加载版本。
TX 完成回调与官方 `v6.1.162` 对应函数逐字一致。
未全量替换为新版驱动：端点发现、新设备支持、短帧校验等其他变动不在此次范围。
未引入仅 re-anchor 而缺少后续 unanchor 的中间版本。

## 构建与安装

在运行 `6.1.118/aarch64` 的 IPC 上，以普通用户构建，不覆盖原构建目录：

```bash
bash tooling/build.sh /home/linux/gs_usb-build/gs_usb.c /home/linux/gs_usb-repair-20260922/build
```

构建脚本要求输出目录尚不存在，核对基线及补丁后源码指纹，再通过现场 headers
执行 `make -j2 W=1 modules`、MODPOST、架构/版本检查。重新构建应使用新目录。
现场 GCC 为 11.4，headers 记录 GCC 9.4，运行内核记录 GCC 10.3.1；构建有工具链
差异提示，无其他编译警告。成功加载及短测支持本次兼容性，但不等于完整 BSP 验收。

本次发布固定位置 `/home/linux/gs_usb-repair-20260922/`，位于 eMMC：

- `build/`：补丁后源码、模块、日志与 SHA256。
- `original/`：原模块及源码副本。
- `tooling/`：本目录脚本及补丁。

`switch.sh` 锁定了本次实际二进制 SHA256，不自动信任其他构建产物：
`db11fa3f71f67b43abe141233cc64e92ba904fb923d2bbe60905e3f67e58b4de`。
重新构建产生不同二进制时必须核查后另发版本，不能直接绕过指纹检查。

```bash
bash /home/linux/gs_usb-repair-20260922/tooling/switch.sh check
sudo bash /home/linux/gs_usb-repair-20260922/tooling/switch.sh apply
```

`check` 全程只读。`apply` 要求所有 CAN DOWN、无 CAN 订阅、模块引用为 0、
四个固定 USB 下联口映射正确、独立 executor 未启用、NVMe 在线；不能用于活动机械臂。
它先停止 Web 以封住新的连接入口，重查空闲后停止 device，备份到 root 所有的
`/var/lib/yam-gs-usb/20260922/gs_usb.ko.original`，仅重载 `gs_usb`。
原文件和新文件均核对，替换通过同目录 rename 完成，然后执行 depmod。
不强制卸载、不重置 USB 主控制器、不重启系统、不拉起 CAN。

## 无运动验证与恢复页面

服务保持停止，分别运行：

```bash
sudo python3 /home/linux/gs_usb-repair-20260922/tooling/check_receive_only.py
sudo python3 /home/linux/gs_usb-repair-20260922/tooling/check_encoder_reopen.py
```

第一项四口各 20 次仅接收停启，最终关闭 listen-only 并保持 DOWN。
第二项只对两侧 Leader 手柄发送官方 `0x50E#FF02` 状态读取，接收 `0x50F`，
每口 20 次重开、每次 100 请求；不构造 SDK、不写电机使能或目标。
依据固定 i2rt `motor_drivers/dm_driver.py:PassiveEncoderReader.read_encoder` 及
`utils/encoder_manager.py:REQ_REPORT`，不调用会检查/修改配置的 encoder 构造函数。
两项都核查服务停止及空闲条件，异常时尝试将涉及接口置 DOWN；失败后应保持服务停止排查。

检查日志无新增 USB/xHCI/PCIe/NVMe 错误、四口均 DOWN、NVMe 正常后，作为 linux 用户：

```bash
systemctl --user start yam-device.service yam-workstation.service
```

服务启动保持机械臂未连接。相机由持有控制权的当前页面连接；维护脚本不接管活跃页面。
真实四臂反复连接、三相机并发、长时运动、发送错误/打开失败注入及重启掉盘验收尚未执行。

## 回滚

保持或按现场安全流程断开四臂后：

```bash
sudo bash /home/linux/gs_usb-repair-20260922/tooling/switch.sh rollback
```

同样经过空闲检查，再换回原模块；原模块 SHA256：
`513caff387c8cb1c73df189fc0ee1d9ab8574dff74ff153b91110fa0967a8f66`。
回滚后先核查四口/NVMe，再按上面的命令启动服务。回滚脚本已准备，未为测试而实际回退。
若目标模块无法加载，脚本恢复原磁盘模块并尝试加载，保持服务停止以供检查。
其他异常不会尝试全局 USB reset 或反复自动重试。
该模块仅用于此 `6.1.118` 内核；更换内核应使用准确对应的 BSP 源码和 headers 重建。

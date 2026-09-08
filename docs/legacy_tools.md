# 保留的上游工具

项目已经以官方 YAM Leader 四模式工作站为主。保留旧工具用于兼容和追溯，不让它们成为默认入口。

| 工具 | 当前定位 | 注意事项 |
|---|---|---|
| `yam-abc-gui` | 上游采集、回放、训练管理界面 | 生命周期独立，不能和新工作站同时打开同一CAN |
| `yam-abc-teleop` | 上游遥操作入口 | 不含当前统一HIL状态机 |
| `yam-abc-deploy` | 上游模型部署入口 | 可能只打开Follower，不可直接替代本项目HIL |
| `yam-abc-convert` | canonical转训练格式 | 新HIL原始数据先经过专家段导出 |
| `yam-abc-cameras` | 相机枚举与序列号辅助 | 现场每次重新核对角色与设备 |
| `yam-abc-doctor` | 环境诊断 | 导入成功不等于真机验收 |

当前用户手册已是中文：[采集](collect.md)、[部署](deploy.md)、[训练交接](training.md)、[硬件](hardware.md)。
原始上游英文内容保留在 `docs/archive/`，带版本和适用范围标识；不要求翻译第三方源码和全部历史资料。

[采集归档](archive/collect-1c04c83.md) · [部署归档](archive/deploy-1c04c83.md) · [训练归档](archive/training-1c04c83.md) · [硬件归档](archive/hardware-1c04c83.md)

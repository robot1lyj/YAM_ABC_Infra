# 核心摘要

仅在需要项目概览、且已加载的 `AGENTS.md` 与[一级路由](context_index.md)不足时读取。这里不放运行快照或阶段日志。

- **系统职责**：RK3588 IPC 控四臂/三D405并录原始数据，Thor 负责模型推理；训练/转换归condapi。设备身份和当前网络查[环境](../environment.md)与[硬件](../workstation.md)，不能从旧IP推断在线。
- **控制边界**：四模式共用单一写入者，人工路径为绝对1:1；维护和运行互斥，故障恢复不自动运动。重启/连接可能影响力矩；操作查[手册](../hil_quickstart.md)，部署查[边界](../deploy.md)。
- **数据合同**：原始MP4/HDF5/JSON，LeRobot仅显式离线转换；14D目标、三RGB和prompt的模型合同查[接口](../condapi_interface.md)，HIL字段查[数据字段](../hil_dataset_fields.md)。
- **检索纪律**：先按[一级路由](context_index.md)选一个owner，故障才查[问题路由](problem_index.md)；进度只在续作时读[检查点](checkpoint.md)。历史证据不作当前许可，写回规则归[记忆owner](../memory.md)。

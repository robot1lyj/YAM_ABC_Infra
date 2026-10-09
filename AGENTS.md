# YAM 项目工作约定

## 范围与产品

- 主仓库 `yam-abc-reproduce` 默认main，i2rt用固定子模块，不恢复同级旧目录。IPC负责设备、控制与原始记录；Thor模型/训练/转换归condapi，不改那边项目。
- 交付设备与采集、数据集两平台，支持局域网跨平台浏览器；复用现有机制，按完整流程验收。设计合同查 `docs/dagger_architecture.md`；模拟通过不是真机通过。

## 开发与交付

- 开发使用独立Git worktree/分支、模拟端口、配置及测试数据目录，不边改正式工作目录边运行正式界面，不争用SDK/CAN。测试/验收后才合并main并按 `docs/deploy.md` 部署，不自行重启用户使用中的服务。
- 完成后运行适用检查及 `git diff --check`，只提交本任务文件；先推origin main，再推github main并核对同一SHA。失败保留本地提交并报告，不强推。远端/依赖/CLI查 `docs/environment.md`；Python3.12/uv，依赖变更提交pyproject/uv.lock，不提交虚拟环境、模型、运行数据、凭据或记忆账本。

## 按需记忆

- 规则归 `docs/memory.md`。已知owner直接读；缺定位才查 `docs/cache/context_index.md`，故障查problem_index，概览才读kernel，续作才读checkpoint。宿主已注入内容不重复读，不固定串读所有层。
- 先更新owner，再替换必要摘要；不追加流水账，不为普通进度新建记忆文件。历史证据/失败按需检索，保留来源、单位、未知项与重试条件；不更新旧哈希冒充复验，历史许可不授权新操作。
- 默认轻量来源核查，精确产物身份或合同要求才用strict。地址/设备/进程在影响下一步时复查；文件字节不是上下文token，写摘要不会清除宿主历史。预算和链接用 `scripts/check_project_memory.py` 检查。

## 设备与安全

- 本站2 Follower＋2官方电动Leader＋3 D405，不是GELLO。既有身份无需每轮重登记，变更设备/接法再查 `docs/workstation.md`；不例行写电机零位、刷固件或关闭超时。
- 构造/连接可能施力矩或校准夹爪；运动、释放力矩前确认四臂支撑、路径清空、有人照看。软件暂停不是实体急停，故障恢复不自动运动。重启影响先查部署边界，未知硬件根因不写成已修。
- 单一Runtime/SDK写入者，维护与运行互斥。遥操作/HIL人工复用绝对1:1，不恢复相对映射或对齐门槛；可选键盘HIL先暂停模型再微调，Leader保持。模式/按键查 `docs/hil_quickstart.md`，模型合同查 `docs/condapi_interface.md`。
- Web启动不构造硬件；本站factory_zero_home为真，Follower与Leader回零是独立操作，核验完整路径、保持夹爪，不写编码器零点；其他站按示教准备位配置。
- 相机与四臂独立连接；HOLD且维护/介入/保存完毕可换任务，不必断臂。原始MP4/HDF5按任务保存，LeRobot仅显式离线转换；预览可丢帧，不得阻塞控制/录制，分进程仍可能争抢资源。

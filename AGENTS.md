# YAM 项目工作约定

## 产品与职责

- 主仓库 `yam-abc-reproduce` 默认 `main`，i2rt用固定子模块，不恢复同级旧目录。RK3588 IPC负责设备、控制与原始记录；Thor模型/训练/转换归condapi。
- 交付设备与采集平台、数据集平台，均支持局域网跨平台浏览器。按完整流程验收；复用现有机制，只在真实边界设必要保护。产品合同归 `docs/dagger_architecture.md`。
- 设备身份已有登记，换设备才重查；模拟通过不是真机通过。现场状态每次操作前复核。

## 自动提交与托管

完成本任务改动后运行适用检查与 `git diff --check`，只提交本任务文件；先推 `origin main`，再推 `github main` 并核对同一SHA。失败保留本地提交并报告，不强推。远端地址见 `docs/environment.md`；IPC访问Gitea须查开发机现址。

## 分层记忆

- 记忆规则归 `docs/memory.md`。先明确任务与缺口，再从 `docs/cache/context_index.md` 找一个owner；`kernel`仅需项目概览时读，`checkpoint`仅续作时读，历史按需检索，不重复加载已知内容。
- 先更新owner，再替换热摘要；不追加流水账、不为普通进度新增记忆文件。预算由记忆owner与 `scripts/check_project_memory.py` 检查。证据/旧尝试只保存在冷层；不得刷新旧哈希冒充复验，历史记录不是操作授权。
- 事实区分配置、用户确认、模拟、实测；复试先查失败原因和重试条件。不记录凭据、私有推理或完整环境变量。

## 环境与实施

- Python3.12/uv；复现用 `uv sync --locked --extra camera --extra gui --extra deploy`，依赖索引与互斥组按 `docs/environment.md`。四模式入口 `uv run --no-sync yam-workstation`，本站配置 `configs/station_hil.yaml`；上游GELLO配置不用于本站。
- 依赖变更提交 `pyproject.toml`/`uv.lock` 并验收；不提交虚拟环境、模型、运行数据或记忆账本。

## 设备与操作边界

- 本站为2 Follower＋2官方电动Leader＋3 D405；不是GELLO。夹爪/USB/相机身份与行程查 `docs/workstation.md`，不例行写电机零位、刷固件或关闭超时。
- 构造/连接机器人可能施力矩或校准夹爪；运动或释放力矩前确认四臂支撑、行程清空、有人照看。软件暂停不是实体急停；故障恢复不自动运动。设备进程重启影响按 `docs/deploy.md` 核对。
- 四模式：遥操作、推理、HIL、采集。单一Runtime/SDK写入者；维护与运行互斥。HIL人工复用绝对1:1遥操作，不恢复相对映射或对齐门槛；按钮/交还细节查 `docs/hil_quickstart.md`，模型合同查 `docs/condapi_interface.md`。
- Web启动不构造硬件；页面连接可能上电。本站 `factory_zero_home: true` 的Follower回零与独立的Leader回零是两项操作，均须先核验完整路径；夹爪保持，不写编码器零点。其他站按示教准备位与配置哈希。
- 采集时相机/四臂可独立连接；HOLD且维护/介入/保存完毕可换任务，不必断臂。原始MP4/HDF5按任务身份保存，LeRobot只显式离线转换。预览可丢帧，不得阻塞控制/录制；分进程仍可能争抢资源。

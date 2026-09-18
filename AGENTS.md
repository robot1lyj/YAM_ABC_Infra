# YAM 项目工作约定

## 产品与职责

- 主仓库为yam-abc-reproduce，默认main；i2rt只用固定子模块，已删除的同级i2rt不恢复。Thor模型/训练/转换归condapi；RK3588 IPC负责设备、控制、仲裁和原始记录。
- 长期交付核心为**设备与采集平台**（初始化、调试、维护、四模式运行）与**数据集平台**。围绕用户完整流程维护，日常操作和结果记录经界面；两个平台须支持局域网跨平台浏览器访问。产品原则与架构归 `docs/dagger_architecture.md`。
- 完整可靠但不过度防御：在设备/模型/进程/存储边界作必要检查，复用现有机制；不为假想场景增加重复状态机、校验、静默fallback或重试框架。改动验收围绕受影响用户流程，公共数据合同变化覆盖两平台。
- 用户确认P0身份与接线完成；不要重复要求本体S/N。当前进度查checkpoint，映射/配置查owner；运动前按实际需要核验现场，不能把P0或mock当真机运行通过。

## 自动提交与托管

每轮代码更新完成、执行适用检查和 `git diff --check` 后，自动提交本任务代码及配套配置/文档/记忆，无需再次询问；不混入其他agent未完成修改。
内网优先，提交后先 `git push -u origin main`，再 `git push github main`，核对同一SHA；失败保留本地提交并报告未同步端，不force push。
origin=`ssh://git@192.168.110.142:2222/wuyan_lyj/YAM.git`；github=`git@github.com:robot1lyj/YAM_ABC_Infra.git`；main跟踪origin/main，upstream保留i2rt-robotics/yam-abc-reproduce。

## 分层记忆

- 使用 `/home/wuyan-lyj/condapi/skills/mlops-memory/SKILL.md`，规则owner为 `docs/memory.md`。先明确任务与缺口，再从 `docs/cache/context_index.md` 选择相关owner；续作才读checkpoint。已在上下文的内容不重读，不默认展开所有链接/历史。
- 热文件预算：本文件≤6KiB，kernel≤3KiB/8主题，一级index≤4KiB，checkpoint≤3KiB；默认路径合计≤13KiB，带续作≤16KiB，按完整UTF-8字节计。每次写回先更新owner，再替换摘要并计量；超限将细节降至owner/冷资料，不另建热文件规避。具体方法与例外只在memory.md维护。
- kernel仅存产品原则/稳定约束/路由；checkpoint只存当前目标、最近结果、活跃阻断、下一步。设备序号、安装命令、完整指标、历史尝试放owner/evidence/records/archive，不把每轮成果追加到热入口。
- 硬件归 `docs/workstation.md`，环境归 `docs/environment.md`，架构归 `docs/dagger_architecture.md`，模型接口归 `docs/condapi_interface.md`；证据在 `docs/evidence/`，经验在 `docs/cache/records/`。细节按问题搜索，所有事实保留来源、单位/版本、适用条件及未知项。
- 完整规范/长期证据不设行数上限；无默认累计检索额度，不因读取计数停止任务或要求新开对话。账本仅是可选诊断，置于docs/cache/runtime/且不提交；摘要写入不代表宿主已压缩上下文。
- 复用工具或重试先查现有方法/失败原因和重试条件；历史记录不是执行授权。不刷新旧哈希冒充复验，不记录凭据/私有推理/完整环境变量；配置、用户确认、模拟、实测分别记录。优化建议在对话提出，不另建建议文档。

## 环境与实施

- uv/Python3.12；复现 `uv sync --locked --extra camera --extra gui --extra deploy`，保留项目镜像和PyTorch显式索引，不用unsafe索引策略或混装互斥训练组。
- 四模式入口 `uv run --no-sync yam-workstation`，使用configs/station_hil.yaml；上游station_yam.yaml为被动GELLO，不用于该设备。设备与数据平台命令按运行手册，端口/局域网实现状态按架构查。
- 提交pyproject.toml/uv.lock，不提交.venv、模型、运行数据/runtime ledger。配置/依赖变更做对应离线验收，不把模块可发现或mock成功写成真机通过。

## 设备与操作边界

- 2标准YAM follower＋2官方电动leader＋3 D405；leader类型yam_lead_left/right、手柄yam_teaching_handle，不是GELLO。用户已确认两只Follower为标准DM4310直线夹爪，配置类型为linear_4310；实际行程仍须P3实测。
- 不例行GELLO清零、写电机零位、刷固件或关闭超时；不显示零点标定/底层速度调参。真实机器人构造可能施力矩、自动校准夹爪；运动前确认固定、行程清空、有人照看。软件停止不能替代硬件急停。
- 四产品模式：遥操作、纯推理、DAgger/HIL、数据采集；HOLD/人工/恢复为内部状态，维护/调试不另造第五种运行模式。所有目标经单一Runtime仲裁，维护不能与策略/遥操作同时写电机。
- HIL界面/键盘介入锁定，右①解锁相对遥操作；人工①交还、②无功能。采集①开始/结束、②放弃；遥操作/推理手柄无功能，空格独立暂停。模型契约/时效/单位不因减少防御而省略。
- --web-port启动不构造硬件，界面连接可能上电；无界面CLI启动会构造设备。软件紧急暂停解除后仍保持；硬件故障不能由此恢复。本站`factory_zero_home: true`只把Follower送到官方六关节零位，Leader保持手动/重力补偿、夹爪保持，不写编码器零点；其他站仍使用已示教且与station哈希匹配的准备位。回位前须核验完整插值路径。
- 正式采集可先建/选任务，独立连接相机和四臂；无任务遥操作可在四臂保持且尚未录制时首次绑定采集任务，无需断臂。首次绑定将会话原子迁入任务目录；绑定后任务固定到该机械臂会话，换任务仍须保存并断开。任务身份随原始/LeRobot归档。记录Follower反馈、提交目标和三相机；MP4＋HDF5原始落盘，LeRobot仅显式离线转换。
- 预览≤5Hz、独立进程/有界缓存，可丢预览帧；控制或录制循环不放浏览器请求/预览编码。设备与采集平台对记录、暂停和恢复给出明确用户状态；不能把独立进程称为无资源竞争。

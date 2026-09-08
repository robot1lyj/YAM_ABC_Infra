# 专家数据导出与格式转换

当前 HIL 原始格式是 JSONL + 三路 MP4 + manifest。旧转换器不能直接读取它；先导出 canonical 格式，再选择训练格式。

## 第一步：导出连续人工段

```bash
.venv/bin/python -m yam_abc_reproduce.hil.export data/episodes/实际会话/episode_000001 --output data/expert/新目录
```

输出目录必须不存在。只有有完整观测的人工段会导出；遇到策略、保持或 epoch 变化会拆成新 episode，避免训练动作跨越隐藏间隔。标为 aborted 的录制默认拒绝导出，需先审核。

每段保留两臂状态/提交动作 NPY、三路视频、metadata、完成标志和 `hil_provenance.jsonl`。
模拟数据保留 mock 标识，不应混入真机训练数据。

## 第二步：按需要安装转换依赖

这是可选步骤，不属于运行工作站所需的轻量环境：

```bash
uv sync --locked --extra camera --extra gui --extra deploy --extra convert
```

uv 会移除本次没有选择的 extras；日常复现时保留你需要的集合。

## 第三步：转换并审核

```bash
yam-abc-convert data/expert/实际目录 --to lerobot --repo-id yam/实际任务
```

用 `yam-abc-convert --help` 查看本版本选项；ABC 输出可使用 `--to abc`。转换后检查 episode 数量、图像角色、视频帧数、14D 顺序和动作单位，再交给 condapi。

旧转换器会转换 canonical 的专家状态、动作和图像，不自动映射所有 HIL 扩展标签。保留原始录制与 provenance；全量 rollout、干预掩码或离线强化学习数据若要接入训练，需另做明确的字段适配。

当前测试验证了专家段拆分、canonical 读取和视频长度；不代表这台设备已完成 LeRobot 安装、全链路转换或模型微调验收。

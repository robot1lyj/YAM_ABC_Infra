# 项目概览

仅在AGENTS与已知owner不足以解释系统时读；不放现场快照。

- IPC控制四臂/三D405并记录；Thor负责模型，训练/转换归condapi。身份归[工作站](../workstation.md)，地址归[环境](../environment.md)。
- 遥操作、推理、HIL、采集共用单一SDK写入者；人工Leader为绝对1:1，可选键盘HIL先停模型。流程查[手册](../hil_quickstart.md)，生命周期查[部署](../deploy.md)。
- 原始MP4/HDF5/JSON与离线LeRobot分离。输入/14D动作归[接口](../condapi_interface.md)，动作/反馈/专家标记归[字段](../hil_dataset_fields.md)。

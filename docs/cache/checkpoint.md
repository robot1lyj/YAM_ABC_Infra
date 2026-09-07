# 续作检查点

## 当前目标

安装国内镜像 uv 采集环境，上传内网 origin/main，接入证据化项目记忆。

## 已处理

- 清华 PyPI 镜像配置与 uv 正式生成的锁文件；包版本条目未变化。
- 本机 uv 0.12.10；uv 管理 Python 3.12.14，替代缺开发头文件的系统 Python。
- i2rt 子模块固定到 5d47b358bafb30c65e397f2ece506550a0db4594。
- 项目记忆路由、规范所有者、有界读取工具与测试已接入。

## 软件验收完成

79 个包安装完成，locked 复现与依赖检查通过；八个关键库可导入。
选定离线测试 69 passed、1 skipped、9 subtests passed；记忆工具 18 项测试通过。
证据：docs/evidence/20260907-environment-audit.json。
推送状态需用 git remote / git ls-remote 现场核对，不从旧记录推断。

## 后续现场任务

核验平行夹爪精确型号、控制电脑与 USB-CAN/相机序列号。
当前上游 station 是被动 GELLO，改为官方 leader 前不能 Start Teleop。
本任务未运行真机初始化，不执行 GELLO 清零、固件刷新或电机零位重写。

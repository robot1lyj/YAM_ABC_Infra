# 工作记忆摘要

- 项目使用 uv / Python 3.12；采集入口为 yam-abc-gui。来源：pyproject.toml、docs/environment.md。
- 硬件为 2 follower + 2 官方电动 leader，夹爪精确型号未确认。来源：docs/workstation.md。
- 上游默认 station 是被动 GELLO，不能直接用于该硬件。来源：configs/station_yam.yaml。
- 真实构造/Start Teleop 可能立即运动；本次环境安装不包含真机验收。来源：docs/workstation.md。
- pyproject.toml、uv.lock 进 Git；.venv 和运行数据不进 Git。来源：AGENTS.md、.gitignore。

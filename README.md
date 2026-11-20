# 外部 Agent 执行监控与质量评估平台

当前实现为 Claude Code Python 采集器。它支持真实的无工具运行和离线 JSONL 回放；Web 平台仍处于设计阶段。

| 目录 | 内容 |
| --- | --- |
| [collector/](collector/) | Python 采集器源码 |
| [tests/](tests/) | 离线自动化测试 |
| [scripts/](scripts/) | 保留的 PowerShell 验证原型 |
| [docs/](docs/) | 课题介绍、系统架构与操作说明 |
| [runs/](runs/) | 本机运行产物：`live` 为真实调用，`replay` 为离线回放，`legacy` 为 PowerShell 原始日志；不提交到 Git |

从项目根目录运行离线测试：

```powershell
python -B -m unittest discover -s tests -v
```

真实调用与离线回放见 [Python 采集器使用说明](docs/PYTHON_COLLECTOR.md)，后续开发范围见 [系统架构设计](docs/系统架构设计.md)。

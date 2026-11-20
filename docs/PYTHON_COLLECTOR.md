# Python 采集器开发说明

## 目标

将 Claude Code CLI 的 `stream-json` 输出转换成规范 JSONL 事件并给出运行摘要。运行模式会启动真实 CLI，可能产生 API 费用；`--safe-mode`、空工具列表、手动权限和禁用会话持久化使其默认只进行无工具响应测试。`replay` 模式只解析本地 JSONL，不启动 Agent、不产生模型调用。

## 环境

需要 Python 3.11 或更高版本以及 PATH 上的 `claude.exe`。采集器只用 Python 标准库，不需要 pip 依赖。

```powershell
python --version
claude --version
```

## 真实运行

以下命令均从项目根目录执行。运行产物集中放在 `runs/`，不会提交到 Git。

```powershell
New-Item -ItemType Directory -Force .\runs\live | Out-Null
python -I -B .\collector\claude_collector.py run `
  --model sonnet `
  --prompt "Reply with exactly: DEMO_OK" `
  --timeout 120 `
  --output .\runs\live\collector-events.jsonl
```

注意：输出文件必须尚不存在，采集器使用独占创建，避免覆盖既有数据。可省略 `--model`，此时沿用 Claude Code 当前设置。请不要把项目根目录作为开放工具任务的工作区；当前采集器空工具列表，不执行文件修改任务。

## 离线回放

```powershell
New-Item -ItemType Directory -Force .\runs\replay | Out-Null
python -I -B .\collector\claude_collector.py replay `
  .\runs\legacy\claude-events-a2c5a22e-e6e2-4d49-be71-848484020ec4.jsonl `
  --output .\runs\replay\normalized-events.jsonl
```

回放只能验证协议解析和数据归一化，结果状态为 `replayed`，不会声称原 CLI 运行成功；原始进程退出码不是事件流可证明的事实。

## 本地测试

在项目根目录运行以下命令。测试使用本地 Python 子进程验证管道读取和超时，不产生模型调用。

```powershell
python -B -m unittest discover -s tests -v
```

## 规范事件

每条规范事件包含 `run_id`、单调递增的 `seq`、`source`、`observed_at`、`kind` 以及可用的会话/原生 ID。工具请求与结果以 `tool_use_id` 对应。`stream_event` 的逐片内容不保存，避免生成大量重复事件或保存推理内容。未知类型只留下有限元数据。Token 用量、成本和结果正文位于运行摘要中；成本是 CLI 估算值。非零退出、超时、缺少最终结果和失败终态不会标记为成功。

## 限制

这是用于下一阶段集成的本机采集原型，不包含 API、数据库、SSE 或 Windows Job Object 子进程树清理。stderr 原文只计字节数，不落盘。CLI `--safe-mode` 能力以当前安装版本行为为准，正式实现仍需确认隔离工作区与进程树回收。

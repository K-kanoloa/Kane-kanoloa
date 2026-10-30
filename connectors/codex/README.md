# Codex connector

需要已安装并登录的 Codex CLI，以及可访问的 Kane-Kanaloa API。

在项目根目录安装连接器依赖：

```sh
npm run setup:codex
```

在 Kane 界面新增外部 Agent，填写与下方相同的 Agent ID，取得一次性配对码。

Windows PowerShell：

```powershell
connectors/codex/.venv/Scripts/python.exe connectors/codex/main.py --url ws://127.0.0.1:8000/api/v1/connectors/ws --agent-id codex --display-name Codex --cwd .
```

Linux：

```sh
connectors/codex/.venv/bin/python connectors/codex/main.py --url ws://127.0.0.1:8000/api/v1/connectors/ws --agent-id codex --display-name Codex --cwd .
```

在隐藏输入提示中输入配对码，不要放入命令参数、源码或日志。连接器必须保持运行。远程连接使用可信的 `wss://` 地址。

支持流式回复、并行任务、取消和审批转发；原生恢复和分支不受此连接器支持。实际能力还取决于已安装的 Codex CLI。令牌优先保存在操作系统凭据存储中；无法存储时，退出后需要重新配对。

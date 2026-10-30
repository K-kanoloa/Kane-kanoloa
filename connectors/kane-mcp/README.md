# Kane MCP helper

在项目根目录安装：

```sh
npm run setup:mcp
```

在 MCP 客户端中设置可执行程序为连接器的独立 Python：

- Windows：`connectors/kane-mcp/.venv/Scripts/python.exe`
- Linux：`connectors/kane-mcp/.venv/bin/python`
- 参数：`connectors/kane-mcp/server.py`
- 工作目录：项目根目录。客户端需要绝对路径时，请填入本机的实际安装路径。

可选环境变量：

| 变量 | 用途 |
| --- | --- |
| `KANE_API_BASE_URL` | API 地址，默认 `http://127.0.0.1:8000` |
| `KANE_CONNECTOR_WS_URL` | Agent 可访问的 WebSocket 地址 |
| `KANE_AGENT_ID` | 要查看的 Agent ID |
| `KANE_API_TOKEN` | API 启用访问保护时的令牌 |

提供连接说明、协议、身份查询、健康检查和脱敏连接记录验证工具。MCP 用于辅助配置，外部 Agent 的持续对话仍需运行对应连接器。

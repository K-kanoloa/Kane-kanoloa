<p align="center"><img src="apps/web/public/kane-octopus.png" width="112" alt="Kane-Kanaloa"></p>

<h1 align="center">Kane / Kanaloa</h1>
<p align="center"><strong>让对话，成为行动。</strong><br>Keep the conversation. Move the work forward.</p>
<p align="center"><a href="https://k-kanoloa.github.io/Kane-Kanoloa/">项目主页</a> · <a href="#快速开始">快速开始</a> · <a href="https://github.com/K-kanoloa/Kane-Kanoloa/archive/refs/heads/main.zip">下载源码</a></p>

---

任务还没做完，对话不该从头开始。

**Kane / Kanaloa 是运行在你电脑上的 Agent 工作台。** 在同一个空间里持续对话、推进任务、切换并行工作，并在需要时决定下一步。支持中文与英文界面。

## 一张工作台，接住整段工作

| 你想做什么 | 工作台如何配合 |
| --- | --- |
| 把一件事继续做下去 | 流式回复、任务续接，保留对话上下文 |
| 同时推进几件事 | 独立任务与并行会话，按需切换 |
| 试试另一条思路 | 从消息创建分支，保留原来的讨论 |
| 在运行中调整方向 | 追加指令，按 Agent 能力送达 |
| 掌握执行节奏 | 审批、取消，以及可选 Loop |
| 使用自己的模型 | 在设置中填写 Base URL、Model、API Key |

**Kane 是工作台，Kanaloa 是内置 Agent。** 外部 Agent 可通过对应连接器接入；模型服务、账号与费用由你自行选择。

## 快速开始

需要 Git、**Node.js 24 LTS（含 npm）**、**Python 3.11 或更新版本（含 pip、venv）**。Windows 使用 PowerShell；Linux 使用终端。Linux 发行版若未提供 venv，请先安装发行版的 `python3-venv` 包。

在下载并解压的项目目录中，或克隆仓库后执行：

```sh
git clone https://github.com/K-kanoloa/Kane-Kanoloa.git
cd Kane-Kanoloa
npm run setup
npm start
```

`setup` 使用 npm 锁文件安装依赖，为 API 创建独立 Python 环境，并构建网页。成功后打开 **http://127.0.0.1:3000**。按 **Ctrl+C** 停止本次启动的服务。

安装或启动失败时运行：

```sh
npm run doctor
```

如果有多个 Python，可先设置 `PYTHON` 为 Python 可执行文件路径，再运行安装。迁移到另一台电脑后重新执行 `npm run setup`，不要复制旧的 `.venv` 或 `node_modules`。

## 配置模型

1. 打开 Kanaloa 设置，填写自己的 **Base URL、Model、API Key**。
2. 保存配置，按界面提示启用 Kanaloa。
3. 创建对话并发送消息。

不附带模型账号、API Key 或使用额度。对话是否能得到真实模型回复，取决于你配置的服务。模型配置应用于新会话。

## 使用

- 用“新任务”创建独立工作；在任务切换框中查看和选择任务。
- 任务运行中可继续发送指令，具体送达方式取决于 Agent 能力。
- 可从一条消息开始分支对话。
- 需要授权时，在界面批准或拒绝。取消不会撤销已经发生的文件修改或外部操作。
- 可在发送前开启 Loop，选择默认、自定义或无限次数；无限模式需要主动停止，并可能产生模型费用。

## 可选 Python 工具

需要 Docker Linux 引擎。Windows 请使用 Docker Desktop 的 Linux 容器模式。

```sh
npm run setup:kanaloa-tools
npm start
```

启动器在工具镜像存在时启用这些工具；未安装时普通对话仍可使用。执行工具需要界面授权。Python 变量只在对应的活动会话中保留，重启或取消后不会自动恢复。详细使用见 [Python 工具](packages/kanaloa-tools/README.md)。

## 可选外部连接器

- [Codex 连接器](connectors/codex/README.md)
- [MCP 接入辅助](connectors/kane-mcp/README.md)
- [连接协议](docs/KANE_CONNECTOR_PROTOCOL.md)

连接器和对应 Agent 必须单独运行；完成配对不等于模型或 Agent 已就绪。

旧 Local Bridge 如需使用：先运行 `npm run setup:bridge`，再运行 `npm start -- --with-bridge`。

## 本地数据与端口

默认网页端口为 3000，API 为 8000。复制 `.env.example` 为 `.env` 可修改端口或设置本地访问令牌。模型凭据请通过 Kanaloa 设置填写。

对话数据库默认位于 `apps/api/kane.db`；`KANE_SQLITE_PATH` 可更改位置。模型凭据保存在运行时自己的本地凭据存储中。备份和移动数据前先停止程序。不要上传数据库、`.env`、凭据或私人日志。

默认服务仅监听本机。若需要远程访问，应另行配置访问令牌、可信 TLS 和网络访问限制。

## License

本项目使用 [PolyForm Noncommercial 1.0.0](LICENSE)。商业使用需另行取得许可。第三方依赖的版权和许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 适合谁

希望把长时间 Agent 工作集中在本地界面中的开发者、研究者和创作者。先用一次真实任务体验，再决定是否加入自己的工作流。

喜欢这个项目，可以点一个 Star，也欢迎通过 [Issues](https://github.com/K-kanoloa/Kane-Kanoloa/issues) 提交可复现的问题。请勿在公开反馈中附带 API Key、私人对话或本地数据库。

当前版本为 **3.0.1-beta**。模型效果取决于所选服务；可选 Python 工具需要 Docker。GitHub Pages 是项目介绍页，完整工作台需在本地安装运行。

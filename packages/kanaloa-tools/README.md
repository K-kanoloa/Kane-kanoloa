# Python tools

先完成项目根目录的 `npm run setup`。这些工具另需 Docker Linux 引擎；Windows 使用 Docker Desktop 的 Linux 容器模式。

```sh
npm run setup:kanaloa-tools
```

构建完成后重新运行根目录的 `npm start`。启动器检测到工具镜像后启用工具；普通对话不要求安装 Docker。

- `ipython` 可在同一活动会话内保留 Python 变量。
- `nooa_execute` 执行有输出约束的子任务，使用当前配置的模型，可能产生额外费用。

每次执行需要 Agent 的授权流程。授权工作目录中的文件可被工具读取，按当前授权也可能被修改；请不要授权包含无关敏感文件的目录。

工具容器不访问网络，只挂载已授权的工作目录。缺少镜像时不会自动退回宿主 Python。取消、进程退出或重启会丢失活动 Python 状态，不会自动重放代码；已经完成的外部修改不会被撤销。

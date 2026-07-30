# autoBot

一个运行在 QQ 群中的 Codex 机器人：

```text
QQ / NapCatQQ -> OneBot 11 reverse WebSocket -> NoneBot2 -> Codex CLI
```

## 安全模型

- 默认仅响应群内 `@机器人`、`/ask`、`/status`、`/reset`。
- 普通问答使用 Codex `read-only` 沙箱。
- `/do` 仅对 `OPERATOR_QQ_IDS` 中的 QQ 开放，并使用 `workspace-write`。
- Codex 始终在 `CODEX_WORKDIR` 内运行；并发、超时、输出长度均有限制。
- OneBot WebSocket 必须配置共享 Token。
- 不提供 `danger-full-access` 模式。

## 群内命令

- `@机器人 问题` 或 `/ask 问题`：只读问答。
- `/do 任务`：授权操作员让 Codex 修改工作目录。
- `/reset`：清除当前群的 Codex 上下文。
- `/status`：查看连接和任务状态。
- `/help`：查看帮助。

## 部署

详细步骤见 [docs/deployment.md](docs/deployment.md)。复制 `.env.example` 为
`.env`，至少填写 `ONEBOT_ACCESS_TOKEN` 和 `OPERATOR_QQ_IDS`。

```bash
./scripts/install.sh
sudo systemctl enable --now autobot
docker compose up -d napcat
```

NapCat 首次启动后，通过 SSH 隧道访问 WebUI，扫码登录并建立 WebSocket 客户端：

```bash
ssh -L 6099:127.0.0.1:6099 server
```

浏览器打开 `http://127.0.0.1:6099/webui`，配置反向 WebSocket：

```text
ws://host.docker.internal:8080/onebot/v11/ws
```

Token 必须与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 相同。

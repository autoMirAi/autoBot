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
- 默认显式使用 `gpt-5.4-mini` 和 `low` 推理强度，优先降低群聊延迟与成本；可通过
  `CODEX_MODEL`、`CODEX_REASONING_EFFORT` 覆盖。
- OneBot WebSocket 必须配置共享 Token。
- 不提供 `danger-full-access` 模式。

## 群内命令

- `@机器人 问题` 或 `/ask 问题`：只读问答。
- `/do 任务`：授权操作员让 Codex 修改工作目录。
- `/reset`：清除当前群的 Codex 上下文。
- `/status`：查看连接和任务状态。
- `/help`：查看帮助。

## 一键启停

部署完成后，使用以下脚本管理 NoneBot 与 NapCat：

```bash
sudo ./start.sh
sudo ./stop.sh
```

`start.sh` 会启动服务、显示 NapCat WebUI 的本地访问地址、Token 和 SSH 隧道命令；WebUI 始终只监听服务器本机，不会暴露到公网。`stop.sh` 不会删除 QQ 登录状态、NapCat 配置或开机自启设置。

## 部署

详细步骤见 [docs/deployment.md](docs/deployment.md)。复制 `.env.example` 为
`.env`，至少填写 `ONEBOT_ACCESS_TOKEN` 和 `OPERATOR_QQ_IDS`。

```bash
./scripts/install.sh
sudo systemctl enable --now autobot
docker compose up -d napcat
```

## Windows ComfyUI 视频 Worker

视频任务使用持久化 SQLite 队列，Windows Worker 主动从 server 领取任务，调用本机
ComfyUI MiniMax H3 工作流，生成完成后把 MP4 上传回 server，再由 NapCat 发回原群。
ComfyUI 本身只需监听 `127.0.0.1:8188`，不要把 ComfyUI 端口开放到局域网。

server 的 `.env` 至少配置：

```dotenv
VIDEO_ENABLED=true
VIDEO_WORKER_TOKEN=<至少 32 字符的随机值>
VIDEO_PUBLIC_BASE_URL=http://host.docker.internal:8080
```

Windows 在 `workers/worker.json` 放置未跟踪的配置：

```json
{
  "server_url": "http://192.168.8.165:8080/video-worker/v1",
  "worker_token": "与 server 相同的随机值",
  "comfy_url": "http://127.0.0.1:8188",
  "worker_id": "windows-4080",
  "comfy_api_key": "Comfy API key"
}
```

运行：

```powershell
python workers/comfyui_worker.py
```

群命令：`/video 描述`、`/video_status [任务号]`、`/video_cancel [任务号]`。
可选参数必须写在描述前：`--ratio 9:16`、`--seconds 5`、`--seed 42`。

NapCat 首次启动后，通过 SSH 隧道访问 WebUI，扫码登录并建立 WebSocket 客户端：

```bash
ssh -L 6099:127.0.0.1:6099 server
```

浏览器打开 `http://127.0.0.1:6099/webui`，配置反向 WebSocket：

```text
ws://host.docker.internal:8080/onebot/v11/ws
```

Token 必须与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 相同。

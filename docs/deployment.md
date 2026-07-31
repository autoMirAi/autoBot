# 部署说明

## 1. 安装 Codex CLI

服务器需要当前 Codex CLI。安装后使用与服务相同的凭据目录登录：

```bash
export CODEX_HOME=/data/40winters/autoBot/data/codex
codex login
codex exec --sandbox read-only --skip-git-repo-check \
  -C /data/40winters/autobot-workspace "回复 ok"
```

`codex exec` 会复用该目录保存的 CLI 登录信息。不要把其中的 `auth.json`
复制进 Git、日志或聊天；`data/` 已被 `.gitignore` 排除。

## 2. 配置 autoBot

```bash
cd /data/40winters/autoBot
cp .env.example .env
chmod 600 .env
```

生成一个随机 Token：

```bash
openssl rand -hex 32
```

将结果填入 `ONEBOT_ACCESS_TOKEN`，并设置 `OPERATOR_QQ_IDS`。如只允许指定群，
同时设置 `ALLOWED_GROUP_IDS`。

## 3. 安装并启动 NoneBot 服务

```bash
chmod +x scripts/install.sh
./scripts/install.sh
systemctl enable --now autobot
systemctl status autobot
```

## 4. 启动并登录 NapCat

```bash
docker compose up -d napcat
docker logs -f autobot-napcat
```

在管理电脑建立 SSH 隧道：

```bash
ssh -L 6099:127.0.0.1:6099 server
```

访问 `http://127.0.0.1:6099/webui` 并扫码登录。在“网络配置”中新建
WebSocket 客户端：

- URL：`ws://host.docker.internal:8080/onebot/v11/ws`
- Token：与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致
- 消息格式：数组

## 5. 验证

```bash
journalctl -u autobot -f
docker logs -f autobot-napcat
```

群内依次测试 `/status`、`/ask 你好`。仅使用专门的小号，保持稳定登录环境，
避免高频发言和批量加群等容易触发 QQ 风控的行为。

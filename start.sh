#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMPOSE_FILE="$PROJECT_DIR/compose.yaml"
WEBUI_CONFIG="$PROJECT_DIR/napcat/config/webui.json"

fail() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    printf '请使用 root 执行：sudo %s\n' "$0" >&2
    exit 1
  fi
}

require_dependencies() {
  command -v systemctl >/dev/null || fail '未找到 systemctl。'
  command -v docker >/dev/null || fail '未找到 docker。'
  command -v python3 >/dev/null || fail '未找到 python3。'
  docker compose version >/dev/null 2>&1 || fail '未找到 Docker Compose V2。'
  [[ -f "$COMPOSE_FILE" ]] || fail "未找到 $COMPOSE_FILE。"
  [[ -f "$WEBUI_CONFIG" ]] || fail "未找到 $WEBUI_CONFIG。"
}

read_webui_config() {
  python3 - "$WEBUI_CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as config_file:
    config = json.load(config_file)

port = config.get("port")
token = config.get("token")
if not isinstance(port, int) or not 1 <= port <= 65535:
    raise SystemExit("WebUI 端口配置无效")
if not isinstance(token, str) or not token:
    raise SystemExit("WebUI Token 未配置")
print(f"{port}\t{token}")
PY
}

require_root
require_dependencies

printf '启动 Docker…\n'
systemctl start docker.service

printf '启动 NapCat…\n'
docker compose --project-directory "$PROJECT_DIR" -f "$COMPOSE_FILE" up -d napcat

printf '启动 autoBot…\n'
systemctl enable --now autobot.service

if ! systemctl is-active --quiet autobot.service; then
  systemctl status autobot.service --no-pager -n 30 || true
  fail 'autoBot 未能启动。'
fi

if ! docker compose --project-directory "$PROJECT_DIR" -f "$COMPOSE_FILE" ps --status running --services | grep -qx 'napcat'; then
  docker compose --project-directory "$PROJECT_DIR" -f "$COMPOSE_FILE" ps
  fail 'NapCat 容器未运行。'
fi

if ! webui_values="$(read_webui_config)"; then
  fail '无法读取 NapCat WebUI 配置。'
fi
IFS=$'\t' read -r webui_port webui_token <<< "$webui_values"

printf '\nautoBot 已启动。\n'
printf '  NoneBot systemd：%s\n' "$(systemctl is-active autobot.service)"
printf '  NapCat 容器：running\n'
printf '\nNapCat WebUI 仅监听服务器本机，不会暴露到公网。\n'
printf '在本机执行：\n'
printf '  ssh -N -L %s:127.0.0.1:%s server\n' "$webui_port" "$webui_port"
printf '然后在浏览器打开：\n'
printf '  http://127.0.0.1:%s/webui?token=%s\n' "$webui_port" "$webui_token"
printf 'WebUI Token：%s\n' "$webui_token"

if ss -tnH '( sport = :8080 )' | grep -Eq '^ESTAB'; then
  printf '\nOneBot WebSocket：已连接。\n'
else
  printf '\nOneBot WebSocket：尚未连接；请等待 NapCat 自动重连，或检查 docker logs autobot-napcat。\n'
fi

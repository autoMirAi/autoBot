#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMPOSE_FILE="$PROJECT_DIR/compose.yaml"

fail() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

if [[ ${EUID} -ne 0 ]]; then
  printf '请使用 root 执行：sudo %s\n' "$0" >&2
  exit 1
fi

command -v systemctl >/dev/null || fail '未找到 systemctl。'
command -v docker >/dev/null || fail '未找到 docker。'
docker compose version >/dev/null 2>&1 || fail '未找到 Docker Compose V2。'
[[ -f "$COMPOSE_FILE" ]] || fail "未找到 $COMPOSE_FILE。"

printf '停止 autoBot…\n'
systemctl stop autobot.service

printf '停止 NapCat…\n'
docker compose --project-directory "$PROJECT_DIR" -f "$COMPOSE_FILE" stop napcat

printf '\nautoBot 与 NapCat 已停止。\n'
printf 'QQ 登录状态和 NapCat 配置均已保留；开机自启设置未改变。\n'
printf '下次执行 sudo %s 即可恢复。\n' "$PROJECT_DIR/start.sh"

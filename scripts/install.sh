#!/usr/bin/env bash
set -euo pipefail

project_dir=/data/40winters/autoBot
cd "$project_dir"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .

install -d -m 0700 "$project_dir/data/codex"
install -d -m 0750 /data/40winters/autobot-workspace
install -m 0644 deploy/autobot.service /etc/systemd/system/autobot.service
systemctl daemon-reload

echo "Installed. Review .env, authenticate Codex, then run:"
echo "  systemctl enable --now autobot"

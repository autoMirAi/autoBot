from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from pathlib import Path


def update_env(path: Path, updates: dict[str, str]) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(f".{path.name}.video.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    if path.exists():
        os.chmod(temporary, path.stat().st_mode)
    else:
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def existing_token(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("VIDEO_WORKER_TOKEN="):
            return line.partition("=")[2].strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--worker-config", type=Path, required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--worker-id", default="windows-4080")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    args = parser.parse_args()

    token = existing_token(args.env_file)
    if len(token) < 32:
        token = secrets.token_urlsafe(48)
    update_env(
        args.env_file,
        {
            "VIDEO_ENABLED": "true",
            "VIDEO_WORKER_TOKEN": token,
            "VIDEO_PUBLIC_BASE_URL": "http://host.docker.internal:8080",
            "VIDEO_MAX_DURATION_SECONDS": "30",
        },
    )

    args.worker_config.parent.mkdir(parents=True, exist_ok=True)
    args.worker_config.write_text(
        json.dumps(
            {
                "server_url": args.server_url.rstrip("/"),
                "worker_token": token,
                "comfy_url": args.comfy_url.rstrip("/"),
                "worker_id": args.worker_id,
                "poll_seconds": 3,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(args.worker_config, 0o600)
    print(f"VIDEO_ENABLED=true; token_present=true; token_length={len(token)}")
    print(f"worker_config={args.worker_config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

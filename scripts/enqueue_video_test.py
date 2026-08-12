from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autobot.video import VideoJobStore


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def positive(values: dict[str, str], key: str, default: int) -> int:
    return int(values.get(key, str(default)))


def first_id(values: dict[str, str], key: str) -> str:
    candidates = [value.strip() for value in values.get(key, "").split(",")]
    candidate = next((value for value in candidates if value), "")
    if not candidate:
        raise RuntimeError(f"{key} does not contain an ID")
    return candidate


def test_group_id(values: dict[str, str], explicit_group_id: str | None) -> str:
    allowed = {
        value.strip()
        for value in values.get("ALLOWED_GROUP_IDS", "").split(",")
        if value.strip()
    }
    if explicit_group_id:
        if allowed and explicit_group_id not in allowed:
            raise RuntimeError("explicit group is not in ALLOWED_GROUP_IDS")
        return explicit_group_id
    if allowed:
        return sorted(allowed)[0]
    raise RuntimeError("--group-id is required when ALLOWED_GROUP_IDS is unrestricted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--group-id")
    args = parser.parse_args()

    values = read_env(args.env_file)
    group_id = test_group_id(values, args.group_id)
    user_id = first_id(values, "OPERATOR_QQ_IDS")
    state_dir = Path(values.get("AUTOBOT_STATE_DIR", "/data/40winters/autoBot/data"))
    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("test prompt is empty")

    store = VideoJobStore(state_dir / "video_jobs.sqlite3")
    try:
        job = store.create(
            group_id=group_id,
            user_id=user_id,
            request_message_id=f"production-test-{int(time.time())}",
            prompt=prompt,
            duration=5,
            ratio="16:9",
            resolution="0.4MP",
            seed=secrets.randbelow(2**32),
            daily_user_limit=positive(values, "VIDEO_DAILY_LIMIT_PER_USER", 2),
            daily_global_limit=positive(values, "VIDEO_DAILY_LIMIT_GLOBAL", 10),
            max_queued=positive(values, "VIDEO_MAX_QUEUED", 10),
        )
        print(f"job_id={job.id}; group_id={group_id}; user_id={user_id}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"


def _id_set(name: str) -> frozenset[str]:
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _flash_model() -> str:
    model = os.getenv("CODEX_MODEL", DEEPSEEK_V4_FLASH_MODEL)
    if model != DEEPSEEK_V4_FLASH_MODEL:
        raise ValueError("CODEX_MODEL must be deepseek-v4-flash")
    return model


@dataclass(frozen=True)
class Settings:
    codex_bin: str
    codex_model: str
    codex_reasoning_effort: str
    codex_workdir: Path
    state_dir: Path
    allowed_group_ids: frozenset[str]
    operator_qq_ids: frozenset[str]
    timeout_seconds: int
    max_parallel: int
    max_prompt_chars: int
    max_reply_chars: int
    session_max_turns: int
    context_history_turns: int
    context_history_chars: int
    video_enabled: bool = False
    video_worker_token: str = ""
    video_public_base_url: str = "http://host.docker.internal:8080"
    video_output_dir: Path = Path("/data/40winters/autoBot/data/video")
    video_reference_dir: Path = Path("/data/40winters/autoBot/data/video-references")
    video_max_prompt_chars: int = 1200
    video_max_file_bytes: int = 80 * 1024 * 1024
    video_max_reference_bytes: int = 15 * 1024 * 1024
    video_max_queued: int = 10
    video_claim_lease_seconds: int = 90
    video_job_timeout_seconds: int = 1800
    video_file_ttl_seconds: int = 86400
    video_max_duration_seconds: int = 30

    @classmethod
    def from_env(cls) -> Settings:
        state_dir = Path(os.getenv("AUTOBOT_STATE_DIR", "/data/40winters/autoBot/data"))
        settings = cls(
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_model=_flash_model(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "low"),
            codex_workdir=Path(os.getenv("CODEX_WORKDIR", "/data/40winters/autobot-workspace")),
            state_dir=state_dir,
            allowed_group_ids=_id_set("ALLOWED_GROUP_IDS"),
            operator_qq_ids=_id_set("OPERATOR_QQ_IDS"),
            timeout_seconds=_positive_int("CODEX_TIMEOUT_SECONDS", 300),
            max_parallel=_positive_int("MAX_PARALLEL_TASKS", 2),
            max_prompt_chars=_positive_int("MAX_PROMPT_CHARS", 6000),
            max_reply_chars=_positive_int("MAX_REPLY_CHARS", 3500),
            session_max_turns=_positive_int("CODEX_SESSION_MAX_TURNS", 8),
            context_history_turns=_positive_int("CODEX_CONTEXT_HISTORY_TURNS", 4),
            context_history_chars=_positive_int("CODEX_CONTEXT_HISTORY_CHARS", 8000),
            video_enabled=_boolean("VIDEO_ENABLED", False),
            video_worker_token=os.getenv("VIDEO_WORKER_TOKEN", "").strip(),
            video_public_base_url=os.getenv(
                "VIDEO_PUBLIC_BASE_URL", "http://host.docker.internal:8080"
            ).rstrip("/"),
            video_output_dir=Path(os.getenv("VIDEO_OUTPUT_DIR", str(state_dir / "video"))),
            video_reference_dir=Path(
                os.getenv("VIDEO_REFERENCE_DIR", str(state_dir / "video-references"))
            ),
            video_max_prompt_chars=_positive_int("VIDEO_MAX_PROMPT_CHARS", 1200),
            video_max_file_bytes=_positive_int("VIDEO_MAX_FILE_BYTES", 80 * 1024 * 1024),
            video_max_reference_bytes=_positive_int(
                "VIDEO_MAX_REFERENCE_BYTES", 15 * 1024 * 1024
            ),
            video_max_queued=_positive_int("VIDEO_MAX_QUEUED", 10),
            video_claim_lease_seconds=_positive_int("VIDEO_CLAIM_LEASE_SECONDS", 90),
            video_job_timeout_seconds=_positive_int("VIDEO_JOB_TIMEOUT_SECONDS", 1800),
            video_file_ttl_seconds=_positive_int("VIDEO_FILE_TTL_SECONDS", 86400),
            video_max_duration_seconds=_positive_int("VIDEO_MAX_DURATION_SECONDS", 30),
        )
        if settings.video_enabled and len(settings.video_worker_token) < 32:
            raise ValueError("VIDEO_WORKER_TOKEN must contain at least 32 characters")
        return settings

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

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_model=_flash_model(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "low"),
            codex_workdir=Path(os.getenv("CODEX_WORKDIR", "/data/40winters/autobot-workspace")),
            state_dir=Path(os.getenv("AUTOBOT_STATE_DIR", "/data/40winters/autoBot/data")),
            allowed_group_ids=_id_set("ALLOWED_GROUP_IDS"),
            operator_qq_ids=_id_set("OPERATOR_QQ_IDS"),
            timeout_seconds=_positive_int("CODEX_TIMEOUT_SECONDS", 300),
            max_parallel=_positive_int("MAX_PARALLEL_TASKS", 2),
            max_prompt_chars=_positive_int("MAX_PROMPT_CHARS", 6000),
            max_reply_chars=_positive_int("MAX_REPLY_CHARS", 3500),
            session_max_turns=_positive_int("CODEX_SESSION_MAX_TURNS", 8),
            context_history_turns=_positive_int("CODEX_CONTEXT_HISTORY_TURNS", 4),
            context_history_chars=_positive_int("CODEX_CONTEXT_HISTORY_CHARS", 8000),
        )

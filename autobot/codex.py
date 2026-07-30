from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from autobot.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexResult:
    text: str
    thread_id: str | None


class SessionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                scope TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._connection.commit()

    def get(self, scope: str) -> str | None:
        row = self._connection.execute(
            "SELECT thread_id FROM sessions WHERE scope = ?", (scope,)
        ).fetchone()
        return str(row[0]) if row else None

    def put(self, scope: str, thread_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO sessions(scope, thread_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                thread_id = excluded.thread_id,
                updated_at = excluded.updated_at
            """,
            (scope, thread_id, int(time.time())),
        )
        self._connection.commit()

    def delete(self, scope: str) -> None:
        self._connection.execute("DELETE FROM sessions WHERE scope = ?", (scope,))
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


class CodexRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.codex_workdir.mkdir(parents=True, exist_ok=True)
        self.sessions = SessionStore(settings.state_dir / "sessions.sqlite3")
        self._semaphore = asyncio.Semaphore(settings.max_parallel)

    @property
    def available(self) -> bool:
        return shutil.which(self.settings.codex_bin) is not None

    def reset(self, scope: str) -> None:
        self.sessions.delete(scope)

    async def ask(self, scope: str, prompt: str, *, writable: bool) -> CodexResult:
        if not self.available:
            raise RuntimeError("服务器尚未安装 Codex CLI")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("问题不能为空")
        if len(prompt) > self.settings.max_prompt_chars:
            raise ValueError(f"消息过长，最多 {self.settings.max_prompt_chars} 个字符")

        mode_scope = f"{scope}:{'write' if writable else 'read'}"
        thread_id = self.sessions.get(mode_scope)
        sandbox = "workspace-write" if writable else "read-only"
        command = [self.settings.codex_bin, "exec", "--json"]
        if thread_id:
            command.extend(["resume", thread_id, prompt])
        else:
            command.extend(
                [
                    "--sandbox",
                    sandbox,
                    "--skip-git-repo-check",
                    "-C",
                    str(self.settings.codex_workdir),
                    prompt,
                ]
            )

        async with self._semaphore:
            return await self._execute(mode_scope, command, thread_id)

    async def _execute(
        self, scope: str, command: list[str], previous_thread_id: str | None
    ) -> CodexResult:
        env = os.environ.copy()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError(f"Codex 执行超过 {self.settings.timeout_seconds} 秒") from None

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            logger.error("Codex failed with %s: %s", process.returncode, detail)
            raise RuntimeError("Codex 执行失败，请让管理员查看服务日志")

        thread_id = previous_thread_id
        final_text = ""
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                final_text = str(item.get("text") or "")

        if thread_id:
            self.sessions.put(scope, thread_id)
        if not final_text:
            final_text = "Codex 已完成任务，但没有返回文本。"
        return CodexResult(
            text=final_text[: self.settings.max_reply_chars],
            thread_id=thread_id,
        )

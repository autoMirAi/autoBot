from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

ACTIVE_STATUSES = ("queued", "claimed", "generating", "uploading", "ready")
WORKER_STATUSES = ("claimed", "generating", "uploading")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class VideoJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoJob:
    id: str
    group_id: str
    user_id: str
    request_message_id: str
    prompt: str
    duration: int
    ratio: str
    resolution: str
    seed: int
    status: str
    worker_id: str | None
    lease_until: int | None
    progress: int
    error: str | None
    file_path: str | None
    file_token: str | None
    file_size: int | None
    created_at: int
    updated_at: int
    sent_at: int | None
    attempts: int
    send_attempts: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> VideoJob:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


class VideoJobStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS video_jobs (
                id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                request_message_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                duration INTEGER NOT NULL,
                ratio TEXT NOT NULL,
                resolution TEXT NOT NULL,
                seed INTEGER NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT,
                lease_until INTEGER,
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                file_path TEXT,
                file_token TEXT,
                file_size INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                sent_at INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                send_attempts INTEGER NOT NULL DEFAULT 0,
                next_send_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_video_jobs_queue
                ON video_jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_video_jobs_user
                ON video_jobs(user_id, created_at);
            """
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _job(self, row: sqlite3.Row | None) -> VideoJob | None:
        return VideoJob.from_row(row) if row is not None else None

    def get(self, job_id: str) -> VideoJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM video_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job(row)

    def latest_for_user(self, user_id: str) -> VideoJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM video_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return self._job(row)

    def queue_position(self, job_id: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT created_at, status FROM video_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "queued":
                return None
            count = self._connection.execute(
                "SELECT COUNT(*) FROM video_jobs WHERE status = 'queued' AND created_at <= ?",
                (row["created_at"],),
            ).fetchone()[0]
        return int(count)

    @staticmethod
    def _local_day_start() -> int:
        now = datetime.now().astimezone()
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def create(
        self,
        *,
        group_id: str,
        user_id: str,
        request_message_id: str,
        prompt: str,
        duration: int,
        ratio: str,
        resolution: str,
        seed: int,
        daily_user_limit: int,
        daily_global_limit: int,
        max_queued: int,
    ) -> VideoJob:
        now = int(time.time())
        day_start = self._local_day_start()
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                active_user = connection.execute(
                    "SELECT COUNT(*) FROM video_jobs WHERE user_id = ? "
                    f"AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
                    (user_id, *ACTIVE_STATUSES),
                ).fetchone()[0]
                if active_user:
                    raise VideoJobError("你已经有一个视频任务在处理中，请等待完成后再提交。")

                daily_user = connection.execute(
                    "SELECT COUNT(*) FROM video_jobs WHERE user_id = ? AND created_at >= ?",
                    (user_id, day_start),
                ).fetchone()[0]
                if daily_user >= daily_user_limit:
                    raise VideoJobError(f"你今天的视频额度已用完（每日 {daily_user_limit} 次）。")

                daily_global = connection.execute(
                    "SELECT COUNT(*) FROM video_jobs WHERE created_at >= ?",
                    (day_start,),
                ).fetchone()[0]
                if daily_global >= daily_global_limit:
                    raise VideoJobError("今天的全局视频额度已用完，请明天再试。")

                queued = connection.execute(
                    "SELECT COUNT(*) FROM video_jobs WHERE status = 'queued'"
                ).fetchone()[0]
                if queued >= max_queued:
                    raise VideoJobError("视频队列已满，请稍后再试。")

                connection.execute(
                    """
                    INSERT INTO video_jobs (
                        id, group_id, user_id, request_message_id, prompt,
                        duration, ratio, resolution, seed, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        job_id,
                        group_id,
                        user_id,
                        request_message_id,
                        prompt,
                        duration,
                        ratio,
                        resolution,
                        seed,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        job = self.get(job_id)
        assert job is not None
        return job

    def _recover_expired(self, now: int, max_attempts: int = 3) -> None:
        self._connection.execute(
            f"""
            UPDATE video_jobs
            SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                error = CASE WHEN attempts >= ? THEN 'Windows Worker 多次超时' ELSE error END,
                worker_id = NULL,
                lease_until = NULL,
                progress = CASE WHEN attempts >= ? THEN progress ELSE 0 END,
                updated_at = ?
            WHERE status IN ({','.join('?' for _ in WORKER_STATUSES)})
              AND lease_until IS NOT NULL AND lease_until < ?
            """,
            (max_attempts, max_attempts, max_attempts, now, *WORKER_STATUSES, now),
        )

    def claim(self, worker_id: str, lease_seconds: int) -> VideoJob | None:
        now = int(time.time())
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._recover_expired(now)
                row = connection.execute(
                    "SELECT id FROM video_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE video_jobs
                    SET status = 'claimed', worker_id = ?, lease_until = ?,
                        attempts = attempts + 1, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (worker_id, now + lease_seconds, now, row["id"]),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(row["id"])

    def heartbeat(
        self, job_id: str, worker_id: str, lease_seconds: int, progress: int
    ) -> VideoJob:
        now = int(time.time())
        progress = max(0, min(99, int(progress)))
        with self._lock:
            cursor = self._connection.execute(
                f"""
                UPDATE video_jobs
                SET status = 'generating', progress = ?, lease_until = ?, updated_at = ?
                WHERE id = ? AND worker_id = ?
                  AND status IN ({','.join('?' for _ in WORKER_STATUSES)})
                """,
                (progress, now + lease_seconds, now, job_id, worker_id, *WORKER_STATUSES),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise VideoJobError("任务不属于此 Worker，或任务已取消。")
        job = self.get(job_id)
        assert job is not None
        return job

    def begin_upload(self, job_id: str, worker_id: str, lease_seconds: int) -> None:
        now = int(time.time())
        with self._lock:
            cursor = self._connection.execute(
                f"""
                UPDATE video_jobs
                SET status = 'uploading', progress = 99, lease_until = ?, updated_at = ?
                WHERE id = ? AND worker_id = ?
                  AND status IN ({','.join('?' for _ in WORKER_STATUSES)})
                """,
                (now + lease_seconds, now, job_id, worker_id, *WORKER_STATUSES),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise VideoJobError("任务不属于此 Worker，或任务已取消。")

    def complete(
        self, job_id: str, worker_id: str, file_path: Path, file_size: int
    ) -> VideoJob:
        now = int(time.time())
        file_token = secrets.token_urlsafe(32)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE video_jobs
                SET status = 'ready', progress = 100, file_path = ?, file_token = ?,
                    file_size = ?, lease_until = NULL, updated_at = ?, next_send_at = ?
                WHERE id = ? AND worker_id = ? AND status = 'uploading'
                """,
                (str(file_path), file_token, file_size, now, now, job_id, worker_id),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise VideoJobError("任务不属于此 Worker，或任务已取消。")
        job = self.get(job_id)
        assert job is not None
        return job

    def fail(self, job_id: str, worker_id: str, error: str) -> VideoJob:
        now = int(time.time())
        error = error.strip()[:800] or "视频生成失败"
        with self._lock:
            cursor = self._connection.execute(
                f"""
                UPDATE video_jobs
                SET status = 'failed', error = ?, lease_until = NULL, updated_at = ?
                WHERE id = ? AND worker_id = ?
                  AND status IN ({','.join('?' for _ in WORKER_STATUSES)})
                """,
                (error, now, job_id, worker_id, *WORKER_STATUSES),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise VideoJobError("任务不属于此 Worker，或任务已取消。")
        job = self.get(job_id)
        assert job is not None
        return job

    def cancel(self, job_id: str, user_id: str, is_operator: bool = False) -> VideoJob:
        now = int(time.time())
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM video_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise VideoJobError("找不到这个视频任务。")
            if row["user_id"] != user_id and not is_operator:
                raise VideoJobError("你只能取消自己的视频任务。")
            if row["status"] not in ACTIVE_STATUSES:
                raise VideoJobError("这个任务已经结束，无法取消。")
            self._connection.execute(
                """
                UPDATE video_jobs SET status = 'cancelled', lease_until = NULL,
                    error = '用户取消', updated_at = ? WHERE id = ?
                """,
                (now, job_id),
            )
            self._connection.commit()
        job = self.get(job_id)
        assert job is not None
        return job

    def next_ready(self) -> VideoJob | None:
        now = int(time.time())
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM video_jobs
                WHERE status = 'ready' AND send_attempts < 5 AND next_send_at <= ?
                ORDER BY created_at LIMIT 1
                """,
                (now,),
            ).fetchone()
        return self._job(row)

    def mark_sent(self, job_id: str) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                """
                UPDATE video_jobs
                SET status = 'completed', sent_at = ?, updated_at = ?, error = NULL
                WHERE id = ? AND status = 'ready'
                """,
                (now, now, job_id),
            )
            self._connection.commit()

    def mark_send_error(self, job_id: str, error: str) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                """
                UPDATE video_jobs
                SET send_attempts = send_attempts + 1, error = ?, updated_at = ?,
                    next_send_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (error.strip()[:800], now, now + 30, job_id),
            )
            self._connection.commit()

    def next_failed(self) -> VideoJob | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM video_jobs
                WHERE status = 'failed' AND sent_at IS NULL
                ORDER BY updated_at LIMIT 1
                """
            ).fetchone()
        return self._job(row)

    def mark_failure_notified(self, job_id: str) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                "UPDATE video_jobs SET sent_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'failed'",
                (now, now, job_id),
            )
            self._connection.commit()

    def file_for_download(self, job_id: str, token: str) -> Path | None:
        job = self.get(job_id)
        if (
            job is None
            or job.status not in {"ready", "completed"}
            or not job.file_path
            or not job.file_token
            or not secrets.compare_digest(job.file_token, token)
        ):
            return None
        path = Path(job.file_path)
        return path if path.is_file() else None

    def cleanup(self, output_dir: Path, ttl_seconds: int) -> int:
        cutoff = int(time.time()) - ttl_seconds
        removed = 0
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, file_path FROM video_jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND updated_at < ? AND file_path IS NOT NULL
                """,
                (cutoff,),
            ).fetchall()
            output_root = output_dir.resolve()
            for row in rows:
                path = Path(row["file_path"])
                try:
                    resolved = path.resolve()
                    if resolved.parent == output_root and resolved.is_file():
                        resolved.unlink()
                        removed += 1
                except OSError:
                    continue
                self._connection.execute(
                    "UPDATE video_jobs SET file_path = NULL, file_token = NULL WHERE id = ?",
                    (row["id"],),
                )
            self._connection.commit()
        return removed


def sha256_stream(chunks: Iterable[bytes], destination: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        destination.write(chunk)
        digest.update(chunk)
        total += len(chunk)
    return total, digest.hexdigest()

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from nonebot import get_app, get_bots, get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from autobot.config import Settings
from autobot.video import VideoJob, VideoJobError, VideoJobStore, parse_video_options

logger = logging.getLogger(__name__)
settings = Settings.from_env()
store = VideoJobStore(settings.state_dir / "video_jobs.sqlite3")
app = get_app()
driver = get_driver()

API_PREFIX = "/video-worker/v1"
WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

video_matcher = on_command("video", priority=10, block=True)
status_matcher = on_command("video_status", priority=10, block=True)
cancel_matcher = on_command("video_cancel", priority=10, block=True)
command_arg = CommandArg()


def _require_enabled() -> None:
    if not settings.video_enabled:
        raise HTTPException(status_code=503, detail="video generation is disabled")


def _require_worker(request: Request) -> None:
    _require_enabled()
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {settings.video_worker_token}"
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid worker token")


def _worker_id(value: object) -> str:
    worker_id = str(value or "")
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise HTTPException(status_code=422, detail="invalid worker_id")
    return worker_id


def _job_payload(job: VideoJob) -> dict[str, object]:
    return {
        "id": job.id,
        "prompt": job.prompt,
        "duration": job.duration,
        "ratio": job.ratio,
        "resolution": job.resolution,
        "seed": job.seed,
        "reference_count": job.reference_count,
        "timeout_seconds": settings.video_job_timeout_seconds,
    }


@app.post(f"{API_PREFIX}/jobs/claim")
async def claim_job(request: Request) -> dict[str, object | None]:
    _require_worker(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    worker_id = _worker_id(body.get("worker_id"))
    job = await asyncio.to_thread(
        store.claim, worker_id, settings.video_claim_lease_seconds
    )
    return {"job": _job_payload(job) if job else None}


@app.post(f"{API_PREFIX}/jobs/{{job_id}}/heartbeat")
async def heartbeat_job(job_id: str, request: Request) -> dict[str, object]:
    _require_worker(request)
    try:
        body = await request.json()
        worker_id = _worker_id(body.get("worker_id"))
        progress = int(body.get("progress", 0))
        job = await asyncio.to_thread(
            store.heartbeat,
            job_id,
            worker_id,
            settings.video_claim_lease_seconds,
            progress,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid heartbeat") from exc
    except VideoJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": job.status, "progress": job.progress}


@app.post(f"{API_PREFIX}/jobs/{{job_id}}/fail")
async def fail_job(job_id: str, request: Request) -> dict[str, object]:
    _require_worker(request)
    try:
        body = await request.json()
        worker_id = _worker_id(body.get("worker_id"))
        error = str(body.get("error", ""))
        job = await asyncio.to_thread(store.fail, job_id, worker_id, error)
    except VideoJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    shutil.rmtree(settings.video_reference_dir / job_id, ignore_errors=True)
    return {"status": job.status}


@app.put(f"{API_PREFIX}/jobs/{{job_id}}/result")
async def upload_result(job_id: str, request: Request) -> dict[str, object]:
    _require_worker(request)
    worker_id = _worker_id(request.headers.get("x-worker-id"))
    expected_sha256 = request.headers.get("x-content-sha256", "").lower()
    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise HTTPException(status_code=422, detail="invalid SHA-256")

    output_dir = settings.video_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / f".{job_id}.uploading"
    final = output_dir / f"{job_id}.mp4"
    digest = hashlib.sha256()
    total = 0
    try:
        await asyncio.to_thread(
            store.begin_upload,
            job_id,
            worker_id,
            settings.video_claim_lease_seconds,
        )
        with temporary.open("wb") as destination:
            async for chunk in request.stream():
                total += len(chunk)
                if total > settings.video_max_file_bytes:
                    raise HTTPException(status_code=413, detail="video file is too large")
                destination.write(chunk)
                digest.update(chunk)
        if total < 12:
            raise HTTPException(status_code=422, detail="video file is empty")
        with temporary.open("rb") as source:
            header = source.read(12)
        if header[4:8] != b"ftyp":
            raise HTTPException(status_code=422, detail="result is not an MP4 file")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 and not secrets.compare_digest(expected_sha256, actual_sha256):
            raise HTTPException(status_code=422, detail="SHA-256 mismatch")
        os.replace(temporary, final)
        job = await asyncio.to_thread(store.complete, job_id, worker_id, final, total)
        shutil.rmtree(settings.video_reference_dir / job_id, ignore_errors=True)
    except VideoJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": job.status, "size": total, "sha256": digest.hexdigest()}


@app.get(f"{API_PREFIX}/files/{{job_id}}")
async def download_result(job_id: str, token: str = "") -> FileResponse:
    _require_enabled()
    path = await asyncio.to_thread(store.file_for_download, job_id, token)
    if path is None:
        raise HTTPException(status_code=404, detail="video not found")
    return FileResponse(path, media_type="video/mp4", filename=f"autobot-{job_id}.mp4")


@app.get(f"{API_PREFIX}/jobs/{{job_id}}/references/{{index}}")
async def download_reference(job_id: str, index: int, request: Request) -> FileResponse:
    _require_worker(request)
    worker_id = _worker_id(request.headers.get("x-worker-id"))
    job = await asyncio.to_thread(store.get, job_id)
    if (
        job is None
        or job.worker_id != worker_id
        or job.status not in {"claimed", "generating", "uploading"}
        or not 1 <= index <= job.reference_count
    ):
        raise HTTPException(status_code=404, detail="reference image not found")
    directory = settings.video_reference_dir / job_id
    matches = list(directory.glob(f"{index:02d}.*"))
    if len(matches) != 1 or not matches[0].is_file():
        raise HTTPException(status_code=404, detail="reference image not found")
    return FileResponse(
        matches[0],
        media_type="application/octet-stream",
        headers={"X-Reference-Extension": matches[0].suffix.lower()},
    )


@app.get(f"{API_PREFIX}/health")
async def worker_health(request: Request) -> dict[str, object]:
    _require_worker(request)
    return {"ok": True, "time": int(time.time())}


def parse_video_request(text: str) -> tuple[str, int, str, int, int | None]:
    return parse_video_options(
        text,
        max_duration=settings.video_max_duration_seconds,
        max_prompt_chars=settings.video_max_prompt_chars,
    )


def _segment_values(message: Any) -> list[tuple[str, dict[str, Any]]]:
    values: list[tuple[str, dict[str, Any]]] = []
    for segment in message or []:
        if isinstance(segment, dict):
            segment_type = str(segment.get("type", ""))
            data = segment.get("data") or {}
        else:
            segment_type = str(getattr(segment, "type", ""))
            data = getattr(segment, "data", {}) or {}
        if isinstance(data, dict):
            values.append((segment_type, data))
    return values


def _image_urls(message: Any) -> list[str]:
    urls: list[str] = []
    for segment_type, data in _segment_values(message):
        if segment_type != "image":
            continue
        value = str(data.get("url") or data.get("file") or "").strip()
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return urls


async def _recent_picture_urls(
    bot: Bot, event: GroupMessageEvent, count: int
) -> list[str]:
    try:
        result = await bot.call_api(
            "get_group_msg_history", group_id=event.group_id, count=max(50, count * 20)
        )
    except Exception as exc:
        raise VideoJobError("无法读取近期群消息，请稍后重试。") from exc
    messages = result.get("messages", []) if isinstance(result, dict) else []
    candidates: list[tuple[int, int, list[str]]] = []
    for order, message in enumerate(messages):
        if isinstance(message, dict):
            user_id = message.get("user_id", "")
            message_id = message.get("message_id", "")
            timestamp = message.get("time", 0)
            content = message.get("message")
        else:
            user_id = getattr(message, "user_id", "")
            message_id = getattr(message, "message_id", "")
            timestamp = getattr(message, "time", 0)
            content = getattr(message, "message", None)
        if str(user_id) != str(event.user_id):
            continue
        if str(message_id) == str(event.message_id):
            continue
        urls = _image_urls(content)
        if urls:
            candidates.append((int(timestamp or 0), order, urls))
    candidates.sort(key=lambda item: (item[0], item[1]))
    urls = [url for _, _, images in candidates for url in images]
    if len(urls) < count:
        raise VideoJobError(f"最近的群消息中只找到 {len(urls)} 张你的图片，需要 {count} 张。")
    return urls[-count:]


def _image_extension(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _download_reference(url: str, temporary: Path, max_bytes: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 autoBot"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            total = 0
            header = b""
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise VideoJobError("参考图片过大，请发送小于 15MB 的图片。")
                    if len(header) < 16:
                        header = (header + chunk)[:16]
                    output.write(chunk)
    except VideoJobError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise VideoJobError("图片已经失效或下载失败，请重新发送后再试。") from exc
    extension = _image_extension(header)
    if extension is None:
        raise VideoJobError("参考图片格式不受支持，请使用 JPG、PNG、GIF 或 WebP。")
    return extension


async def _save_references(job_id: str, urls: list[str]) -> None:
    directory = settings.video_reference_dir / job_id
    directory.mkdir(parents=True, exist_ok=False)
    try:
        for index, url in enumerate(urls, start=1):
            temporary = directory / f".{index:02d}.downloading"
            extension = await asyncio.to_thread(
                _download_reference, url, temporary, settings.video_max_reference_bytes
            )
            os.replace(temporary, directory / f"{index:02d}{extension}")
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _group_allowed(event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    return not settings.allowed_group_ids or group_id in settings.allowed_group_ids


@video_matcher.handle()
async def handle_video(
    bot: Bot, event: GroupMessageEvent, args: Message = command_arg
) -> None:
    if not _group_allowed(event):
        return
    if not settings.video_enabled:
        await video_matcher.finish(Message("视频生成功能暂未启用。"))
    try:
        prompt, duration, ratio, seed, picture_count = parse_video_request(
            args.extract_plain_text().strip()
        )
        if picture_count is not None:
            reference_urls = await _recent_picture_urls(bot, event, picture_count)
        elif event.reply is not None:
            reference_urls = _image_urls(event.reply.message)[:1]
            if not reference_urls:
                raise VideoJobError("引用消息中没有可用图片，请引用一张图片后重试。")
        else:
            reference_urls = []
        job_id = uuid.uuid4().hex[:12]
        if reference_urls:
            await _save_references(job_id, reference_urls)
        job = await asyncio.to_thread(
            store.create,
            group_id=str(event.group_id),
            user_id=str(event.user_id),
            request_message_id=str(event.message_id),
            prompt=prompt,
            duration=duration,
            ratio=ratio,
            resolution="0.4MP",
            seed=seed,
            max_queued=settings.video_max_queued,
            reference_count=len(reference_urls),
            job_id=job_id,
        )
        position = await asyncio.to_thread(store.queue_position, job.id)
    except VideoJobError as exc:
        if "job_id" in locals():
            shutil.rmtree(settings.video_reference_dir / job_id, ignore_errors=True)
        await video_matcher.finish(Message(str(exc)))
        return
    await video_matcher.finish(
        Message(
            f"视频任务已进入队列。\n任务：{job.id}\n"
            f"模式：{'参考图生视频' if reference_urls else '文生视频'}"
            f"{' · ' + str(len(reference_urls)) + ' 张参考图' if reference_urls else ''}\n"
            f"规格：{duration} 秒 · 约 0.4MP · {ratio}\n当前排队：第 {position or 1} 位"
        )
    )


def _status_text(job: VideoJob, position: int | None) -> str:
    labels = {
        "queued": "排队中",
        "claimed": "Windows 已领取",
        "generating": "生成中",
        "uploading": "正在上传",
        "ready": "正在发送到 QQ",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }
    details = [f"视频任务 {job.id}", f"状态：{labels.get(job.status, job.status)}"]
    if position is not None:
        details.append(f"排队：第 {position} 位")
    if job.status in {"claimed", "generating", "uploading"}:
        details.append(f"进度：{job.progress}%")
    if job.error:
        details.append(f"说明：{job.error}")
    return "\n".join(details)


@status_matcher.handle()
async def handle_status(event: GroupMessageEvent, args: Message = command_arg) -> None:
    if not _group_allowed(event):
        return
    requested_id = args.extract_plain_text().strip()
    job = await asyncio.to_thread(
        store.get if requested_id else store.latest_for_user,
        requested_id or str(event.user_id),
    )
    if job is None:
        await status_matcher.finish(Message("没有找到视频任务。"))
    if job.user_id != str(event.user_id) and str(event.user_id) not in settings.operator_qq_ids:
        await status_matcher.finish(Message("你只能查看自己的视频任务。"))
    position = await asyncio.to_thread(store.queue_position, job.id)
    await status_matcher.finish(Message(_status_text(job, position)))


@cancel_matcher.handle()
async def handle_cancel(event: GroupMessageEvent, args: Message = command_arg) -> None:
    if not _group_allowed(event):
        return
    job_id = args.extract_plain_text().strip()
    if not job_id:
        latest = await asyncio.to_thread(store.latest_for_user, str(event.user_id))
        if latest is None:
            await cancel_matcher.finish(Message("没有找到可取消的视频任务。"))
        job_id = latest.id
    try:
        job = await asyncio.to_thread(
            store.cancel,
            job_id,
            str(event.user_id),
            str(event.user_id) in settings.operator_qq_ids,
        )
    except VideoJobError as exc:
        await cancel_matcher.finish(Message(str(exc)))
        return
    shutil.rmtree(settings.video_reference_dir / job.id, ignore_errors=True)
    await cancel_matcher.finish(Message(f"视频任务 {job.id} 已取消。"))


async def _send_video(bot: Bot, job: VideoJob) -> None:
    url = f"{settings.video_public_base_url}{API_PREFIX}/files/{job.id}?token={job.file_token}"
    await bot.send_group_msg(
        group_id=int(job.group_id),
        message=MessageSegment.video(file=url, cache=False, proxy=True, timeout=180),
    )
    try:
        await bot.send_group_msg(
            group_id=int(job.group_id),
            message=MessageSegment.at(job.user_id)
            + MessageSegment.text(f" 视频任务 {job.id} 已完成。"),
        )
    except Exception:
        logger.warning("Video sent, but completion notice failed for job %s", job.id)


async def _delivery_loop() -> None:
    last_cleanup = 0.0
    while True:
        try:
            if time.time() - last_cleanup > 3600:
                await asyncio.to_thread(
                    store.cleanup,
                    settings.video_output_dir,
                    settings.video_file_ttl_seconds,
                    settings.video_reference_dir,
                )
                last_cleanup = time.time()
            job = await asyncio.to_thread(store.next_ready)
            bots = get_bots()
            if not bots:
                await asyncio.sleep(3)
                continue
            bot = next(iter(bots.values()))
            if job is None:
                failed = await asyncio.to_thread(store.next_failed)
                if failed is not None:
                    try:
                        await bot.send_group_msg(
                            group_id=int(failed.group_id),
                            message=MessageSegment.at(failed.user_id)
                            + MessageSegment.text(
                                f" 视频任务 {failed.id} 失败：{failed.error or '未知错误'}"
                            ),
                        )
                    except Exception:
                        logger.exception("Failed to report video job failure %s", failed.id)
                        await asyncio.sleep(10)
                    else:
                        await asyncio.to_thread(store.mark_failure_notified, failed.id)
                else:
                    await asyncio.sleep(3)
                continue
            try:
                await _send_video(bot, job)
            except Exception as exc:
                logger.exception("Failed to send video job %s", job.id)
                await asyncio.to_thread(store.mark_send_error, job.id, str(exc))
            else:
                await asyncio.to_thread(store.mark_sent, job.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled video delivery loop failure")
            await asyncio.sleep(5)


delivery_task: asyncio.Task[None] | None = None


@driver.on_startup
async def start_video_delivery() -> None:
    global delivery_task
    if settings.video_enabled:
        settings.video_output_dir.mkdir(parents=True, exist_ok=True)
        settings.video_reference_dir.mkdir(parents=True, exist_ok=True)
        delivery_task = asyncio.create_task(_delivery_loop(), name="video-delivery")
        logger.info("Video generation API and delivery loop enabled")


@driver.on_shutdown
async def stop_video_delivery() -> None:
    if delivery_task is not None:
        delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
    store.close()

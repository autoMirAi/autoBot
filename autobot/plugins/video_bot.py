from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time

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


@app.get(f"{API_PREFIX}/health")
async def worker_health(request: Request) -> dict[str, object]:
    _require_worker(request)
    return {"ok": True, "time": int(time.time())}


def parse_video_request(text: str) -> tuple[str, int, str, int]:
    return parse_video_options(
        text,
        max_duration=settings.video_max_duration_seconds,
        max_prompt_chars=settings.video_max_prompt_chars,
    )


def _group_allowed(event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    return not settings.allowed_group_ids or group_id in settings.allowed_group_ids


@video_matcher.handle()
async def handle_video(event: GroupMessageEvent, args: Message = command_arg) -> None:
    if not _group_allowed(event):
        return
    if not settings.video_enabled:
        await video_matcher.finish(Message("视频生成功能暂未启用。"))
    try:
        prompt, duration, ratio, seed = parse_video_request(args.extract_plain_text().strip())
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
        )
        position = await asyncio.to_thread(store.queue_position, job.id)
    except VideoJobError as exc:
        await video_matcher.finish(Message(str(exc)))
        return
    await video_matcher.finish(
        Message(
            f"视频任务已进入队列。\n任务：{job.id}\n"
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
                    store.cleanup, settings.video_output_dir, settings.video_file_ttl_seconds
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
        delivery_task = asyncio.create_task(_delivery_loop(), name="video-delivery")
        logger.info("Video generation API and delivery loop enabled")


@driver.on_shutdown
async def stop_video_delivery() -> None:
    if delivery_task is not None:
        delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
    store.close()

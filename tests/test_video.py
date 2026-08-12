import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autobot.video import VideoJobError, VideoJobStore, parse_video_options
from workers.comfyui_worker import VideoWorker


def create_job(store: VideoJobStore, user_id: str = "100"):
    return store.create(
        group_id="200",
        user_id=user_id,
        request_message_id="300",
        prompt="a cat running through neon rain",
        duration=5,
        ratio="16:9",
        resolution="768P",
        seed=42,
        max_queued=10,
    )


class VideoJobStoreTests(unittest.TestCase):
    def test_full_job_lifecycle_and_signed_download(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = VideoJobStore(root / "video.sqlite3")
            job = create_job(store)
            self.assertEqual(store.queue_position(job.id), 1)

            claimed = store.claim("worker-1", lease_seconds=60)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, job.id)
            store.heartbeat(job.id, "worker-1", lease_seconds=60, progress=30)
            store.begin_upload(job.id, "worker-1", lease_seconds=60)

            video = root / f"{job.id}.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42video")
            ready = store.complete(job.id, "worker-1", video, video.stat().st_size)
            self.assertEqual(ready.status, "ready")
            self.assertIsNone(store.file_for_download(job.id, "wrong-token"))
            self.assertEqual(store.file_for_download(job.id, ready.file_token), video)

            store.mark_sent(job.id)
            self.assertEqual(store.get(job.id).status, "completed")
            store.close()

    def test_prevents_parallel_jobs_without_daily_limit(self) -> None:
        with TemporaryDirectory() as directory:
            store = VideoJobStore(Path(directory) / "video.sqlite3")
            first = create_job(store)
            with self.assertRaisesRegex(VideoJobError, "已经有一个"):
                create_job(store)
            store.cancel(first.id, "100")

            second = create_job(store)
            store.cancel(second.id, "100")
            third = create_job(store)
            self.assertEqual(third.status, "queued")
            store.close()

    def test_expired_claim_is_requeued(self) -> None:
        with TemporaryDirectory() as directory:
            store = VideoJobStore(Path(directory) / "video.sqlite3")
            job = create_job(store)
            store.claim("worker-dead", lease_seconds=1)
            store._connection.execute(
                "UPDATE video_jobs SET lease_until = ? WHERE id = ?",
                (int(time.time()) - 1, job.id),
            )
            store._connection.commit()
            recovered = store.claim("worker-new", lease_seconds=60)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, job.id)
            self.assertEqual(recovered.worker_id, "worker-new")
            self.assertEqual(recovered.attempts, 2)
            store.close()


class VideoRequestTests(unittest.TestCase):
    def test_defaults_to_five_seconds(self) -> None:
        prompt, duration, ratio, _ = parse_video_options("a cat running")
        self.assertEqual((prompt, duration, ratio), ("a cat running", 5, "16:9"))

    def test_short_seconds_option_supports_thirty_seconds(self) -> None:
        prompt, duration, ratio, _ = parse_video_options("-s 15 --ratio 9:16 neon cat")
        self.assertEqual((prompt, duration, ratio), ("neon cat", 15, "9:16"))

        _, duration, _, _ = parse_video_options("-s 30 maximum duration")
        self.assertEqual(duration, 30)

    def test_long_seconds_options_remain_compatible(self) -> None:
        for option in ("--seconds", "--duration"):
            _, duration, _, _ = parse_video_options(f"{option} 15 neon cat")
            self.assertEqual(duration, 15)

    def test_rejects_duration_above_thirty_seconds(self) -> None:
        with self.assertRaisesRegex(VideoJobError, "5 到 30 秒"):
            parse_video_options("-s 31 too long")


class ComfyUIWorkflowTests(unittest.TestCase):
    def test_builds_locked_local_minimax_h3_workflow(self) -> None:
        workflow = VideoWorker.build_workflow(
            {
                "id": "abc123",
                "prompt": "a cinematic cat",
                "duration": 5,
                "ratio": "9:16",
                "resolution": "768P",
                "seed": 7,
            }
        )
        generator = workflow["104"]
        self.assertEqual(generator["class_type"], "MiniMaxH3ImageToVideo")
        self.assertEqual(generator["inputs"]["prompt"], "a cinematic cat")
        self.assertEqual(generator["inputs"]["width"], 480)
        self.assertEqual(generator["inputs"]["height"], 864)
        self.assertEqual(generator["inputs"]["length"], 124)
        self.assertEqual(workflow["92"]["inputs"]["video"], ["91", 0])
        self.assertEqual(workflow["92"]["inputs"]["format"], "mp4")
        self.assertEqual(workflow["92"]["inputs"]["codec"], "auto")

    def test_finds_nested_video_metadata(self) -> None:
        metadata = VideoWorker._find_video_metadata(
            {"8": {"videos": [{"filename": "job.mp4", "subfolder": "autobot"}]}}
        )
        self.assertEqual(
            metadata,
            {"filename": "job.mp4", "subfolder": "autobot", "type": "output"},
        )


if __name__ == "__main__":
    unittest.main()

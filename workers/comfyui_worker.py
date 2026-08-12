from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkerError(RuntimeError):
    pass


class JobCancelled(WorkerError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    server_url: str
    worker_token: str
    comfy_url: str
    worker_id: str
    poll_seconds: float

    @classmethod
    def load(cls) -> WorkerConfig:
        config_path = Path(
            os.getenv("AUTOBOT_VIDEO_WORKER_CONFIG", Path(__file__).with_name("worker.json"))
        )
        values: dict[str, Any] = {}
        if config_path.is_file():
            values = json.loads(config_path.read_text(encoding="utf-8"))

        def setting(environment: str, key: str, default: str = "") -> str:
            return os.getenv(environment, str(values.get(key, default))).strip()

        config = cls(
            server_url=setting("AUTOBOT_VIDEO_SERVER_URL", "server_url").rstrip("/"),
            worker_token=setting("AUTOBOT_VIDEO_WORKER_TOKEN", "worker_token"),
            comfy_url=setting("COMFYUI_URL", "comfy_url", "http://127.0.0.1:8188").rstrip(
                "/"
            ),
            worker_id=setting(
                "AUTOBOT_VIDEO_WORKER_ID", "worker_id", socket.gethostname().lower()
            ),
            poll_seconds=float(setting("AUTOBOT_VIDEO_POLL_SECONDS", "poll_seconds", "3")),
        )
        if not config.server_url:
            raise WorkerError("AUTOBOT_VIDEO_SERVER_URL is not configured")
        if len(config.worker_token) < 32:
            raise WorkerError("AUTOBOT_VIDEO_WORKER_TOKEN is missing or too short")
        if not 0.5 <= config.poll_seconds <= 60:
            raise WorkerError("poll_seconds must be between 0.5 and 60")
        return config


class VideoWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.client_id = uuid.uuid4().hex
        self._schema_validated = False

    @property
    def _server_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.worker_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _request_json(
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code == 409:
                raise JobCancelled(detail) from exc
            raise WorkerError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except OSError as exc:
            raise WorkerError(f"Cannot reach {url}: {exc}") from exc
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"Invalid JSON from {url}") from exc

    def health(self) -> None:
        self._request_json(
            "GET",
            f"{self.config.server_url}/health",
            headers=self._server_headers,
        )
        self._request_json("GET", f"{self.config.comfy_url}/system_stats")
        if not self._schema_validated:
            self.validate_workflow_schema()
            self._schema_validated = True

    def validate_workflow_schema(self) -> None:
        sample = {
            "id": "schema-check",
            "prompt": "schema check",
            "duration": 5,
            "ratio": "16:9",
            "resolution": "0.4MP",
            "seed": 1,
        }
        workflows = [
            self.build_workflow(sample),
            self.build_workflow(sample | {"reference_count": 2}, ["schema-1.png", "schema-2.png"]),
        ]
        for workflow in workflows:
            self._validate_nodes(workflow)

    def _validate_nodes(self, workflow: dict[str, Any]) -> None:
        for node_id, node in workflow.items():
            class_type = str(node["class_type"])
            definition_payload = self._request_json(
                "GET",
                f"{self.config.comfy_url}/object_info/{urllib.parse.quote(class_type)}",
            )
            definition = definition_payload.get(class_type)
            if not isinstance(definition, dict):
                raise WorkerError(f"ComfyUI node is unavailable: {class_type}")
            input_definition = definition.get("input") or {}
            required = set((input_definition.get("required") or {}).keys())
            optional = set((input_definition.get("optional") or {}).keys())
            provided = set((node.get("inputs") or {}).keys())
            missing = required - provided
            unknown = provided - required - optional
            if class_type == "MiniMaxH3ReferenceToVideo":
                unknown = {
                    name for name in unknown if not name.startswith("ref_images.ref_image_")
                }
            if missing or unknown:
                raise WorkerError(
                    f"ComfyUI schema mismatch at node {node_id} ({class_type}): "
                    f"missing={sorted(missing)}, unknown={sorted(unknown)}"
                )

    def claim(self) -> dict[str, Any] | None:
        payload = self._request_json(
            "POST",
            f"{self.config.server_url}/jobs/claim",
            {"worker_id": self.config.worker_id},
            self._server_headers,
        )
        job = payload.get("job")
        return job if isinstance(job, dict) else None

    def heartbeat(self, job_id: str, progress: int) -> None:
        self._request_json(
            "POST",
            f"{self.config.server_url}/jobs/{job_id}/heartbeat",
            {"worker_id": self.config.worker_id, "progress": progress},
            self._server_headers,
        )

    def report_failure(self, job_id: str, error: str) -> None:
        try:
            self._request_json(
                "POST",
                f"{self.config.server_url}/jobs/{job_id}/fail",
                {"worker_id": self.config.worker_id, "error": error[:800]},
                self._server_headers,
            )
        except JobCancelled:
            return

    @staticmethod
    def dimensions_for_ratio(ratio: str) -> tuple[int, int]:
        dimensions = {
            "16:9": (864, 480),
            "4:3": (736, 544),
            "1:1": (640, 640),
            "3:4": (544, 736),
            "9:16": (480, 864),
            "21:9": (960, 416),
        }
        try:
            return dimensions[ratio]
        except KeyError as exc:
            raise WorkerError(f"Unsupported aspect ratio: {ratio}") from exc

    @staticmethod
    def build_workflow(
        job: dict[str, Any], reference_names: list[str] | None = None
    ) -> dict[str, Any]:
        job_id = str(job["id"])
        width, height = VideoWorker.dimensions_for_ratio(str(job["ratio"]))
        duration = int(job["duration"])
        frames = max(5, round(duration * 24))
        frames += (5 - frames % 17) % 17
        workflow = {
            "11": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
            },
            "24": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
            },
            "23": {
                "class_type": "VAEDecodeAudio",
                "inputs": {"samples": ["14", 0], "vae": ["24", 0]},
            },
            "10": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["14", 0], "vae": ["11", 0]},
            },
            "17": {
                "class_type": "KSamplerSelect",
                "inputs": {"sampler_name": "res_multistep"},
            },
            "9": {
                "class_type": "BasicScheduler",
                "inputs": {
                    "model": ["6", 0],
                    "scheduler": "simple",
                    "steps": 20,
                    "denoise": 1.0,
                },
            },
            "14": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["15", 0],
                    "guider": ["16", 0],
                    "sampler": ["17", 0],
                    "sigmas": ["9", 0],
                    "latent_image": ["104", 1],
                },
            },
            "16": {
                "class_type": "BasicGuider",
                "inputs": {"model": ["6", 0], "conditioning": ["104", 0]},
            },
            "6": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                    "weight_dtype": "default",
                },
            },
            "13": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                    "type": "minimax",
                    "device": "default",
                },
            },
            "15": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": int(job["seed"])},
            },
            "91": {
                "class_type": "CreateVideo",
                "inputs": {
                    "images": ["10", 0],
                    "audio": ["23", 0],
                    "fps": 24.0,
                    "bit_depth": 8,
                },
            },
            "104": {
                "class_type": "MiniMaxH3ImageToVideo",
                "inputs": {
                    "clip": ["13", 0],
                    "vae": ["11", 0],
                    "prompt": str(job["prompt"]),
                    "width": width,
                    "height": height,
                    "length": frames,
                },
            },
            "92": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["91", 0],
                    "filename_prefix": f"autobot/{job_id}",
                    "format": "mp4",
                    # DynamicCombo values are sent as the selected key; ComfyUI
                    # expands this to {"codec": "auto"} before execution.
                    "codec": "auto",
                },
            },
        }
        if reference_names:
            generator_inputs = workflow["104"]["inputs"]
            generator_inputs["audio_vae"] = ["24", 0]
            generator_inputs["ref_image_size"] = "match"
            workflow["104"]["class_type"] = "MiniMaxH3ReferenceToVideo"
            for offset, name in enumerate(reference_names):
                node_id = str(201 + offset)
                workflow[node_id] = {
                    "class_type": "LoadImage",
                    "inputs": {"image": name},
                }
                generator_inputs[f"ref_images.ref_image_{offset}"] = [node_id, 0]
        return workflow

    def download_reference(self, job_id: str, index: int, directory: Path) -> Path:
        headers = dict(self._server_headers)
        headers["X-Worker-ID"] = self.config.worker_id
        request = urllib.request.Request(
            f"{self.config.server_url}/jobs/{job_id}/references/{index}", headers=headers
        )
        destination: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                extension = response.headers.get("X-Reference-Extension", "").lower()
                if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                    raise WorkerError("Server returned an unsupported reference image type")
                destination = directory / f"reference-{index:02d}{extension}"
                with destination.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise WorkerError(f"Reference download failed with HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise WorkerError(f"Reference download failed: {exc}") from exc
        if destination is None:
            raise WorkerError("Reference download did not produce a file")
        return destination

    def upload_comfyui_image(self, path: Path, job_id: str, index: int) -> str:
        boundary = f"----autobot{uuid.uuid4().hex}"
        filename = f"{job_id}-{index:02d}{path.suffix.lower()}"
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + path.read_bytes() + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
            f"--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            f"{self.config.comfy_url}/upload/image",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerError(f"Failed to upload reference image to ComfyUI: {exc}") from exc
        name = payload.get("name")
        subfolder = str(payload.get("subfolder", "")).strip("/\\")
        if not isinstance(name, str) or not name:
            raise WorkerError(f"Invalid ComfyUI image upload response: {payload}")
        return f"{subfolder}/{name}" if subfolder else name

    def queue_comfyui(
        self, job: dict[str, Any], reference_names: list[str] | None = None
    ) -> str:
        response = self._request_json(
            "POST",
            f"{self.config.comfy_url}/prompt",
            {
                "prompt": self.build_workflow(job, reference_names),
                "client_id": self.client_id,
                "extra_data": {"comfy_usage_source": "autobot-video-worker"},
            },
            {"Content-Type": "application/json"},
        )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            errors = response.get("node_errors") or response.get("error") or response
            raise WorkerError(f"ComfyUI rejected the workflow: {errors}")
        return str(prompt_id)

    def interrupt_comfyui(self) -> None:
        try:
            self._request_json("POST", f"{self.config.comfy_url}/interrupt", {})
        except WorkerError:
            pass

    @staticmethod
    def _find_video_metadata(value: Any) -> dict[str, str] | None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and filename.lower().endswith(".mp4"):
                return {
                    "filename": filename,
                    "subfolder": str(value.get("subfolder", "")),
                    "type": str(value.get("type", "output")),
                }
            for child in value.values():
                match = VideoWorker._find_video_metadata(child)
                if match:
                    return match
        elif isinstance(value, list):
            for child in value:
                match = VideoWorker._find_video_metadata(child)
                if match:
                    return match
        return None

    def wait_for_result(self, job: dict[str, Any], prompt_id: str) -> dict[str, str]:
        started = time.monotonic()
        timeout_seconds = int(job.get("timeout_seconds", 1800))
        last_heartbeat = 0.0
        while True:
            elapsed = time.monotonic() - started
            if elapsed > timeout_seconds:
                self.interrupt_comfyui()
                raise WorkerError(f"ComfyUI timed out after {timeout_seconds} seconds")
            if elapsed - last_heartbeat >= 15 or last_heartbeat == 0:
                progress = min(90, 5 + int(elapsed / max(timeout_seconds, 1) * 85))
                try:
                    self.heartbeat(str(job["id"]), progress)
                except JobCancelled:
                    self.interrupt_comfyui()
                    raise
                last_heartbeat = elapsed

            history = self._request_json(
                "GET", f"{self.config.comfy_url}/history/{prompt_id}", timeout=30
            )
            entry = history.get(prompt_id)
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                status_text = str(status.get("status_str", ""))
                if status_text in {"error", "failed"}:
                    messages = status.get("messages") or "unknown ComfyUI error"
                    raise WorkerError(f"ComfyUI generation failed: {messages}")
                metadata = self._find_video_metadata(entry.get("outputs", {}))
                if metadata:
                    return metadata
                if status.get("completed"):
                    raise WorkerError("ComfyUI finished without an MP4 output")
            time.sleep(self.config.poll_seconds)

    def download_comfyui_result(self, metadata: dict[str, str], destination: Path) -> None:
        query = urllib.parse.urlencode(metadata)
        request = urllib.request.Request(f"{self.config.comfy_url}/view?{query}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with destination.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
        except OSError as exc:
            raise WorkerError(f"Failed to read ComfyUI video output: {exc}") from exc

    def upload_result(self, job_id: str, path: Path) -> None:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        headers = {
            "Authorization": f"Bearer {self.config.worker_token}",
            "Content-Type": "video/mp4",
            "Content-Length": str(len(payload)),
            "X-Worker-ID": self.config.worker_id,
            "X-Content-SHA256": digest,
        }
        request = urllib.request.Request(
            f"{self.config.server_url}/jobs/{job_id}/result",
            data=payload,
            method="PUT",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code == 409:
                raise JobCancelled(detail) from exc
            raise WorkerError(f"Video upload failed with HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise WorkerError(f"Video upload failed: {exc}") from exc

    def process(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        self.heartbeat(job_id, 1)
        with tempfile.TemporaryDirectory(prefix="autobot-video-") as directory:
            temporary_root = Path(directory)
            reference_names: list[str] = []
            for index in range(1, int(job.get("reference_count", 0)) + 1):
                reference = self.download_reference(job_id, index, temporary_root)
                reference_names.append(self.upload_comfyui_image(reference, job_id, index))
            self.heartbeat(job_id, 3)
            prompt_id = self.queue_comfyui(job, reference_names)
            metadata = self.wait_for_result(job, prompt_id)
            output = Path(directory) / f"{job_id}.mp4"
            self.download_comfyui_result(metadata, output)
            self.heartbeat(job_id, 95)
            self.upload_result(job_id, output)

    def run_forever(self) -> None:
        while True:
            try:
                self.health()
                break
            except WorkerError as exc:
                print(f"waiting for server and ComfyUI: {exc}", file=sys.stderr, flush=True)
                time.sleep(10)
        print(
            f"autoBot video worker ready: {self.config.worker_id} -> {self.config.server_url}",
            flush=True,
        )
        while True:
            try:
                job = self.claim()
                if job is None:
                    time.sleep(self.config.poll_seconds)
                    continue
                print(f"claimed video job {job['id']}", flush=True)
                try:
                    self.process(job)
                except JobCancelled:
                    print(f"video job {job['id']} was cancelled", flush=True)
                except Exception as exc:
                    print(f"video job {job['id']} failed: {exc}", file=sys.stderr, flush=True)
                    self.report_failure(str(job["id"]), str(exc))
                else:
                    print(f"video job {job['id']} uploaded", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"worker loop error: {exc}", file=sys.stderr, flush=True)
                time.sleep(max(5, self.config.poll_seconds))


def main() -> int:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 53182))
        lock.listen(1)
    except OSError:
        print("another autoBot video worker is already running", file=sys.stderr)
        return 0
    try:
        VideoWorker(WorkerConfig.load()).run_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"video worker stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

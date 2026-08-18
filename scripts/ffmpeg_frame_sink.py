#!/usr/bin/env python3
"""把连续 BGR24 帧直接编码为 H.264/yuv420p MP4 candidate。"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

try:
    from .media_validation import MediaValidationError
except ImportError:  # pragma: no cover - direct script/module execution
    from media_validation import MediaValidationError


_STDERR_TAIL_BYTES = 16 * 1024
_PATH_PATTERN = re.compile(
    r"(?i)(?:[a-z]:[\\/][^\r\n\t]+|/(?:[^\s:/]+/)+[^\s:]+|https?://\S+)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[-_]?key|authorization|bearer|cookie|token)\b\s*[:=]\s*\S+"
)


def _sanitized_tail(chunks: deque[bytes], *, limit: int = 1000) -> str:
    text = b"".join(chunks)[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
    text = _SECRET_PATTERN.sub(r"\1=<redacted>", text)
    text = _PATH_PATTERN.sub("<path>", text)
    compact = " ".join(text.split())
    return compact[-limit:] if compact else "无去敏错误输出"


class FFmpegFrameSink:
    """兼容 ``write(frame)`` 的单次编码 sink；成功关闭前不产生正式文件。"""

    def __init__(
        self,
        output_path: str | Path,
        *,
        width: int,
        height: int,
        fps: int,
        expected_frame_count: int,
        ffmpeg_executable: str | None = None,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        for value, label in (
            (width, "width"),
            (height, "height"),
            (fps, "fps"),
            (expected_frame_count, "expected_frame_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MediaValidationError(f"{label} 必须为正整数")
        if width % 2 or height % 2:
            raise MediaValidationError("H.264/yuv420p 输出尺寸必须为偶数")

        self.output_path = Path(output_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.expected_frame_count = expected_frame_count
        self.frame_count = 0
        self._closed = False
        self._failed = False
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_error: str | None = None

        executable = ffmpeg_executable or shutil.which("ffmpeg")
        if not executable:
            raise MediaValidationError("正式渲染缺少 ffmpeg")
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaValidationError("无法准备 FFmpeg candidate 目录") from exc

        self.argv = [
            executable,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-an",
            "-frames:v",
            str(expected_frame_count),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(self.output_path),
        ]
        try:
            self._process = popen_factory(
                self.argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError("无法启动 FFmpeg scene encoder") from exc
        if self._process.stdin is None or self._process.stderr is None:
            self._terminate_process()
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError("FFmpeg scene encoder 缺少受控 pipe")
        self._stdin: BinaryIO = self._process.stdin
        self._stderr: BinaryIO = self._process.stderr
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="ffmpeg-scene-stderr-drain",
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = self._stderr.read(8192)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self._stderr_chunks.append(bytes(chunk))
                self._stderr_size += len(chunk)
                while self._stderr_size > _STDERR_TAIL_BYTES and self._stderr_chunks:
                    removed = self._stderr_chunks.popleft()
                    self._stderr_size -= len(removed)
        except Exception:
            self._stderr_error = "stderr drain 失败"

    def _tail(self) -> str:
        return _sanitized_tail(self._stderr_chunks)

    def _finish_stderr(self) -> bool:
        self._stderr_thread.join(timeout=10)
        finished = not self._stderr_thread.is_alive()
        if finished:
            try:
                self._stderr.close()
            except OSError:
                pass
        return finished

    def _terminate_process(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except Exception:
            pass

    def _fail(self, message: str, *, cause: Exception | None = None) -> None:
        self._failed = True
        try:
            self._stdin.close()
        except Exception:
            pass
        self._terminate_process()
        self._finish_stderr()
        self.output_path.unlink(missing_ok=True)
        detail = self._tail()
        error = MediaValidationError(f"{message}: {detail}")
        if cause is None:
            raise error
        raise error from cause

    def write(self, frame: np.ndarray) -> None:
        if self._closed or self._failed:
            raise MediaValidationError("FFmpeg frame sink 已关闭")
        if self.frame_count >= self.expected_frame_count:
            self._fail("写入帧数超过 timing plan")
        if not isinstance(frame, np.ndarray):
            self._fail("BGR24 frame 必须为 numpy.ndarray")
        if frame.dtype != np.uint8 or frame.shape != (self.height, self.width, 3):
            self._fail("BGR24 frame dtype 或尺寸与 render profile 不一致")
        returncode = self._process.poll()
        if returncode is not None:
            self._fail("FFmpeg scene encoder 提前退出")
        contiguous = np.ascontiguousarray(frame)
        payload = memoryview(contiguous).cast("B")
        try:
            while payload:
                written = self._stdin.write(payload)
                if written is None:
                    written = len(payload)
                if written <= 0:
                    raise OSError("short write")
                payload = payload[written:]
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._fail("FFmpeg BGR24 stdin 写入失败", cause=exc)
        self.frame_count += 1

    def close(self) -> Path:
        if self._closed:
            if self._failed:
                raise MediaValidationError("FFmpeg frame sink 已失败")
            return self.output_path
        self._closed = True
        close_error: Exception | None = None
        try:
            self._stdin.close()
        except (BrokenPipeError, OSError, ValueError) as exc:
            close_error = exc
        try:
            returncode = self._process.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            self._failed = True
            self._terminate_process()
            self._finish_stderr()
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError("FFmpeg scene encoder 关闭超时") from exc
        if not self._finish_stderr():
            self._failed = True
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError("FFmpeg stderr drain 未完成")
        if close_error is not None:
            self._failed = True
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError(
                f"FFmpeg BGR24 stdin 关闭失败: {self._tail()}"
            ) from close_error
        if self._stderr_error is not None or returncode != 0:
            self._failed = True
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError(
                f"FFmpeg scene encoder 非零退出: {self._tail()}"
            )
        if self.frame_count != self.expected_frame_count:
            self._failed = True
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError(
                "实际写入帧数与 timing plan frameCount 不一致"
            )
        if not self.output_path.is_file() or self.output_path.stat().st_size <= 0:
            self._failed = True
            self.output_path.unlink(missing_ok=True)
            raise MediaValidationError("FFmpeg scene encoder 未生成有效 candidate")
        return self.output_path

    def abort(self) -> None:
        if self._closed and not self._failed:
            return
        self._failed = True
        self._closed = True
        try:
            self._stdin.close()
        except Exception:
            pass
        self._terminate_process()
        self._finish_stderr()
        self.output_path.unlink(missing_ok=True)

    def release(self) -> None:
        self.close()

    def __enter__(self) -> "FFmpegFrameSink":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self.abort()
            return False
        self.close()
        return False


__all__ = ["FFmpegFrameSink"]

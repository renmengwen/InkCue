#!/usr/bin/env python3
"""Python edge-tts 7.2.8 的同步 provider adapter。

该模块只负责发起请求、限制重试并返回内存中的原始媒体。项目文件、
manifest、checkpoint 和人工批准都由上层 ``voiceover.py`` 编排。
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.metadata
import inspect
import functools
import math
import re
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from voiceover import (
    CancelledError,
    PermanentProviderError,
    RawAudioResult,
    RetryableProviderError,
    SynthesisRequest,
)


EDGE_TTS_PACKAGE_VERSION = "7.2.8"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_QUEUE_INTERVAL_SECONDS = 0.5

_RETRYABLE_HTTP_STATUSES = {429, 502, 503, 504}
_CONNECTION_ERRNOS = {
    value
    for name in (
        "ECONNABORTED",
        "ECONNREFUSED",
        "ECONNRESET",
        "ENETDOWN",
        "ENETRESET",
        "ENETUNREACH",
        "EHOSTDOWN",
        "EHOSTUNREACH",
        "ETIMEDOUT",
    )
    if isinstance((value := getattr(errno, name, None)), int)
}
_RATE_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+Hz$")


def sanitize_provider_request_id(value: object | None) -> str | None:
    """把 provider request id 转为不可逆、可审计的短摘要。"""

    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _token_cancelled(token: object | None) -> bool:
    if token is None:
        return False
    for name in ("is_cancelled", "cancelled", "is_set"):
        member = getattr(token, name, None)
        try:
            value = member() if callable(member) else member
        except TypeError:
            continue
        if isinstance(value, bool) and value:
            return True
    return False


def _raise_if_cancelled(token: object | None) -> None:
    if _token_cancelled(token):
        raise CancelledError("Edge TTS 请求已取消")


def _http_status(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return exc.__class__.__name__ in {
        "TimeoutError",
        "ServerTimeoutError",
        "ConnectionTimeoutError",
        "SocketTimeoutError",
    }


def _is_connection_failure(exc: BaseException) -> bool:
    if isinstance(exc, (socket.gaierror, ConnectionError)):
        return True
    if isinstance(exc, OSError) and exc.errno in _CONNECTION_ERRNOS:
        return True
    return exc.__class__.__name__ in {
        "ClientConnectionError",
        "ClientConnectorError",
        "ClientOSError",
        "ServerConnectionError",
        "ServerDisconnectedError",
    }


def _classify_exception(exc: BaseException) -> BaseException:
    """按冻结合同分类，错误文本不回显 URL、Cookie 或 request id。"""

    if isinstance(exc, (CancelledError, PermanentProviderError, RetryableProviderError)):
        return exc
    if isinstance(exc, asyncio.CancelledError):
        return CancelledError("Edge TTS 请求已取消")
    status = _http_status(exc)
    if status in _RETRYABLE_HTTP_STATUSES:
        return RetryableProviderError(f"Edge TTS 暂时不可用（HTTP {status}）")
    if status is not None:
        return PermanentProviderError(f"Edge TTS 请求被拒绝（HTTP {status}）")
    if _is_timeout(exc):
        return RetryableProviderError("Edge TTS 请求超时")
    if _is_connection_failure(exc):
        return RetryableProviderError("Edge TTS DNS 或连接失败")
    return PermanentProviderError(
        f"Edge TTS 配置或协议失败（{exc.__class__.__name__}）"
    )


def _edge_sdk_timeout_seconds(value: object) -> int:
    """把外部数值 timeout 转为 edge-tts 7.2.8 要求的正整数秒。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value <= 0
    ):
        raise PermanentProviderError("Edge TTS timeoutSeconds 必须为正有限数")
    # 向上取整不会缩短调用方给出的等待时间；小于 1 秒的正数也不能变成 0。
    return max(1, math.ceil(value))


def _validate_request(request: SynthesisRequest) -> None:
    if not isinstance(request.text, str) or not request.text.strip():
        raise PermanentProviderError("Edge TTS 朗读文本不能为空")
    if not isinstance(request.voice, str) or not request.voice.strip():
        raise PermanentProviderError("Edge TTS voice 不能为空")
    _edge_sdk_timeout_seconds(request.timeoutSeconds)
    if not _RATE_RE.fullmatch(request.normalizedRate):
        raise PermanentProviderError("Edge TTS normalizedRate 格式无效")
    if not _PITCH_RE.fullmatch(request.normalizedPitch):
        raise PermanentProviderError("Edge TTS normalizedPitch 格式无效")
    if not _RATE_RE.fullmatch(request.normalizedVolume):
        raise PermanentProviderError("Edge TTS normalizedVolume 格式无效")


@functools.lru_cache(maxsize=1)
def _load_edge_tts() -> Any:
    try:
        installed = importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PermanentProviderError(
            "未安装 edge-tts 7.2.8；请先准备 edge-tts feature 环境"
        ) from exc
    if installed != EDGE_TTS_PACKAGE_VERSION:
        raise PermanentProviderError(
            f"edge-tts 版本不匹配：需要 {EDGE_TTS_PACKAGE_VERSION}，实际 {installed}"
        )
    try:
        import edge_tts  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PermanentProviderError("无法导入 edge-tts 7.2.8") from exc
    return edge_tts


@functools.lru_cache(maxsize=None)
def _communicate_timeout_parameters(communicate: object) -> frozenset[str]:
    """Cache stable SDK constructor capabilities for the CLI process."""

    return frozenset(inspect.signature(communicate).parameters)


async def _collect_edge_audio(request: SynthesisRequest) -> bytes:
    edge_tts = _load_edge_tts()
    sdk_timeout_seconds = _edge_sdk_timeout_seconds(request.timeoutSeconds)
    kwargs = {
        "text": request.text,
        "voice": request.voice,
        "rate": request.normalizedRate,
        "pitch": request.normalizedPitch,
        "volume": request.normalizedVolume,
    }
    timeout_parameters = _communicate_timeout_parameters(edge_tts.Communicate)
    if "connect_timeout" in timeout_parameters:
        kwargs["connect_timeout"] = sdk_timeout_seconds
    if "receive_timeout" in timeout_parameters:
        kwargs["receive_timeout"] = sdk_timeout_seconds
    communicator = edge_tts.Communicate(**kwargs)
    audio_chunks: list[bytes] = []
    async for chunk in communicator.stream():
        _raise_if_cancelled(request.cancellationToken)
        if chunk.get("type") == "audio":
            data = chunk.get("data")
            if not isinstance(data, bytes):
                raise PermanentProviderError("Edge TTS 返回了无效音频 chunk")
            audio_chunks.append(data)
    if not audio_chunks:
        raise PermanentProviderError("Edge TTS 未返回音频数据")
    return b"".join(audio_chunks)


def _default_request_executor(request: SynthesisRequest) -> bytes:
    async def run() -> bytes:
        task = asyncio.create_task(_collect_edge_audio(request))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(request.timeoutSeconds)
        try:
            while True:
                if _token_cancelled(request.cancellationToken):
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise CancelledError("Edge TTS 请求已取消")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise TimeoutError("Edge TTS request timeout")
                done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    raise PermanentProviderError("同步 Edge TTS adapter 不能在运行中的 event loop 内调用")


class EdgeTtsAdapter:
    """线程安全的同步 Edge TTS adapter；共享实例仍强制请求启动间隔。"""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        queue_interval_seconds: float = DEFAULT_QUEUE_INTERVAL_SECONDS,
        request_executor: Callable[[SynthesisRequest], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1 到 10 的整数")
        if queue_interval_seconds < 0:
            raise ValueError("queue_interval_seconds 不能为负数")
        self.max_attempts = max_attempts
        self.queue_interval_seconds = float(queue_interval_seconds)
        self._request_executor = request_executor or _default_request_executor
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_started: float | None = None
        self._queue_lock = threading.Lock()

    def _wait_for_queue_slot(self, token: object | None) -> None:
        with self._queue_lock:
            _raise_if_cancelled(token)
            now = self._monotonic()
            if self._last_request_started is not None:
                remaining = self.queue_interval_seconds - (
                    now - self._last_request_started
                )
                while remaining > 0:
                    _raise_if_cancelled(token)
                    self._sleep(min(remaining, 0.1))
                    now = self._monotonic()
                    remaining = self.queue_interval_seconds - (
                        now - self._last_request_started
                    )
            _raise_if_cancelled(token)
            self._last_request_started = self._monotonic()

    @staticmethod
    def _coerce_result(value: object) -> RawAudioResult:
        if isinstance(value, RawAudioResult):
            media = value.bytes
            declared_format = value.declaredFormat
            request_id = value.providerRequestId
        elif isinstance(value, bytes):
            media = value
            declared_format = "audio/mpeg"
            request_id = None
        elif isinstance(value, tuple) and len(value) in {2, 3}:
            media = value[0]
            declared_format = value[1]
            request_id = value[2] if len(value) == 3 else None
        else:
            raise PermanentProviderError("Edge TTS adapter 返回类型无效")
        if not isinstance(media, bytes) or not media:
            raise PermanentProviderError("Edge TTS 返回了空音频")
        if not isinstance(declared_format, str) or not declared_format.strip():
            raise PermanentProviderError("Edge TTS 未声明原始媒体格式")
        return RawAudioResult(
            media,
            declared_format,
            sanitize_provider_request_id(request_id),
        )

    def synthesize(self, request: SynthesisRequest) -> RawAudioResult:
        _validate_request(request)
        last_retryable: RetryableProviderError | None = None
        for attempt in range(1, self.max_attempts + 1):
            _raise_if_cancelled(request.cancellationToken)
            self._wait_for_queue_slot(request.cancellationToken)
            try:
                value = self._request_executor(request)
                _raise_if_cancelled(request.cancellationToken)
                return self._coerce_result(value)
            except asyncio.CancelledError as exc:
                raise CancelledError("Edge TTS 请求已取消") from exc
            except Exception as exc:
                classified = _classify_exception(exc)
                if isinstance(classified, CancelledError):
                    raise classified from exc
                if isinstance(classified, PermanentProviderError):
                    raise classified from exc
                if not isinstance(classified, RetryableProviderError):
                    raise PermanentProviderError("Edge TTS 未知协议失败") from exc
                last_retryable = classified
                if attempt == self.max_attempts:
                    break
        assert last_retryable is not None
        raise RetryableProviderError(
            f"Edge TTS 可重试失败已耗尽（{self.max_attempts} 次）: {last_retryable}"
        ) from last_retryable


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_QUEUE_INTERVAL_SECONDS",
    "EDGE_TTS_PACKAGE_VERSION",
    "EdgeTtsAdapter",
    "sanitize_provider_request_id",
]

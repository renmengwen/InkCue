#!/usr/bin/env python3
"""豆包语音 Seed Audio HTTP adapter；只返回原始 WAV，不写项目文件。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any, Mapping

try:
    from .voiceover import (
        CancelledError,
        PermanentProviderError,
        RawAudioResult,
        RetryableProviderError,
        SynthesisRequest,
    )
except ImportError:  # pragma: no cover
    from voiceover import (
        CancelledError,
        PermanentProviderError,
        RawAudioResult,
        RetryableProviderError,
        SynthesisRequest,
    )


DOUBAO_PROVIDER_CONTRACT_VERSION = "doubao-seed-audio-http-v1"
DOUBAO_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/create"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_QUEUE_INTERVAL_SECONDS = 0.5
_RETRYABLE_HTTP_STATUSES = {429, 502, 503, 504}
_RATE_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+Hz$")
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "限流",
    "请求过于频繁",
)


def sanitize_provider_request_id(value: object | None) -> str | None:
    """把 X-Tt-Logid 转成不可逆、可审计的短摘要。"""

    if value is None or not str(value).strip():
        return None
    return "sha256:" + hashlib.sha256(
        str(value).strip().encode("utf-8")
    ).hexdigest()[:16]


def _cancelled(token: object | None) -> bool:
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


def _raise_cancelled(token: object | None) -> None:
    if _cancelled(token):
        raise CancelledError("豆包语音请求已取消")


def _signed_integer(value: str, *, suffix: str, label: str) -> int:
    pattern = _PITCH_RE if suffix == "Hz" else _RATE_RE
    if not pattern.fullmatch(value):
        raise PermanentProviderError(f"豆包 {label} 格式无效")
    return int(value[: -len(suffix)])


def _rate(value: str) -> int:
    result = _signed_integer(value, suffix="%", label="normalizedRate")
    if not -50 <= result <= 100:
        raise PermanentProviderError("豆包 speech_rate 必须位于 -50–100")
    return result


def _volume(value: str) -> int:
    result = _signed_integer(value, suffix="%", label="normalizedVolume")
    if not -50 <= result <= 100:
        raise PermanentProviderError("豆包 loudness_rate 必须位于 -50–100")
    return result


def _pitch(value: str) -> int:
    result = _signed_integer(value, suffix="Hz", label="normalizedPitch")
    if not -12 <= result <= 12:
        raise PermanentProviderError("豆包 pitch_rate 必须位于 -12–12")
    return result


def _is_rate_limited(message: object | None) -> bool:
    normalized = str(message or "").strip().lower()
    return any(marker in normalized for marker in _RATE_LIMIT_MARKERS)


class DoubaoAdapter:
    """线程安全的同步豆包 HTTP adapter；共享实例强制请求启动间隔。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "seed-audio-1.0",
        endpoint: str = DOUBAO_ENDPOINT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        queue_interval_seconds: float = DEFAULT_QUEUE_INTERVAL_SECONDS,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise PermanentProviderError("豆包 apiKey 未配置")
        if not isinstance(model, str) or not model.strip():
            raise PermanentProviderError("豆包 model 未配置")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise PermanentProviderError("豆包 endpoint 必须是 HTTPS 地址")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1 到 10 的整数")
        if (
            not math.isfinite(float(queue_interval_seconds))
            or float(queue_interval_seconds) < 0
        ):
            raise ValueError("queue_interval_seconds 必须是非负有限数")
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.max_attempts = max_attempts
        self.queue_interval_seconds = float(queue_interval_seconds)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._request_id_factory = request_id_factory
        self._last_request_started: float | None = None
        self._queue_lock = threading.Lock()

    def _wait(self, token: object | None) -> None:
        _raise_cancelled(token)
        with self._queue_lock:
            now = self._monotonic()
            if self._last_request_started is not None:
                remaining = self.queue_interval_seconds - (
                    now - self._last_request_started
                )
                while remaining > 0:
                    _raise_cancelled(token)
                    self._sleep(min(remaining, 0.1))
                    remaining = self.queue_interval_seconds - (
                        self._monotonic() - self._last_request_started
                    )
            _raise_cancelled(token)
            self._last_request_started = self._monotonic()

    def _payload(self, request: SynthesisRequest) -> bytes:
        if not isinstance(request.text, str) or not request.text.strip():
            raise PermanentProviderError("豆包朗读文本不能为空")
        if len(request.text) > 3000:
            raise PermanentProviderError("豆包 text_prompt 不能超过 3000 字符")
        if not isinstance(request.voice, str) or not request.voice.strip():
            raise PermanentProviderError("豆包 speaker 不能为空")
        if request.providerContractVersion != DOUBAO_PROVIDER_CONTRACT_VERSION:
            raise PermanentProviderError("豆包 provider contractVersion 不匹配")
        payload = {
            "model": self.model,
            "text_prompt": request.text,
            "references": [{"speaker": request.voice}],
            "audio_config": {
                "format": "wav",
                "sample_rate": 24000,
                "speech_rate": _rate(request.normalizedRate),
                "loudness_rate": _volume(request.normalizedVolume),
                "pitch_rate": _pitch(request.normalizedPitch),
                "enable_subtitle": False,
            },
        }
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def _request_once(self, request: SynthesisRequest) -> RawAudioResult:
        http_request = urllib.request.Request(
            self.endpoint,
            data=self._payload(request),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self.api_key,
                "X-Api-Request-Id": str(self._request_id_factory()),
            },
        )
        try:
            with self._opener(
                http_request, timeout=float(request.timeoutSeconds)
            ) as response:
                raw = response.read()
                headers = getattr(response, "headers", None)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE_HTTP_STATUSES:
                raise RetryableProviderError(
                    f"豆包语音暂时不可用（HTTP {exc.code}）"
                ) from exc
            raise PermanentProviderError(
                f"豆包语音请求被拒绝（HTTP {exc.code}）"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            OSError,
        ) as exc:
            raise RetryableProviderError("豆包语音 DNS、连接或请求超时") from exc

        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentProviderError("豆包语音返回了无效 JSON") from exc
        if not isinstance(value, Mapping):
            raise PermanentProviderError("豆包语音响应顶层必须是对象")
        # Seed Audio success responses may omit ``code`` entirely and return
        # the Base64 ``audio`` field directly.  Only an explicitly present,
        # non-zero business code is an error.
        if "code" in value and value.get("code") != 0:
            message = value.get("message")
            if _is_rate_limited(message):
                raise RetryableProviderError("豆包语音 provider 明确限流")
            raise PermanentProviderError("豆包语音 provider 返回业务错误")
        audio = value.get("audio")
        if not isinstance(audio, str) or not audio:
            raise PermanentProviderError("豆包语音返回缺少 Base64 音频")
        try:
            media = base64.b64decode(audio, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PermanentProviderError("豆包语音返回了无效 Base64 音频") from exc
        if not media:
            raise PermanentProviderError("豆包语音返回了空音频")
        header_get = getattr(headers, "get", None)
        log_id = header_get("X-Tt-Logid") if callable(header_get) else None
        return RawAudioResult(
            media,
            "audio/wav",
            sanitize_provider_request_id(log_id),
        )

    def synthesize(self, request: SynthesisRequest) -> RawAudioResult:
        last_retryable: RetryableProviderError | None = None
        for attempt in range(1, self.max_attempts + 1):
            _raise_cancelled(request.cancellationToken)
            self._wait(request.cancellationToken)
            try:
                result = self._request_once(request)
                _raise_cancelled(request.cancellationToken)
                return result
            except CancelledError:
                raise
            except RetryableProviderError as exc:
                last_retryable = exc
                if attempt == self.max_attempts:
                    break
        assert last_retryable is not None
        raise RetryableProviderError(
            f"豆包语音可重试失败已耗尽（{self.max_attempts} 次）: {last_retryable}"
        ) from last_retryable


__all__ = [
    "DOUBAO_ENDPOINT",
    "DOUBAO_PROVIDER_CONTRACT_VERSION",
    "DoubaoAdapter",
    "sanitize_provider_request_id",
]

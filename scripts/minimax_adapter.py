#!/usr/bin/env python3
"""MiniMax T2A V2 的同步 adapter；只返回原始 MP3，不写项目文件。"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Mapping

try:
    from .voiceover import CancelledError, PermanentProviderError, RawAudioResult, RetryableProviderError, SynthesisRequest
except ImportError:  # pragma: no cover
    from voiceover import CancelledError, PermanentProviderError, RawAudioResult, RetryableProviderError, SynthesisRequest


MINIMAX_PROVIDER_CONTRACT_VERSION = "minimax-t2a-v2-v1"
MINIMAX_ENDPOINT = "https://api.minimaxi.com/v1/t2a_v2"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_QUEUE_INTERVAL_SECONDS = 0.5
_RATE_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+Hz$")
_RETRYABLE = {429, 502, 503, 504}


def sanitize_provider_request_id(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    return "sha256:" + hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()[:16]


def _cancelled(token: object | None) -> bool:
    if token is None:
        return False
    for name in ("is_cancelled", "cancelled", "is_set"):
        member = getattr(token, name, None)
        value = member() if callable(member) else member
        if isinstance(value, bool) and value:
            return True
    return False


def _raise_cancelled(token: object | None) -> None:
    if _cancelled(token):
        raise CancelledError("MiniMax TTS 请求已取消")


def _rate(value: str) -> float:
    if not _RATE_RE.fullmatch(value):
        raise PermanentProviderError("MiniMax normalizedRate 格式无效")
    result = 1.0 + int(value[:-1]) / 100.0
    if not 0.5 <= result <= 2.0:
        raise PermanentProviderError("MiniMax speed 必须位于 0.5–2")
    return round(result, 4)


def _volume(value: str) -> float:
    if not _RATE_RE.fullmatch(value):
        raise PermanentProviderError("MiniMax normalizedVolume 格式无效")
    result = 1.0 + int(value[:-1]) / 100.0
    if not 0 < result <= 10:
        raise PermanentProviderError("MiniMax vol 必须大于 0 且不超过 10")
    return round(result, 4)


def _pitch(value: str) -> int:
    if not _PITCH_RE.fullmatch(value):
        raise PermanentProviderError("MiniMax normalizedPitch 格式无效")
    result = int(value[:-2])
    if not -12 <= result <= 12:
        raise PermanentProviderError("MiniMax pitch 必须位于 -12–12")
    return result


def _http_status(exc: BaseException) -> int | None:
    try:
        status = int(getattr(exc, "code", getattr(exc, "status", -1)))
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


class MiniMaxAdapter:
    def __init__(self, *, api_key: str, model: str = "speech-2.8-hd", emotion: str = "calm", text_normalization: bool = True,
                 endpoint: str = MINIMAX_ENDPOINT, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 queue_interval_seconds: float = DEFAULT_QUEUE_INTERVAL_SECONDS,
                 opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise PermanentProviderError("MiniMax apiKey 未配置")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1 到 10 的整数")
        self.api_key = api_key
        self.model = model
        self.emotion = emotion
        self.text_normalization = bool(text_normalization)
        self.endpoint = endpoint
        self.max_attempts = max_attempts
        self.queue_interval_seconds = float(queue_interval_seconds)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_started: float | None = None

    def _wait(self, token: object | None) -> None:
        _raise_cancelled(token)
        now = self._monotonic()
        if self._last_request_started is not None:
            remaining = self.queue_interval_seconds - (now - self._last_request_started)
            while remaining > 0:
                _raise_cancelled(token)
                self._sleep(min(remaining, 0.1))
                remaining = self.queue_interval_seconds - (self._monotonic() - self._last_request_started)
        _raise_cancelled(token)
        self._last_request_started = self._monotonic()

    def _payload(self, request: SynthesisRequest) -> bytes:
        if not request.text.strip():
            raise PermanentProviderError("MiniMax 朗读文本不能为空")
        if request.providerContractVersion != MINIMAX_PROVIDER_CONTRACT_VERSION:
            raise PermanentProviderError("MiniMax provider contractVersion 不匹配")
        payload = {
            "model": self.model,
            "text": request.text,
            "stream": False,
            "voice_setting": {
                "voice_id": request.voice,
                "speed": _rate(request.normalizedRate),
                "vol": _volume(request.normalizedVolume),
                "pitch": _pitch(request.normalizedPitch),
                "emotion": self.emotion,
                "text_normalization": self.text_normalization,
            },
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
            "subtitle_enable": False,
            "output_format": "hex",
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _request_once(self, request: SynthesisRequest) -> RawAudioResult:
        data = self._payload(request)
        http_request = urllib.request.Request(
            self.endpoint, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(http_request, timeout=float(request.timeoutSeconds)) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE:
                raise RetryableProviderError(f"MiniMax 请求暂时不可用（HTTP {exc.code}）") from exc
            raise PermanentProviderError(f"MiniMax 请求被拒绝（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise RetryableProviderError("MiniMax DNS、连接或请求超时") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentProviderError("MiniMax 返回了无效 JSON") from exc
        base = value.get("base_resp") if isinstance(value, Mapping) else None
        if not isinstance(base, Mapping) or base.get("status_code") != 0:
            message = base.get("status_msg") if isinstance(base, Mapping) else None
            safe = str(message)[:160] if message else "未知 provider 错误"
            raise PermanentProviderError(f"MiniMax provider 错误: {safe}")
        audio = value.get("data", {}).get("audio") if isinstance(value.get("data"), Mapping) else None
        if not isinstance(audio, str) or not audio:
            raise PermanentProviderError("MiniMax 返回缺少 hex 音频")
        try:
            media = bytes.fromhex(audio)
        except ValueError as exc:
            raise PermanentProviderError("MiniMax 返回了无效 hex 音频") from exc
        if not media:
            raise PermanentProviderError("MiniMax 返回了空音频")
        return RawAudioResult(media, "audio/mpeg", sanitize_provider_request_id(value.get("trace_id")))

    def synthesize(self, request: SynthesisRequest) -> RawAudioResult:
        last: RetryableProviderError | None = None
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
                last = exc
                if attempt == self.max_attempts:
                    break
        assert last is not None
        raise RetryableProviderError(f"MiniMax 可重试失败已耗尽（{self.max_attempts} 次）: {last}") from last


__all__ = ["MINIMAX_ENDPOINT", "MINIMAX_PROVIDER_CONTRACT_VERSION", "MiniMaxAdapter", "sanitize_provider_request_id"]

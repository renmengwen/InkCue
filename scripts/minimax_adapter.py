#!/usr/bin/env python3
"""MiniMax T2A V2 的同步 adapter；只返回原始 MP3，不写项目文件。"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Mapping

try:
    from .voiceover import CancelledError, PermanentProviderError, RawAudioResult, RetryableProviderError, SynthesisRequest
except ImportError:  # pragma: no cover
    from voiceover import CancelledError, PermanentProviderError, RawAudioResult, RetryableProviderError, SynthesisRequest


MINIMAX_ENDPOINT = "https://api.minimaxi.com/v1/t2a_v2"
MINIMAX_SUBTITLE_TYPE = "word"
MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_QUEUE_INTERVAL_SECONDS = 0.5
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 35.0
_RATE_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+Hz$")
_RETRYABLE = {429, 502, 503, 504}
_RATE_LIMIT_MARKERS = ("rate limit", "rpm", "too many requests", "限流", "请求过于频繁")


class _MiniMaxRetryableError(RetryableProviderError):
    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after_seconds = retry_after_seconds


class _MiniMaxSubtitleUnavailable(PermanentProviderError):
    """Audio response existed, but its native subtitle artifact is unusable."""

    provider_response_received = True
    external_result_incomplete = True


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


def _retry_after_seconds(headers: object | None) -> float | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if 0 < seconds <= 300 else None


def _is_rate_limited(status_message: object | None) -> bool:
    normalized = str(status_message or "").strip().lower()
    return any(marker in normalized for marker in _RATE_LIMIT_MARKERS)


class MiniMaxAdapter:
    def __init__(self, *, api_key: str, model: str = "speech-2.8-hd", emotion: str = "calm", text_normalization: bool = True,
                 endpoint: str = MINIMAX_ENDPOINT, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 queue_interval_seconds: float = DEFAULT_QUEUE_INTERVAL_SECONDS,
                 requests_per_minute: int | None = None,
                 rate_limit_backoff_seconds: float = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
                 native_word_subtitles: bool = False,
                 opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise PermanentProviderError("MiniMax apiKey 未配置")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1 到 10 的整数")
        if requests_per_minute is not None and (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, int)
            or not 1 <= requests_per_minute <= 600
        ):
            raise ValueError("requests_per_minute 必须是 1 到 600 的整数或 None")
        if not math.isfinite(float(queue_interval_seconds)) or float(queue_interval_seconds) < 0:
            raise ValueError("queue_interval_seconds 必须是非负有限数")
        if (
            not math.isfinite(float(rate_limit_backoff_seconds))
            or not 1 <= float(rate_limit_backoff_seconds) <= 300
        ):
            raise ValueError("rate_limit_backoff_seconds 必须位于 1–300 秒")
        self.api_key = api_key
        self.model = model
        self.emotion = emotion
        self.text_normalization = bool(text_normalization)
        self.endpoint = endpoint
        self.max_attempts = max_attempts
        rpm_interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self.queue_interval_seconds = max(float(queue_interval_seconds), rpm_interval)
        self.requests_per_minute = requests_per_minute
        self.rate_limit_backoff_seconds = float(rate_limit_backoff_seconds)
        self.native_word_subtitles = bool(native_word_subtitles)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_started: float | None = None
        self._blocked_until = 0.0
        self._schedule_lock = threading.Lock()

    def _wait(self, token: object | None) -> None:
        _raise_cancelled(token)
        # The adapter instance is shared by all voice worker threads.  Keep the
        # provider start-rate decision under one lock so concurrent workers
        # cannot all observe the same stale ``_last_request_started`` value.
        with self._schedule_lock:
            now = self._monotonic()
            allowed_at = self._blocked_until
            if self._last_request_started is not None:
                allowed_at = max(
                    allowed_at,
                    self._last_request_started + self.queue_interval_seconds,
                )
            remaining = allowed_at - now
            while remaining > 0:
                _raise_cancelled(token)
                self._sleep(min(remaining, 0.25))
                remaining = allowed_at - self._monotonic()
            _raise_cancelled(token)
            self._last_request_started = self._monotonic()

    def _defer_requests(self, seconds: float) -> None:
        with self._schedule_lock:
            self._blocked_until = max(
                self._blocked_until,
                self._monotonic() + seconds,
            )

    def _payload(self, request: SynthesisRequest) -> bytes:
        if not request.text.strip():
            raise PermanentProviderError("MiniMax 朗读文本不能为空")
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
            "subtitle_enable": self.native_word_subtitles,
            **(
                {"subtitle_type": MINIMAX_SUBTITLE_TYPE}
                if self.native_word_subtitles
                else {}
            ),
            "output_format": "hex",
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _download_subtitles(self, value: Mapping[str, Any], timeout_seconds: float) -> bytes:
        data = value.get("data")
        subtitle_url = data.get("subtitle_file") if isinstance(data, Mapping) else None
        if subtitle_url is None:
            subtitle_url = value.get("subtitle_file")
        if not isinstance(subtitle_url, str) or not subtitle_url.strip():
            raise _MiniMaxSubtitleUnavailable("MiniMax 返回缺少原生字幕文件")
        parsed = urllib.parse.urlparse(subtitle_url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise _MiniMaxSubtitleUnavailable("MiniMax 原生字幕链接必须是 HTTPS")
        subtitle_request = urllib.request.Request(
            subtitle_url.strip(), method="GET", headers={"Accept": "application/json"}
        )
        try:
            with self._opener(subtitle_request, timeout=timeout_seconds) as response:
                payload = response.read(MAX_SUBTITLE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _MiniMaxSubtitleUnavailable(
                f"MiniMax 原生字幕下载失败（HTTP {exc.code}）"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise _MiniMaxSubtitleUnavailable(
                "MiniMax 原生字幕下载连接或请求超时"
            ) from exc
        if not payload or len(payload) > MAX_SUBTITLE_BYTES:
            raise _MiniMaxSubtitleUnavailable("MiniMax 原生字幕文件为空或超过 8 MiB")
        try:
            decoded = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _MiniMaxSubtitleUnavailable("MiniMax 原生字幕文件不是有效 JSON") from exc
        if not isinstance(decoded, (list, Mapping)):
            raise _MiniMaxSubtitleUnavailable("MiniMax 原生字幕 JSON 顶层结构无效")
        return payload

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
                raise _MiniMaxRetryableError(
                    f"MiniMax 请求暂时不可用（HTTP {exc.code}）",
                    rate_limited=exc.code == 429,
                    retry_after_seconds=_retry_after_seconds(exc.headers),
                ) from exc
            raise PermanentProviderError(f"MiniMax 请求被拒绝（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise _MiniMaxRetryableError("MiniMax DNS、连接或请求超时") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentProviderError("MiniMax 返回了无效 JSON") from exc
        base = value.get("base_resp") if isinstance(value, Mapping) else None
        if not isinstance(base, Mapping) or base.get("status_code") != 0:
            message = base.get("status_msg") if isinstance(base, Mapping) else None
            if _is_rate_limited(message):
                raise _MiniMaxRetryableError(
                    "MiniMax provider 明确限流",
                    rate_limited=True,
                )
            # provider 自由文本可能回显请求头、正文或账号信息，只用于内部分
            # 类，不进入异常、CLI、manifest errorSummary 或日志。
            raise PermanentProviderError("MiniMax provider 返回业务错误")
        audio = value.get("data", {}).get("audio") if isinstance(value.get("data"), Mapping) else None
        if not isinstance(audio, str) or not audio:
            raise PermanentProviderError("MiniMax 返回缺少 hex 音频")
        try:
            media = bytes.fromhex(audio)
        except ValueError as exc:
            raise PermanentProviderError("MiniMax 返回了无效 hex 音频") from exc
        if not media:
            raise PermanentProviderError("MiniMax 返回了空音频")
        subtitle_bytes = (
            self._download_subtitles(value, float(request.timeoutSeconds))
            if self.native_word_subtitles
            else None
        )
        return RawAudioResult(
            media,
            "audio/mpeg",
            sanitize_provider_request_id(value.get("trace_id")),
            subtitle_bytes,
            MINIMAX_SUBTITLE_TYPE if subtitle_bytes is not None else None,
        )

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
                if isinstance(exc, _MiniMaxRetryableError) and exc.rate_limited:
                    self._defer_requests(
                        exc.retry_after_seconds or self.rate_limit_backoff_seconds
                    )
                if attempt == self.max_attempts:
                    break
        assert last is not None
        raise RetryableProviderError(f"MiniMax 可重试失败已耗尽（{self.max_attempts} 次）: {last}") from last


__all__ = [
    "MINIMAX_ENDPOINT",
    "MINIMAX_SUBTITLE_TYPE",
    "MiniMaxAdapter",
    "sanitize_provider_request_id",
]

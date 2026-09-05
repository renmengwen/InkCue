#!/usr/bin/env python3
"""豆包 Seed Audio prompt-only adapter；原子返回音频与严格字级字幕。"""
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


DOUBAO_SUBTITLE_SCHEMA_VERSION = 1
DOUBAO_SUBTITLE_KIND = "providerNativeWordSubtitles"
DOUBAO_SUBTITLE_TYPE = "word"
DOUBAO_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/create"
DOUBAO_MAX_AUDIO_DURATION_SECONDS = 120
DOUBAO_TIMESTAMP_TOLERANCE_MS = 100
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

DOUBAO_EVIDENCE_REASON_CODES = frozenset(
    {
        "invalid_duration",
        "invalid_original_duration",
        "missing_subtitle",
        "invalid_subtitle_text",
        "empty_sentences",
        "invalid_sentence",
        "invalid_sentence_timing",
        "empty_words",
        "invalid_word",
        "invalid_word_timing",
        "sentence_words_text_mismatch",
        "subtitle_sentences_text_mismatch",
        "unclassified_native_evidence_failure",
    }
)


class DoubaoEvidenceUnavailable(PermanentProviderError):
    """响应已含可能计费的音频，但同请求原生字幕证据不完整。"""

    provider_response_received = True
    external_result_incomplete = True
    retry_allowed = False

    def __init__(self, reason_code: str) -> None:
        if reason_code not in DOUBAO_EVIDENCE_REASON_CODES:
            reason_code = "unclassified_native_evidence_failure"
        self.reason_code = reason_code
        super().__init__(
            "豆包已返回音频，但同请求原生字级字幕或时长证据无效"
            f"（reason={reason_code}）"
        )


class _DoubaoEvidenceValidationError(ValueError):
    """只携带稳定原因码，避免把 provider 响应或正文写入审计。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


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
        if model != "seed-audio-1.0":
            raise PermanentProviderError("豆包 model 必须为 seed-audio-1.0")
        if endpoint != DOUBAO_ENDPOINT:
            raise PermanentProviderError("豆包 endpoint 必须使用 Seed Audio 非流式 create 接口")
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
        payload = {
            "model": self.model,
            "text_prompt": request.text,
            "audio_config": {
                "format": "wav",
                "sample_rate": 24000,
                "speech_rate": _rate(request.normalizedRate),
                "loudness_rate": _volume(request.normalizedVolume),
                "pitch_rate": _pitch(request.normalizedPitch),
                "enable_subtitle": True,
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
        try:
            subtitle_bytes, subtitle_metadata = self._subtitle_sidecar(
                value, text_prompt=request.text
            )
        except _DoubaoEvidenceValidationError as exc:
            raise DoubaoEvidenceUnavailable(exc.reason_code) from exc
        except (TypeError, ValueError, KeyError) as exc:
            raise DoubaoEvidenceUnavailable(
                "unclassified_native_evidence_failure"
            ) from exc
        header_get = getattr(headers, "get", None)
        log_id = header_get("X-Tt-Logid") if callable(header_get) else None
        return RawAudioResult(
            media,
            "audio/wav",
            sanitize_provider_request_id(log_id),
            subtitle_bytes,
            DOUBAO_SUBTITLE_TYPE,
            subtitle_metadata,
        )

    @staticmethod
    def _duration_ms(value: object, *, reason_code: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _DoubaoEvidenceValidationError(reason_code)
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0 or seconds > DOUBAO_MAX_AUDIO_DURATION_SECONDS:
            raise _DoubaoEvidenceValidationError(reason_code)
        return round(seconds * 1000)

    @staticmethod
    def _timestamp(value: object, *, reason_code: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _DoubaoEvidenceValidationError(reason_code)
        return value

    @classmethod
    def _subtitle_sidecar(
        cls, value: Mapping[str, Any], *, text_prompt: str
    ) -> tuple[bytes, dict[str, Any]]:
        duration_ms = cls._duration_ms(
            value.get("duration"), reason_code="invalid_duration"
        )
        original_duration_ms = cls._duration_ms(
            value.get("original_duration"),
            reason_code="invalid_original_duration",
        )
        subtitle = value.get("subtitle")
        if not isinstance(subtitle, Mapping):
            raise _DoubaoEvidenceValidationError("missing_subtitle")
        subtitle_text = subtitle.get("text")
        sentences = subtitle.get("sentences")
        if not isinstance(subtitle_text, str) or not subtitle_text.strip():
            raise _DoubaoEvidenceValidationError("invalid_subtitle_text")
        if not isinstance(sentences, list) or not sentences:
            raise _DoubaoEvidenceValidationError("empty_sentences")
        normalized_sentences: list[dict[str, Any]] = []
        previous_sentence_start = -1
        previous_word_start = -1
        total_words = 0
        for sentence in sentences:
            if not isinstance(sentence, Mapping):
                raise _DoubaoEvidenceValidationError("invalid_sentence")
            start_ms = cls._timestamp(
                sentence.get("start_time"), reason_code="invalid_sentence_timing"
            )
            end_ms = cls._timestamp(
                sentence.get("end_time"), reason_code="invalid_sentence_timing"
            )
            sentence_text = sentence.get("text")
            words = sentence.get("words")
            if (
                end_ms <= start_ms
                or end_ms > duration_ms + DOUBAO_TIMESTAMP_TOLERANCE_MS
                or start_ms < previous_sentence_start
            ):
                raise _DoubaoEvidenceValidationError("invalid_sentence_timing")
            if not isinstance(sentence_text, str) or not sentence_text.strip():
                raise _DoubaoEvidenceValidationError("invalid_sentence")
            if not isinstance(words, list) or not words:
                raise _DoubaoEvidenceValidationError("empty_words")
            normalized_words: list[dict[str, Any]] = []
            sentence_word_text = ""
            for word in words:
                if not isinstance(word, Mapping):
                    raise _DoubaoEvidenceValidationError("invalid_word")
                word_start = cls._timestamp(
                    word.get("start_time"),
                    reason_code="invalid_word_timing",
                )
                word_end = cls._timestamp(
                    word.get("end_time"),
                    reason_code="invalid_word_timing",
                )
                word_text = word.get("text")
                if not isinstance(word_text, str) or not word_text.strip():
                    raise _DoubaoEvidenceValidationError("invalid_word")
                has_lexical_content = any(
                    character.isalnum() for character in word_text
                )
                if (
                    word_end < word_start
                    or word_end > duration_ms + DOUBAO_TIMESTAMP_TOLERANCE_MS
                    or word_start < previous_word_start
                    or (word_end == word_start and has_lexical_content)
                ):
                    raise _DoubaoEvidenceValidationError("invalid_word_timing")
                normalized_words.append(
                    {"start_time": word_start, "end_time": word_end, "text": word_text}
                )
                sentence_word_text += word_text
                previous_word_start = word_start
                total_words += 1
            if re.sub(r"\s+", "", sentence_word_text) != re.sub(
                r"\s+", "", sentence_text
            ):
                raise _DoubaoEvidenceValidationError(
                    "sentence_words_text_mismatch"
                )
            normalized_sentences.append(
                {
                    "start_time": start_ms,
                    "end_time": end_ms,
                    "text": sentence_text,
                    "words": normalized_words,
                }
            )
            previous_sentence_start = start_ms
        if re.sub(r"\s+", "", "".join(item["text"] for item in normalized_sentences)) != re.sub(
            r"\s+", "", subtitle_text
        ):
            raise _DoubaoEvidenceValidationError(
                "subtitle_sentences_text_mismatch"
            )
        sidecar = {
            "schemaVersion": DOUBAO_SUBTITLE_SCHEMA_VERSION,
            "kind": DOUBAO_SUBTITLE_KIND,
            "provider": "doubao",
            "model": self.model,
            "textPromptSha256": hashlib.sha256(text_prompt.encode("utf-8")).hexdigest(),
            "durationMs": duration_ms,
            "originalDurationMs": original_duration_ms,
            "subtitle": {"text": subtitle_text, "sentences": normalized_sentences},
        }
        payload = json.dumps(
            sidecar, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return payload, {
            "durationMs": duration_ms,
            "originalDurationMs": original_duration_ms,
            "textPromptSha256": sidecar["textPromptSha256"],
            "sentenceCount": len(normalized_sentences),
            "wordCount": total_words,
        }

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
    "DOUBAO_EVIDENCE_REASON_CODES",
    "DOUBAO_ENDPOINT",
    "DOUBAO_SUBTITLE_SCHEMA_VERSION",
    "DOUBAO_SUBTITLE_KIND",
    "DOUBAO_SUBTITLE_TYPE",
    "DOUBAO_TIMESTAMP_TOLERANCE_MS",
    "DoubaoAdapter",
    "DoubaoEvidenceUnavailable",
    "sanitize_provider_request_id",
]

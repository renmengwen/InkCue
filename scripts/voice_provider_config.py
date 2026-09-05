#!/usr/bin/env python3
"""读取旁白 provider 配置；本地密钥只在进程内使用，绝不进入输出。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from typing import Any, Mapping

try:
    from .voiceover import normalize_pitch, normalize_rate, normalize_volume, VoiceoverValidationError
except ImportError:  # pragma: no cover
    from voiceover import normalize_pitch, normalize_rate, normalize_volume, VoiceoverValidationError


class VoiceProviderConfigError(ValueError):
    pass


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderConfigError(f"无法读取语音 provider 配置: {exc}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise VoiceProviderConfigError("语音 provider 配置 schemaVersion 必须为 1")
    if not isinstance(value.get("providers"), dict):
        raise VoiceProviderConfigError("语音 provider 配置缺少 providers")
    return value


def _provider_key(providers: Mapping[str, Any], provider_id: str) -> str:
    wanted = provider_id.lower()
    for key in providers:
        if str(key).lower() == wanted:
            return str(key)
    raise VoiceProviderConfigError("未找到 active 语音 provider")


def load_voice_provider_config(*, provider_id: str | None = None, root: Path | None = None) -> dict[str, Any]:
    base = root or _root()
    local = base / "config" / "voice-providers.local.json"
    example = base / "config" / "voice-providers.example.json"
    path = local if local.is_file() else example
    value = _load(path)
    selected = provider_id or value.get("activeProvider")
    if not isinstance(selected, str) or not selected.strip():
        raise VoiceProviderConfigError("activeProvider 必须是非空字符串")
    key = _provider_key(value["providers"], selected)
    config = copy.deepcopy(value["providers"][key])
    if not isinstance(config, dict):
        raise VoiceProviderConfigError("active provider 配置必须是对象")
    # 旧 local 文件中的内部 contractVersion 不再参与配置或控制流。
    # 兼容性只体现在忽略该冗余字段，不迁移也不把它写入任何 artifact。
    config.pop("contractVersion", None)
    normalized = key.lower()
    config["id"] = normalized
    config["configKey"] = key
    config["configFile"] = str(path.name)
    if normalized == "minimax":
        if not isinstance(config.get("apiKey"), str) or not config["apiKey"].strip():
            raise VoiceProviderConfigError("MiniMax 缺少本地 apiKey；请在 config/voice-providers.local.json 配置")
        if config.get("protocol") != "MiniMax":
            raise VoiceProviderConfigError("MiniMax protocol 必须为 MiniMax")
        for field in ("voice", "language", "model"):
            if not isinstance(config.get(field), str) or not config[field].strip():
                raise VoiceProviderConfigError(f"MiniMax {field} 必须是非空字符串")
        try:
            config["rate"] = normalize_rate(config.get("rate", 0))
            config["pitch"] = normalize_pitch(config.get("pitch", 0))
            config["volume"] = normalize_volume(config.get("volume", 0))
        except VoiceoverValidationError as exc:
            raise VoiceProviderConfigError(str(exc)) from exc
        retries = config.get("maxRetries", 2)
        if isinstance(retries, bool) or not isinstance(retries, int) or not 1 <= retries <= 10:
            raise VoiceProviderConfigError("MiniMax maxRetries 必须位于 1–10")
        rpm = config.get("requestsPerMinute", 20)
        if isinstance(rpm, bool) or not isinstance(rpm, int) or not 1 <= rpm <= 600:
            raise VoiceProviderConfigError("MiniMax requestsPerMinute 必须位于 1–600")
        backoff_ms = config.get("rateLimitBackoffMs", 35000)
        if (
            isinstance(backoff_ms, bool)
            or not isinstance(backoff_ms, int)
            or not 1000 <= backoff_ms <= 300000
        ):
            raise VoiceProviderConfigError("MiniMax rateLimitBackoffMs 必须位于 1000–300000")
        config["requestsPerMinute"] = rpm
        config["rateLimitBackoffMs"] = backoff_ms
    elif normalized == "doubao":
        if not isinstance(config.get("apiKey"), str) or not config["apiKey"].strip():
            raise VoiceProviderConfigError(
                "豆包缺少本地 apiKey；请在 config/voice-providers.local.json 配置"
            )
        if config.get("protocol") != "Doubao":
            raise VoiceProviderConfigError("豆包 protocol 必须为 Doubao")
        for field in ("language", "model"):
            if not isinstance(config.get(field), str) or not config[field].strip():
                raise VoiceProviderConfigError(f"豆包 {field} 必须是非空字符串")
        if config["model"] != "seed-audio-1.0":
            raise VoiceProviderConfigError("豆包 model 当前只允许 seed-audio-1.0")
        if config.get("outputFormat") != "audio-24khz-mono-wav":
            raise VoiceProviderConfigError(
                "豆包 outputFormat 必须为 audio-24khz-mono-wav"
            )
        endpoint = config.get("endpoint", "https://openspeech.bytedance.com/api/v3/tts/create")
        if endpoint != "https://openspeech.bytedance.com/api/v3/tts/create":
            raise VoiceProviderConfigError("豆包 endpoint 必须为 Seed Audio 非流式 create 接口")
        try:
            config["rate"] = normalize_rate(config.get("rate", 0))
            config["pitch"] = normalize_pitch(config.get("pitch", 0))
            config["volume"] = normalize_volume(config.get("volume", 0))
        except VoiceoverValidationError as exc:
            raise VoiceProviderConfigError(str(exc)) from exc
        rate_value = int(config["rate"][:-1])
        pitch_value = int(config["pitch"][:-2])
        volume_value = int(config["volume"][:-1])
        if not -50 <= rate_value <= 100:
            raise VoiceProviderConfigError("豆包 rate 必须位于 -50–100")
        if not -12 <= pitch_value <= 12:
            raise VoiceProviderConfigError("豆包 pitch 必须位于 -12–12")
        if not -50 <= volume_value <= 100:
            raise VoiceProviderConfigError("豆包 volume 必须位于 -50–100")
        timeout_seconds = config.get("requestTimeoutSeconds", 60)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 600
        ):
            raise VoiceProviderConfigError(
                "豆包 requestTimeoutSeconds 必须位于 1–600"
            )
        retries = config.get("maxRetries", 2)
        if isinstance(retries, bool) or not isinstance(retries, int) or not 1 <= retries <= 10:
            raise VoiceProviderConfigError("豆包 maxRetries 必须位于 1–10")
        queue_interval_ms = config.get("queueIntervalMs", 500)
        if (
            isinstance(queue_interval_ms, bool)
            or not isinstance(queue_interval_ms, int)
            or not 0 <= queue_interval_ms <= 300000
        ):
            raise VoiceProviderConfigError("豆包 queueIntervalMs 必须位于 0–300000")
        config["endpoint"] = endpoint
        config["queueIntervalMs"] = queue_interval_ms
        config["requestTimeoutSeconds"] = float(timeout_seconds)
        config.pop("voice", None)
        config["voiceControlMode"] = "text_prompt"
    return config


def active_provider_id(*, root: Path | None = None) -> str:
    base = root or _root()
    local = base / "config" / "voice-providers.local.json"
    example = base / "config" / "voice-providers.example.json"
    value = _load(local if local.is_file() else example)
    selected = value.get("activeProvider")
    if not isinstance(selected, str) or not selected.strip():
        raise VoiceProviderConfigError("activeProvider 必须是非空字符串")
    normalized = selected.lower()
    if normalized not in {"edge-tts", "minimax", "doubao"}:
        raise VoiceProviderConfigError(
            "activeProvider 只允许 edge-tts、MiniMax 或 doubao"
        )
    return normalized


def voice_provider_status(*, root: Path | None = None) -> dict[str, Any]:
    """返回严格 allowlist 的 provider 状态，不暴露本地配置原文或凭据。"""

    base = root or _root()
    local = base / "config" / "voice-providers.local.json"
    example = base / "config" / "voice-providers.example.json"
    value = _load(local if local.is_file() else example)
    selected = value.get("activeProvider")
    if not isinstance(selected, str) or not selected.strip():
        raise VoiceProviderConfigError("activeProvider 必须是非空字符串")
    key = _provider_key(value["providers"], selected)
    config = value["providers"][key]
    if not isinstance(config, Mapping):
        raise VoiceProviderConfigError("active provider 配置必须是对象")
    provider = key.lower()
    if provider not in {"edge-tts", "minimax", "doubao"}:
        raise VoiceProviderConfigError(
            "activeProvider 只允许 edge-tts、MiniMax 或 doubao"
        )
    voice = "text-prompt-authored" if provider == "doubao" else config.get("voice")
    model = config.get("model")
    if not isinstance(voice, str) or not voice.strip():
        raise VoiceProviderConfigError("active provider voice 必须是非空字符串")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise VoiceProviderConfigError("active provider model 必须是非空字符串或 null")
    try:
        rate = normalize_rate(config.get("rate", 0))
    except VoiceoverValidationError as exc:
        raise VoiceProviderConfigError(str(exc)) from exc
    credentials_configured = provider == "edge-tts" or (
        isinstance(config.get("apiKey"), str) and bool(config["apiKey"].strip())
    )
    result = {
        "provider": provider,
        "model": model,
        "voice": voice,
        "rate": rate,
        "credentialsConfigured": credentials_configured,
    }
    if provider == "doubao":
        result["voiceControlMode"] = "text_prompt"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读输出脱敏语音 provider 状态")
    parser.add_argument("command", choices=("status",))
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(voice_provider_status(), ensure_ascii=False, sort_keys=True))
        return 0
    except VoiceProviderConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2


__all__ = [
    "VoiceProviderConfigError",
    "active_provider_id",
    "load_voice_provider_config",
    "voice_provider_status",
]


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""读取旁白 provider 配置；本地密钥只在进程内使用，绝不进入输出。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
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
    raise VoiceProviderConfigError(f"未找到语音 provider: {provider_id}")


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
        raise VoiceProviderConfigError(f"provider {selected} 配置必须是对象")
    normalized = "minimax" if key.lower() == "minimax" else key.lower()
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
    return config


def active_provider_id(*, root: Path | None = None) -> str:
    base = root or _root()
    local = base / "config" / "voice-providers.local.json"
    example = base / "config" / "voice-providers.example.json"
    value = _load(local if local.is_file() else example)
    selected = value.get("activeProvider")
    if not isinstance(selected, str) or not selected.strip():
        raise VoiceProviderConfigError("activeProvider 必须是非空字符串")
    normalized = "minimax" if selected.lower() == "minimax" else selected.lower()
    if normalized not in {"edge-tts", "minimax"}:
        raise VoiceProviderConfigError(
            "activeProvider 只允许 edge-tts 或 MiniMax"
        )
    return normalized


__all__ = ["VoiceProviderConfigError", "active_provider_id", "load_voice_provider_config"]

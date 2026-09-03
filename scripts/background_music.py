#!/usr/bin/env python3
"""读取 project.json 的单一 backgroundMusic.enabled 字段。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .project_workspace import Project, sha256_file
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import Project, sha256_file


BGM_ASSET_FILE = "assets/bgm/first-light-particles.mp3"
BGM_ASSET_SHA256 = "be5bd64f2d5f2f73a63bdec3afa4e1123b275ca9c1b77e2bb830c065d92b9724"
BGM_MIX_CONTRACT_VERSION = "background-music-mix-v1"
FIXED_ASSET_RENDER_MODE = "fixed_asset_mix"
PROVIDER_EMBEDDED_RENDER_MODE = "provider_embedded"
DOUBAO_PROVIDER_CONTRACT_VERSION = "doubao-seed-audio-expressive-native-word-v2"
DEFAULT_GAIN_DB = -15.0
DEFAULT_FADE_IN_MS = 1200
DEFAULT_FADE_OUT_MS = 1800

BGM_ASSET_PATH = Path(__file__).resolve().parent.parent / BGM_ASSET_FILE


class BackgroundMusicError(ValueError):
    """backgroundMusic 字段或曲目文件无效。"""


def load_background_music_plan(project: Project) -> dict[str, Any]:
    """把单一开关展开为最终封装所需的实际 render mode。"""
    if not project.background_music_enabled:
        return {"enabled": False}
    if project.voiceover_mode == "doubao":
        return {
            "enabled": True,
            "renderMode": PROVIDER_EMBEDDED_RENDER_MODE,
            "provider": "doubao",
            "providerContractVersion": DOUBAO_PROVIDER_CONTRACT_VERSION,
        }
    if not BGM_ASSET_PATH.is_file() or sha256_file(BGM_ASSET_PATH) != BGM_ASSET_SHA256:
        raise BackgroundMusicError("skill 内置 BGM 文件缺失或 SHA-256 不一致")
    return {
        "enabled": True,
        "renderMode": FIXED_ASSET_RENDER_MODE,
        "asset": BGM_ASSET_FILE,
        "assetSha256": BGM_ASSET_SHA256,
        "title": "First Light Particles",
        "artist": "Yoiyami",
        "license": "CC0-1.0",
        "gainDb": DEFAULT_GAIN_DB,
        "fadeInMs": DEFAULT_FADE_IN_MS,
        "fadeOutMs": DEFAULT_FADE_OUT_MS,
        "loop": True,
    }


__all__ = [
    "BGM_ASSET_SHA256",
    "BGM_ASSET_FILE",
    "BGM_ASSET_PATH",
    "BGM_MIX_CONTRACT_VERSION",
    "DOUBAO_PROVIDER_CONTRACT_VERSION",
    "FIXED_ASSET_RENDER_MODE",
    "PROVIDER_EMBEDDED_RENDER_MODE",
    "BackgroundMusicError",
    "DEFAULT_GAIN_DB",
    "load_background_music_plan",
]

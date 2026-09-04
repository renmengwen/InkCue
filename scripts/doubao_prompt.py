#!/usr/bin/env python3
"""豆包 Seed Audio 单人白板旁白的 authored performance brief 合同。"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence


DOUBAO_PROMPT_SPEC_VERSION = "doubao-whiteboard-authored-performance-v3"
DOUBAO_PERFORMANCE_BRIEF_VERSION = "doubao-performance-brief-v1"
DOUBAO_TEXT_PROMPT_MAX_CHARACTERS = 3000
DOUBAO_MAX_AUDIO_DURATION_SECONDS = 120
DOUBAO_SAMPLE_MAX_AUDIO_DURATION_SECONDS = 30

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_FIELDS = {
    "contractVersion",
    "referenceSha256",
    "narratorDirection",
    "music",
    "passages",
}
_MUSIC_FIELDS = {
    "enabledOpeningDirection",
    "enabledEndingDirection",
}
_PASSAGE_FIELDS = {"sceneId", "voiceDirection", "enabledMusicBefore"}


class DoubaoPromptError(ValueError):
    """performance brief、原稿分段或 Seed Audio 硬限制无效。"""


def _direction(
    value: object,
    *,
    label: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DoubaoPromptError(f"{label} 必须是字符串")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise DoubaoPromptError(f"{label} 不能为空")
    if "\n" in normalized or "\r" in normalized:
        raise DoubaoPromptError(f"{label} 必须是单段自然语言")
    if "「" in normalized or "」" in normalized:
        raise DoubaoPromptError(f"{label} 不得包含正文边界符号「」")
    return normalized


def _scene_sources(
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not cues or not scenes:
        raise DoubaoPromptError("豆包 performance brief 需要非空 cue 与 scene")
    cue_text: dict[int, str] = {}
    for cue in cues:
        ordinal = cue.get("sourceOrdinal")
        text = cue.get("text")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise DoubaoPromptError("豆包 prompt cue sourceOrdinal 无效")
        if not isinstance(text, str) or not text.strip():
            raise DoubaoPromptError("豆包 prompt cue 文本不能为空")
        cue_text[ordinal] = text

    sources: list[dict[str, Any]] = []
    for scene in scenes:
        scene_id = scene.get("sceneId")
        cue_range = scene.get("sourceCueRange")
        if (
            not isinstance(scene_id, str)
            or not scene_id
            or not isinstance(cue_range, Sequence)
            or isinstance(cue_range, (str, bytes))
            or len(cue_range) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in cue_range
            )
        ):
            raise DoubaoPromptError("豆包 prompt scene 结构无效")
        first, last = int(cue_range[0]), int(cue_range[1])
        if first > last or any(
            ordinal not in cue_text for ordinal in range(first, last + 1)
        ):
            raise DoubaoPromptError("豆包 prompt scene cueRange 未完整覆盖原稿")
        sources.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [first, last],
            }
        )
    return sources


def build_doubao_prompt_spec(
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
    *,
    performance_brief: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把 coordinator authored brief 绑定到 current scene，不复制正文。"""

    sources = _scene_sources(cues, scenes)
    if not isinstance(performance_brief, Mapping):
        raise DoubaoPromptError(
            "豆包旁白需要 coordinator 参考 current Seed Audio 示例生成 performance brief"
        )
    brief = dict(performance_brief)
    if set(brief) != _TOP_LEVEL_FIELDS:
        raise DoubaoPromptError(
            "豆包 performance brief 顶层字段必须严格匹配 current 合同"
        )
    if brief.get("contractVersion") != DOUBAO_PERFORMANCE_BRIEF_VERSION:
        raise DoubaoPromptError("豆包 performance brief contractVersion 不匹配")
    reference_sha = brief.get("referenceSha256")
    if not isinstance(reference_sha, str) or not _SHA256_RE.fullmatch(reference_sha):
        raise DoubaoPromptError("豆包 performance brief referenceSha256 无效")

    music = brief.get("music")
    if not isinstance(music, Mapping) or set(music) != _MUSIC_FIELDS:
        raise DoubaoPromptError("豆包 performance brief.music 字段无效")
    normalized_music = {
        field: _direction(music.get(field), label=f"performance brief.music.{field}")
        for field in sorted(_MUSIC_FIELDS)
    }

    passages = brief.get("passages")
    if not isinstance(passages, list) or len(passages) != len(sources):
        raise DoubaoPromptError(
            "豆包 performance brief.passages 必须与 current scene 一一对应"
        )
    normalized_passages: list[dict[str, Any]] = []
    for index, (passage, source) in enumerate(zip(passages, sources), start=1):
        if not isinstance(passage, Mapping) or set(passage) != _PASSAGE_FIELDS:
            raise DoubaoPromptError(
                f"豆包 performance brief passage[{index}] 字段无效"
            )
        if passage.get("sceneId") != source["sceneId"]:
            raise DoubaoPromptError(
                f"豆包 performance brief passage[{index}] 未绑定 current scene"
            )
        normalized_passages.append(
            {
                **source,
                "voiceDirection": _direction(
                    passage.get("voiceDirection"),
                    label=f"performance brief passage[{index}].voiceDirection",
                ),
                "enabledMusicBefore": _direction(
                    passage.get("enabledMusicBefore"),
                    label=f"performance brief passage[{index}].enabledMusicBefore",
                    allow_empty=True,
                ),
            }
        )

    return {
        "contractVersion": DOUBAO_PROMPT_SPEC_VERSION,
        "referenceSha256": reference_sha,
        "narratorDirection": _direction(
            brief.get("narratorDirection"),
            label="performance brief.narratorDirection",
        ),
        "music": normalized_music,
        "passages": normalized_passages,
    }


def validate_doubao_prompt_spec(
    value: Mapping[str, Any],
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """重验已冻结的 prompt spec 与 current scene mapping。"""

    if not isinstance(value, Mapping):
        raise DoubaoPromptError("豆包 promptSpec 必须是对象")
    raw = dict(value)
    raw_passages = raw.get("passages")
    brief_passages = None
    if isinstance(raw_passages, list):
        brief_passages = []
        for passage in raw_passages:
            if not isinstance(passage, Mapping):
                raise DoubaoPromptError("豆包 promptSpec.passages 必须是对象列表")
            brief_passages.append(
                {
                    "sceneId": passage.get("sceneId"),
                    "voiceDirection": passage.get("voiceDirection"),
                    "enabledMusicBefore": passage.get("enabledMusicBefore"),
                }
            )
    brief = {
        "contractVersion": DOUBAO_PERFORMANCE_BRIEF_VERSION,
        "referenceSha256": raw.get("referenceSha256"),
        "narratorDirection": raw.get("narratorDirection"),
        "music": raw.get("music"),
        "passages": brief_passages,
    }
    rebuilt = build_doubao_prompt_spec(
        cues,
        scenes,
        performance_brief=brief,
    )
    if raw != rebuilt:
        raise DoubaoPromptError("豆包 promptSpec 与 current brief/source/scenes 不一致")
    return rebuilt


def render_doubao_text_prompt(
    prompt_spec: Mapping[str, Any],
    speech_text: str,
    *,
    background_music_enabled: bool,
    sample: bool = False,
    target_duration_seconds: float | None = None,
) -> str:
    """渲染唯一请求 prompt；创意来自 brief，正文由程序逐字装配。"""

    if prompt_spec.get("contractVersion") != DOUBAO_PROMPT_SPEC_VERSION:
        raise DoubaoPromptError("豆包 promptSpec 合同版本不匹配")
    if not isinstance(speech_text, str) or not speech_text.strip():
        raise DoubaoPromptError("豆包 text_prompt 的已确认原稿不能为空")
    if not isinstance(background_music_enabled, bool):
        raise DoubaoPromptError("豆包 text_prompt BGM 开关必须是布尔值")
    if sample and background_music_enabled:
        raise DoubaoPromptError("豆包样音固定不生成 BGM")
    if target_duration_seconds is not None and (
        isinstance(target_duration_seconds, bool)
        or not isinstance(target_duration_seconds, (int, float))
        or target_duration_seconds <= 0
        or target_duration_seconds > DOUBAO_MAX_AUDIO_DURATION_SECONDS
    ):
        raise DoubaoPromptError("豆包单次音频目标时长必须位于 0–120 秒")

    narrator = _direction(
        prompt_spec.get("narratorDirection"), label="promptSpec.narratorDirection"
    )
    music = prompt_spec.get("music")
    if not isinstance(music, Mapping):
        raise DoubaoPromptError("豆包 promptSpec.music 必须是对象")
    lines = [narrator]
    if sample:
        lines.extend(
            [
                (
                    "样音只由这位旁白朗读「」内原稿，不增删改写，不朗读引号外说明；"
                    "不生成音乐、环境音、拟音或额外人声。"
                ),
                f"旁白自然朗读：「{speech_text}」",
            ]
        )
    else:
        if background_music_enabled:
            lines.append(
                _direction(
                    music.get("enabledOpeningDirection"),
                    label="promptSpec.music.enabledOpeningDirection",
                )
            )
            lines.append(
                "只由这位旁白朗读「」内原稿，不增删改写，不朗读引号外说明；"
                "音乐始终低于人声，不生成环境音、拟音或额外人声。"
            )
        else:
            lines.append(
                "只由这位旁白朗读「」内原稿，不增删改写，不朗读引号外说明；"
                "不生成音乐、环境音、拟音或额外人声。"
            )
        passages = prompt_spec.get("passages")
        scene_texts = speech_text.split("\n\n")
        if not isinstance(passages, list) or len(scene_texts) != len(passages):
            raise DoubaoPromptError("豆包整轨原稿段落数与 authored passages 不一致")
        for index, (scene_text, passage) in enumerate(
            zip(scene_texts, passages), start=1
        ):
            if not isinstance(passage, Mapping):
                raise DoubaoPromptError(f"豆包 passage[{index}] 结构无效")
            if background_music_enabled:
                transition = _direction(
                    passage.get("enabledMusicBefore"),
                    label=f"promptSpec passage[{index}].enabledMusicBefore",
                    allow_empty=True,
                )
                if transition:
                    lines.append(transition)
            voice_direction = _direction(
                passage.get("voiceDirection"),
                label=f"promptSpec passage[{index}].voiceDirection",
            )
            lines.append(f"{voice_direction}：「{scene_text}」")
        if background_music_enabled:
            lines.append(
                _direction(
                    music.get("enabledEndingDirection"),
                    label="promptSpec.music.enabledEndingDirection",
                )
            )
    prompt = "\n\n".join(lines)
    if len(prompt) > DOUBAO_TEXT_PROMPT_MAX_CHARACTERS:
        raise DoubaoPromptError(
            "豆包完整 text_prompt 超过 3000 字符；请精简 performance brief，"
            "禁止截断、拆句、退回裸文本或自动换 provider"
        )
    return prompt


def text_prompt_sha256(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise DoubaoPromptError("豆包 text_prompt 不能为空")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


__all__ = [
    "DOUBAO_MAX_AUDIO_DURATION_SECONDS",
    "DOUBAO_PERFORMANCE_BRIEF_VERSION",
    "DOUBAO_PROMPT_SPEC_VERSION",
    "DOUBAO_SAMPLE_MAX_AUDIO_DURATION_SECONDS",
    "DOUBAO_TEXT_PROMPT_MAX_CHARACTERS",
    "DoubaoPromptError",
    "build_doubao_prompt_spec",
    "render_doubao_text_prompt",
    "text_prompt_sha256",
    "validate_doubao_prompt_spec",
]

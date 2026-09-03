#!/usr/bin/env python3
"""豆包 Seed Audio 单人白板知识讲解导演式 prompt 的确定性合同。"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence


DOUBAO_PROMPT_SPEC_VERSION = "doubao-whiteboard-single-narrator-director-v2"
DOUBAO_TEXT_PROMPT_MAX_CHARACTERS = 3000
DOUBAO_MAX_AUDIO_DURATION_SECONDS = 120
DOUBAO_SAMPLE_MAX_AUDIO_DURATION_SECONDS = 30


class DoubaoPromptError(ValueError):
    """prompt spec、原稿分段或 Seed Audio 硬限制无效。"""


def _scene_direction(index: int, count: int, text: str) -> str:
    if count == 1:
        position = "自然开场并完整收束"
    elif index == 0:
        position = "自然开场，先建立清晰主题"
    elif index == count - 1:
        position = "承接前文后稳妥收束，结尾留短暂停顿"
    else:
        position = "承接上一段，转折处稍作停顿后继续推进"
    if any(mark in text for mark in "？！?!"):
        emotion = "疑问处轻微抬起语调，重点句适度加重，但保持克制"
    elif any(mark in text for mark in "：；:;"):
        emotion = "解释关系清晰，列举与结论之间保留自然停顿"
    else:
        emotion = "语气温和笃定，重点词轻微加重，不使用播报腔"
    return f"{position}；{emotion}；严格依照原稿标点控制停顿"


def build_doubao_prompt_spec(
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """只冻结导演模板和 scene 方向，不复制 source 正文。"""

    if not cues or not scenes:
        raise DoubaoPromptError("豆包导演式 prompt 需要非空 cue 与 scene")
    cue_text: dict[int, str] = {}
    for cue in cues:
        ordinal = cue.get("sourceOrdinal")
        text = cue.get("text")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise DoubaoPromptError("豆包 prompt cue sourceOrdinal 无效")
        if not isinstance(text, str) or not text.strip():
            raise DoubaoPromptError("豆包 prompt cue 文本不能为空")
        cue_text[ordinal] = text
    directions: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        scene_id = scene.get("sceneId")
        cue_range = scene.get("sourceCueRange")
        if (
            not isinstance(scene_id, str)
            or not scene_id
            or not isinstance(cue_range, Sequence)
            or isinstance(cue_range, (str, bytes))
            or len(cue_range) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in cue_range)
        ):
            raise DoubaoPromptError("豆包 prompt scene 结构无效")
        first, last = int(cue_range[0]), int(cue_range[1])
        if first > last or any(ordinal not in cue_text for ordinal in range(first, last + 1)):
            raise DoubaoPromptError("豆包 prompt scene cueRange 未完整覆盖原稿")
        source_text = "".join(cue_text[ordinal] for ordinal in range(first, last + 1))
        directions.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [first, last],
                "direction": _scene_direction(index, len(scenes), source_text),
            }
        )
    return {
        "contractVersion": DOUBAO_PROMPT_SPEC_VERSION,
        "roleDirection": (
            "单一中文白板知识讲解旁白；成年、自然清晰、音色温和可信，"
            "标准普通话，像面对一位听众耐心解释"
        ),
        "performanceDirection": (
            "表达自然克制，避免新闻播报腔、广告腔和夸张戏剧腔；语速舒展，"
            "按标点自然换气，重点词只轻微加重，段落间保留真实短停顿"
        ),
        "contentPolicy": (
            "只能朗读中文引号内的已确认原稿，必须逐字保留，不得擅自增删、改写、"
            "复述、解释或补充；不得朗读引号外的导演说明；全程只有这一位旁白，"
            "不得生成第二人声、和声、对白或口头提示"
        ),
        "backgroundMusic": {
            "selectionSource": "project.json#backgroundMusic.enabled",
            "enabledDirection": (
                "生成克制、低于人声、无歌词的器乐背景音乐，以柔和钢琴、轻微木吉他"
                "或极薄的氛围铺底为主，节奏平稳，不抢重点；开头自然淡入，结尾自然淡出；"
                "除这层器乐外不得生成环境音、影视拟音或任何额外人声"
            ),
            "disabledDirection": (
                "只生成人声；明确禁止背景音乐、环境音、拟音、转场音效和任何额外人声"
            ),
        },
        "sceneDirections": directions,
    }


def _validate_prompt_spec(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if value.get("contractVersion") != DOUBAO_PROMPT_SPEC_VERSION:
        raise DoubaoPromptError("豆包 promptSpec 合同版本不匹配")
    for field in ("roleDirection", "performanceDirection", "contentPolicy"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise DoubaoPromptError(f"豆包 promptSpec.{field} 不能为空")
    music = value.get("backgroundMusic")
    if not isinstance(music, Mapping):
        raise DoubaoPromptError("豆包 promptSpec.backgroundMusic 必须是对象")
    if music.get("selectionSource") != "project.json#backgroundMusic.enabled":
        raise DoubaoPromptError("豆包 promptSpec BGM 选择来源无效")
    for field in ("enabledDirection", "disabledDirection"):
        if not isinstance(music.get(field), str) or not music[field].strip():
            raise DoubaoPromptError(f"豆包 promptSpec.backgroundMusic.{field} 不能为空")
    directions = value.get("sceneDirections")
    if not isinstance(directions, list) or not directions:
        raise DoubaoPromptError("豆包 promptSpec.sceneDirections 不能为空")
    return value


def render_doubao_text_prompt(
    prompt_spec: Mapping[str, Any],
    speech_text: str,
    *,
    background_music_enabled: bool,
    sample: bool = False,
    target_duration_seconds: float | None = None,
) -> str:
    """渲染唯一请求 prompt；原稿只出现在中文引号内。"""

    spec = _validate_prompt_spec(prompt_spec)
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

    music = spec["backgroundMusic"]
    lines = [
        f"角色与音色：{spec['roleDirection']}。",
        f"整体人声方向：{spec['performanceDirection']}。",
        f"内容硬约束：{spec['contentPolicy']}。",
        "配乐与声场："
        + str(
            music["enabledDirection"]
            if background_music_enabled
            else music["disabledDirection"]
        )
        + "。",
    ]
    if target_duration_seconds is not None:
        lines.append(
            f"时长控制：整轨目标约 {target_duration_seconds:.3f} 秒，且绝不得超过 120 秒。"
        )
    if sample:
        lines.extend(
            [
                "样音表演：只验证同一旁白的人声、语气、停顿和重音方向；不生成配乐。",
                f"只朗读以下已确认原稿：“{speech_text}”",
            ]
        )
    else:
        scene_texts = speech_text.split("\n\n")
        directions = spec["sceneDirections"]
        if len(scene_texts) != len(directions):
            raise DoubaoPromptError("豆包整轨原稿段落数与 sceneDirections 不一致")
        for index, (scene_text, direction) in enumerate(zip(scene_texts, directions), start=1):
            if not isinstance(direction, Mapping) or not isinstance(
                direction.get("direction"), str
            ):
                raise DoubaoPromptError("豆包 sceneDirection 结构无效")
            lines.append(
                f"第 {index} 段导演说明：{direction['direction']}。"
                f"只朗读以下已确认原稿：“{scene_text}”"
            )
    prompt = "\n".join(lines)
    if len(prompt) > DOUBAO_TEXT_PROMPT_MAX_CHARACTERS:
        raise DoubaoPromptError(
            "豆包完整 text_prompt 超过 3000 字符；禁止截断、拆句、退回裸文本或自动换 provider"
        )
    return prompt


def text_prompt_sha256(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise DoubaoPromptError("豆包 text_prompt 不能为空")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


__all__ = [
    "DOUBAO_MAX_AUDIO_DURATION_SECONDS",
    "DOUBAO_PROMPT_SPEC_VERSION",
    "DOUBAO_SAMPLE_MAX_AUDIO_DURATION_SECONDS",
    "DOUBAO_TEXT_PROMPT_MAX_CHARACTERS",
    "DoubaoPromptError",
    "build_doubao_prompt_spec",
    "render_doubao_text_prompt",
    "text_prompt_sha256",
]

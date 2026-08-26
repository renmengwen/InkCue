#!/usr/bin/env python3
"""阶段 0 联合批准选项的确定性枚举与严格解析。

本模块只处理用户可见选项和结构化选择，不读取项目、不写批准，也不判断
宿主能力。coordinator 必须把当前真实能力作为参数传入，并由项目层在原子
批准前重新验证 content/sample identity 与能力条件。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


OPTIONS_CONTRACT_VERSION = "whiteboard-initial-approval-options-v1"
CHOICE_CONTRACT_VERSION = "whiteboard-initial-approval-choice-v1"

_VOICEOVER_MODES = {"edge-tts", "minimax", "doubao", "disabled"}
_IMAGE_GENERATION_MODES = {"provider", "gpt-login"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMBER_RE = re.compile(r"^[0-9]+$")

_IMAGE_TEXT = {
    "gpt-login": "使用当前登录的 GPT 账号生成图片",
    "provider": "使用已配置图片供应商生成图片",
}


class InitialApprovalOptionError(ValueError):
    """阶段 0 选项上下文或用户回复不满足严格合同。"""


def _choice(
    *,
    number: int,
    choice_id: str,
    action: str,
    text: str,
    content_approved: bool,
    sample_approved: bool,
    sample_required: bool,
    background_music_enabled: bool | None = None,
    agent_approval_enabled: bool | None = None,
    image_generation_mode: str | None = None,
    requires_feedback: bool = False,
) -> dict[str, Any]:
    return {
        "contractVersion": OPTIONS_CONTRACT_VERSION,
        "number": number,
        "choiceId": choice_id,
        "action": action,
        "contentApproved": content_approved,
        "sampleApproved": sample_approved,
        "sampleApprovalRequired": sample_required,
        "backgroundMusicEnabled": background_music_enabled,
        "agentApprovalEnabled": agent_approval_enabled,
        "imageGenerationMode": image_generation_mode,
        "text": text,
        "requiresFeedback": requires_feedback,
    }


def _available_image_modes(
    *,
    gpt_login_image_generation_available: bool,
    configured_image_provider_available: bool,
    fixed_image_generation_mode: str | None,
) -> tuple[tuple[str, ...], bool]:
    if not isinstance(gpt_login_image_generation_available, bool):
        raise InitialApprovalOptionError(
            "gpt_login_image_generation_available 必须是布尔值"
        )
    if not isinstance(configured_image_provider_available, bool):
        raise InitialApprovalOptionError(
            "configured_image_provider_available 必须是布尔值"
        )

    capability = {
        "gpt-login": gpt_login_image_generation_available,
        "provider": configured_image_provider_available,
    }
    if fixed_image_generation_mode is not None:
        if fixed_image_generation_mode not in _IMAGE_GENERATION_MODES:
            raise InitialApprovalOptionError("fixed_image_generation_mode 非法")
        if not capability[fixed_image_generation_mode]:
            raise InitialApprovalOptionError("固定生图方式当前不可用")
        return (fixed_image_generation_mode,), False

    available = tuple(
        mode for mode in ("gpt-login", "provider") if capability[mode]
    )
    return available, len(available) == 2


def build_initial_approval_options(
    *,
    voiceover_mode: str,
    gpt_login_image_generation_available: bool,
    configured_image_provider_available: bool,
    fixed_image_generation_mode: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """只枚举当前能力下合法、可完整复制的阶段 0 选项。

    当两种生图能力同时可用且没有提前固定时，生图方式进入每条通过句；
    其他情况下只冻结唯一可用/已固定方式，不把它重复作为选择。若当前没有
    任何合法生图方式，则不生成通过项，只保留返工项。
    """

    if voiceover_mode not in _VOICEOVER_MODES:
        raise InitialApprovalOptionError("voiceover_mode 非法")
    image_modes, image_mode_selectable = _available_image_modes(
        gpt_login_image_generation_available=gpt_login_image_generation_available,
        configured_image_provider_available=configured_image_provider_available,
        fixed_image_generation_mode=fixed_image_generation_mode,
    )

    options: list[dict[str, Any]] = []
    approval_number = 1
    if voiceover_mode == "disabled":
        # 传统静音 SRT 不生成或批准样音，BGM 也必须保持关闭，避免改变静音交付。
        for agent_enabled in (True, False):
            for image_mode in image_modes:
                clauses = ["字幕与分镜方案通过", "不使用 BGM"]
                if image_mode_selectable:
                    clauses.append(_IMAGE_TEXT[image_mode])
                clauses.append(
                    "后续由 AI 自主推进至成片"
                    if agent_enabled
                    else "后续由我逐阶段确认"
                )
                options.append(
                    _choice(
                        number=approval_number,
                        choice_id=(
                            f"approve-silent-{'agent' if agent_enabled else 'manual'}-"
                            f"{image_mode}"
                        ),
                        action="approve",
                        text="，".join(clauses) + "。",
                        content_approved=True,
                        sample_approved=False,
                        sample_required=False,
                        background_music_enabled=False,
                        agent_approval_enabled=agent_enabled,
                        image_generation_mode=image_mode,
                    )
                )
                approval_number += 1
        options.append(
            _choice(
                number=approval_number,
                choice_id="revise-silent-plan",
                action="revise_content",
                text="字幕与分镜方案需要修改。修改意见：……",
                content_approved=False,
                sample_approved=False,
                sample_required=False,
                requires_feedback=True,
            )
        )
        return tuple(options)

    # 固定生图方式时，这四句须保持产品合同指定的逐字文案与顺序。
    approval_axes = (
        (True, True),
        (False, True),
        (True, False),
        (False, False),
    )
    for background_music_enabled, agent_enabled in approval_axes:
        for image_mode in image_modes:
            clauses = [
                "草案和样音通过",
                "使用 BGM" if background_music_enabled else "不使用 BGM",
            ]
            if image_mode_selectable:
                clauses.append(_IMAGE_TEXT[image_mode])
            clauses.append(
                "后续由 AI 自主推进至成片"
                if agent_enabled
                else "后续由我逐阶段确认"
            )
            options.append(
                _choice(
                    number=approval_number,
                    choice_id=(
                        f"approve-{'bgm' if background_music_enabled else 'no-bgm'}-"
                        f"{'agent' if agent_enabled else 'manual'}-{image_mode}"
                    ),
                    action="approve",
                    text="，".join(clauses) + "。",
                    content_approved=True,
                    sample_approved=True,
                    sample_required=True,
                    background_music_enabled=background_music_enabled,
                    agent_approval_enabled=agent_enabled,
                    image_generation_mode=image_mode,
                )
            )
            approval_number += 1

    revision_specs = (
        (
            "revise-content",
            "revise_content",
            "草案需要修改，当前样音暂不批准。修改意见：……",
            False,
            False,
        ),
        (
            "revise-sample",
            "revise_sample",
            "草案通过，样音需要调整，其他方案保持不变。调整意见：……",
            True,
            False,
        ),
        (
            "revise-content-and-sample",
            "revise_both",
            "草案和样音都需要修改。修改意见：……",
            False,
            False,
        ),
    )
    for choice_id, action, text, content_approved, sample_approved in revision_specs:
        options.append(
            _choice(
                number=approval_number,
                choice_id=choice_id,
                action=action,
                text=text,
                content_approved=content_approved,
                sample_approved=sample_approved,
                sample_required=True,
                requires_feedback=True,
            )
        )
        approval_number += 1
    return tuple(options)


def _validate_identity(value: str | None, *, label: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise InitialApprovalOptionError(f"{label} 缺失")
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InitialApprovalOptionError(f"{label} 必须是小写 64 位 SHA-256")
    if not required:
        raise InitialApprovalOptionError(f"{label} 在当前静音路径中必须为空")
    return value


def _validated_options(options: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(options, Sequence) or isinstance(options, (str, bytes)) or not options:
        raise InitialApprovalOptionError("options 必须是非空序列")
    seen_numbers: set[int] = set()
    seen_texts: set[str] = set()
    seen_ids: set[str] = set()
    validated: list[Mapping[str, Any]] = []
    required = {
        "contractVersion",
        "number",
        "choiceId",
        "action",
        "contentApproved",
        "sampleApproved",
        "sampleApprovalRequired",
        "backgroundMusicEnabled",
        "agentApprovalEnabled",
        "imageGenerationMode",
        "text",
        "requiresFeedback",
    }
    for option in options:
        if not isinstance(option, Mapping) or set(option) != required:
            raise InitialApprovalOptionError("option 字段不符合 allowlist")
        if option["contractVersion"] != OPTIONS_CONTRACT_VERSION:
            raise InitialApprovalOptionError("option contractVersion 不匹配")
        number = option["number"]
        text = option["text"]
        choice_id = option["choiceId"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise InitialApprovalOptionError("option number 非法")
        if not isinstance(text, str) or not text:
            raise InitialApprovalOptionError("option text 非法")
        if not isinstance(choice_id, str) or not choice_id:
            raise InitialApprovalOptionError("option choiceId 非法")
        if number in seen_numbers or text in seen_texts or choice_id in seen_ids:
            raise InitialApprovalOptionError("option 编号、句子或 choiceId 重复")
        seen_numbers.add(number)
        seen_texts.add(text)
        seen_ids.add(choice_id)
        validated.append(option)
    if seen_numbers != set(range(1, len(validated) + 1)):
        raise InitialApprovalOptionError("option 编号必须从 1 连续")
    return tuple(validated)


def _match_response(
    response: str,
    options: tuple[Mapping[str, Any], ...],
) -> tuple[Mapping[str, Any], str | None, str]:
    if not isinstance(response, str) or not response.strip():
        raise InitialApprovalOptionError("回复不能为空")
    answer = response.strip()
    if _NUMBER_RE.fullmatch(answer):
        number = int(answer)
        for option in options:
            if option["number"] == number:
                return option, None, "number"
        raise InitialApprovalOptionError("回复编号不在当前选项中")

    for option in options:
        text = str(option["text"])
        if answer == text:
            return option, None, "full_sentence"
        if bool(option["requiresFeedback"]) and text.endswith("……"):
            prefix = text[:-2]
            if answer.startswith(prefix):
                feedback = answer[len(prefix) :].strip()
                if feedback and feedback != "……":
                    return option, feedback, "revision_sentence"
    raise InitialApprovalOptionError("回复必须是当前完整选项、编号或规定的修改句式")


def parse_initial_approval_response(
    response: str,
    *,
    options: Sequence[Mapping[str, Any]],
    content_identity_sha256: str,
    sample_identity_sha256: str | None = None,
) -> dict[str, Any]:
    """把当前选项的完整句/编号解析为带 current identity 的结构化 choice。

    该函数不把近义句、关键词或自由文本猜成批准。修改选项允许且只允许把
    展示句末的省略号替换为非空意见。
    """

    validated_options = _validated_options(options)
    option, feedback, matched_by = _match_response(response, validated_options)
    content_identity = _validate_identity(
        content_identity_sha256,
        label="content_identity_sha256",
        required=True,
    )
    sample_required = bool(option["sampleApprovalRequired"])
    sample_identity = _validate_identity(
        sample_identity_sha256,
        label="sample_identity_sha256",
        required=sample_required,
    )
    requires_feedback = bool(option["requiresFeedback"])
    return {
        "contractVersion": CHOICE_CONTRACT_VERSION,
        "choiceId": option["choiceId"],
        "optionNumber": option["number"],
        "action": option["action"],
        "contentApproved": option["contentApproved"],
        "sampleApproved": option["sampleApproved"],
        "sampleApprovalRequired": sample_required,
        "backgroundMusicEnabled": option["backgroundMusicEnabled"],
        "agentApprovalEnabled": option["agentApprovalEnabled"],
        "imageGenerationMode": option["imageGenerationMode"],
        "contentIdentitySha256": content_identity,
        "sampleIdentitySha256": sample_identity,
        "revisionInstructions": feedback,
        "requiresFeedback": requires_feedback,
        "readyForAtomicApproval": option["action"] == "approve",
        "matchedBy": matched_by,
        "selectedText": option["text"],
    }


def render_numbered_options(options: Sequence[Mapping[str, Any]]) -> str:
    """把已生成选项渲染成稳定的 Markdown 编号列表。"""

    validated = _validated_options(options)
    return "\n".join(f"{option['number']}. {option['text']}" for option in validated)


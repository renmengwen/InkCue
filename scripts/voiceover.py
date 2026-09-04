#!/usr/bin/env python3
"""Pure voice-over planning, identity and manifest contracts.

This module intentionally performs no network or FFmpeg work.  Provider
adapters consume the frozen protocol below, while later orchestration owns
queueing, checkpoint publication, approval gates and canonical media.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


# Scripts are used both as ``python scripts/foo.py`` (top-level imports) and as
# ``scripts.foo`` in unittest.  Keep the protocol classes singleton in either
# import order so adapter ``isinstance`` checks cannot see duplicate classes.
if __name__ == "scripts.voiceover":
    sys.modules.setdefault("voiceover", sys.modules[__name__])
elif __name__ == "voiceover":
    sys.modules.setdefault("scripts.voiceover", sys.modules[__name__])


VOICE_PLAN_SCHEMA_VERSION = 1
VOICE_MANIFEST_SCHEMA_VERSION = 1
SEGMENTATION_CONTRACT_VERSION = "speech-unit-v1"
FULL_TRACK_SEGMENTATION_CONTRACT_VERSION = "full-track-v1"
DEFAULT_PROVIDER_CONTRACT_VERSION = "edge-tts-python-7.2.8-v1"
DOUBAO_PROVIDER_CONTRACT_VERSION = "doubao-seed-audio-expressive-native-word-v2"
DOUBAO_PROMPT_SPEC_VERSION = "doubao-whiteboard-authored-performance-v3"
DOUBAO_MODEL = "seed-audio-1.0"
DOUBAO_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/create"
SUPPORTED_PROVIDER_PROTOCOLS = {
    "edge-tts": "edge-tts",
    "minimax": "MiniMax",
    "doubao": "Doubao",
}
SUPPORTED_AUDIO_PROVIDERS = set(SUPPORTED_PROVIDER_PROTOCOLS)
REVIEW_POLICIES = frozenset({"user_first", "agent_first"})
SAMPLE_APPROVAL_BASES = frozenset({"user_sample_listening", "user_joint_initial_approval"})
FULL_APPROVAL_BASES = frozenset({"human_full_listening", "technical_after_user_sample"})
FULL_REVIEW_BASES = frozenset(
    {
        "current_full_audio_listening",
        "user_joint_initial_sample_authorization_and_current_technical_validation",
    }
)
DEFAULT_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
DEFAULT_SEGMENTATION = {
    "contractVersion": SEGMENTATION_CONTRACT_VERSION,
    "minCodePoints": 12,
    "targetCodePoints": 24,
    "maxCodePoints": 36,
}
FULL_TRACK_SEGMENTATION = {
    "contractVersion": FULL_TRACK_SEGMENTATION_CONTRACT_VERSION,
    "sceneSeparator": "\n\n",
}
SEGMENT_STATUSES = (
    "pending",
    "prepared",
    "requesting",
    # Accepted only for legacy manifests; new orchestration never writes it.
    "normalizing",
    "candidate_ready",
    "publishing",
    "validated",
    "failed",
    "cancelled",
    "unknown_external_outcome",
)
SAMPLE_STATUSES = (
    "pending",
    "validated",
    "unknown_external_outcome",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RATE_VOLUME_RE = re.compile(r"^[+-]\d+%$")
_PITCH_RE = re.compile(r"^[+-]\d+Hz$")
_STRONG_BREAKS = frozenset("。！？!?；;…")
_SECONDARY_BREAKS = frozenset("，,、：:")


class VoiceoverValidationError(ValueError):
    """Raised when a plan, speech unit or manifest violates the contract."""


class ProviderError(RuntimeError):
    """Base class for classified provider failures."""


class RetryableProviderError(ProviderError):
    """A bounded retry may succeed (DNS, connection, timeout, 429/5xx)."""


class PermanentProviderError(ProviderError):
    """A retry must not be attempted without changing the request/config."""


class CancelledError(ProviderError):
    """The caller cancelled the request; cancellation is never retried."""


@dataclass(frozen=True)
class SynthesisRequest:
    """Frozen provider request.  Camel-case field names match persisted JSON."""

    text: str
    voice: str
    normalizedRate: str
    normalizedPitch: str
    normalizedVolume: str
    providerContractVersion: str
    timeoutSeconds: float
    cancellationToken: object | None = None


@dataclass(frozen=True)
class RawAudioResult:
    """Untrusted provider bytes; normalization happens in a separate module."""

    bytes: bytes
    declaredFormat: str
    providerRequestId: str | None = None
    providerSubtitleBytes: bytes | None = None
    providerSubtitleType: str | None = None
    providerMetadata: Mapping[str, Any] | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    """Synchronous provider boundary used by real and fake adapters."""

    def synthesize(self, request: SynthesisRequest) -> RawAudioResult:
        ...


# A protocol alias retained for callers that want an Edge-specific annotation.
EdgeTtsAdapter = ProviderAdapter


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise VoiceoverValidationError("hash 文本必须是字符串")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_signed(value: int | str, *, suffix: str, label: str) -> str:
    if isinstance(value, bool):
        raise VoiceoverValidationError(f"{label} 不能是布尔值")
    if isinstance(value, int):
        return f"{value:+d}{suffix}"
    if not isinstance(value, str):
        raise VoiceoverValidationError(f"{label} 必须是整数或带单位字符串")
    stripped = value.strip()
    if stripped == "default":
        return f"+0{suffix}"
    pattern = _PITCH_RE if suffix == "Hz" else _RATE_VOLUME_RE
    if not pattern.fullmatch(stripped):
        raise VoiceoverValidationError(f"{label} 必须显式包含正负号和 {suffix} 单位")
    number = int(stripped[: -len(suffix)])
    return f"{number:+d}{suffix}"


def normalize_rate(value: int | str) -> str:
    return _normalise_signed(value, suffix="%", label="rate")


def normalize_pitch(value: int | str = 0) -> str:
    return _normalise_signed(value, suffix="Hz", label="pitch")


def normalize_volume(value: int | str = 0) -> str:
    return _normalise_signed(value, suffix="%", label="volume")


def _validate_relative_file(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VoiceoverValidationError(f"{label} 必须是非空 POSIX 相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise VoiceoverValidationError(f"{label} 不能是绝对路径或包含点路径")
    if ":" in path.parts[0]:
        raise VoiceoverValidationError(f"{label} 不能包含盘符")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise VoiceoverValidationError(f"{label} 必须是 64 位小写 SHA-256")
    return value


def _normalise_speech_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_punctuation_only(text: str) -> bool:
    compact = "".join(character for character in text if not character.isspace())
    return bool(compact) and all(unicodedata.category(character).startswith("P") for character in compact)


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def _split_at_breaks(text: str, breaks: frozenset[str]) -> list[str]:
    parts: list[str] = []
    start = 0
    for index, character in enumerate(text):
        if character in breaks:
            parts.append(text[start : index + 1])
            start = index + 1
    if start < len(text):
        parts.append(text[start:])
    return [part for part in parts if part]


def _hard_split_codepoints(text: str, maximum: int) -> list[str]:
    return [text[index : index + maximum] for index in range(0, len(text), maximum)]


def _split_long_text(text: str, maximum: int) -> list[str]:
    """Split deterministically: sentence end, secondary punctuation, code point."""

    strong = _split_at_breaks(text, _STRONG_BREAKS)
    result: list[str] = []
    for sentence in strong:
        if len(sentence) <= maximum:
            result.append(sentence)
            continue
        secondary = _split_at_breaks(sentence, _SECONDARY_BREAKS)
        buffer = ""
        for part in secondary:
            if len(part) > maximum:
                if buffer:
                    result.append(buffer)
                    buffer = ""
                result.extend(_hard_split_codepoints(part, maximum))
            elif not buffer or len(buffer) + len(part) <= maximum:
                buffer += part
            else:
                result.append(buffer)
                buffer = part
        if buffer:
            result.append(buffer)
    return [part for part in result if part]


def _validate_segmentation(value: Mapping[str, Any] | None) -> dict[str, Any]:
    segmentation = dict(DEFAULT_SEGMENTATION if value is None else value)
    contract_version = segmentation.get("contractVersion")
    if contract_version == FULL_TRACK_SEGMENTATION_CONTRACT_VERSION:
        if segmentation.get("sceneSeparator") != "\n\n":
            raise VoiceoverValidationError("full-track sceneSeparator 首版固定为两个换行")
        if set(segmentation) != {"contractVersion", "sceneSeparator"}:
            raise VoiceoverValidationError("full-track segmentation 包含未知字段")
        return copy.deepcopy(FULL_TRACK_SEGMENTATION)
    if contract_version != SEGMENTATION_CONTRACT_VERSION:
        raise VoiceoverValidationError("segmentation.contractVersion 不受支持")
    minimum = segmentation.get("minCodePoints")
    target = segmentation.get("targetCodePoints")
    maximum = segmentation.get("maxCodePoints")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (minimum, target, maximum)):
        raise VoiceoverValidationError("分段 code point 阈值必须是整数")
    if not 0 < minimum <= target <= maximum:
        raise VoiceoverValidationError("分段阈值必须满足 0 < min <= target <= max")
    return {
        "contractVersion": SEGMENTATION_CONTRACT_VERSION,
        "minCodePoints": minimum,
        "targetCodePoints": target,
        "maxCodePoints": maximum,
    }


def plan_full_track_unit(
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]] | None = None,
    *,
    segmentation: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把全部确认旁白规划成一个保留 scene 段落的合成请求。

    scene/cue 边界仍写入 unit 审计信息，但不会在 provider 请求层拆分。
    正式 scene 时间必须在整轨音频生成后由外部 ASR 对齐结果派生。
    """

    validated_cues = _validate_cues(cues)
    scene_ranges = _scene_assignments(validated_cues, scenes)
    contract = _validate_segmentation(segmentation or FULL_TRACK_SEGMENTATION)
    if contract["contractVersion"] != FULL_TRACK_SEGMENTATION_CONTRACT_VERSION:
        raise VoiceoverValidationError("整轨规划必须使用 full-track segmentation contract")

    scene_texts: list[str] = []
    source_parts: list[dict[str, int]] = []
    for scene_id, first, last in scene_ranges:
        text = ""
        for cue in validated_cues[first - 1 : last]:
            speech = _normalise_speech_text(cue["text"])
            if _is_punctuation_only(speech):
                raise VoiceoverValidationError(f"{scene_id} 包含纯标点 cue，无法生成整轨旁白")
            text = _join_text(text, speech)
            source_parts.append({"sourceOrdinal": cue["sourceOrdinal"], "partIndex": 1})
        if not text:
            raise VoiceoverValidationError(f"{scene_id} 没有可朗读文本")
        scene_texts.append(text)

    speech_text = contract["sceneSeparator"].join(scene_texts)
    source_text = speech_text
    first = 1
    last = len(validated_cues)
    scene_specs = [
        {"sceneId": scene_id, "sourceCueRange": [scene_first, scene_last]}
        for scene_id, scene_first, scene_last in scene_ranges
    ]
    unit = {
        "index": 1,
        "sceneId": "full-track",
        "sceneCueRanges": scene_specs,
        "sourceCueRange": [first, last],
        "sourceOrdinalRange": [first, last],
        "sourceOrdinals": list(range(first, last + 1)),
        "sourceParts": source_parts,
        "originalText": source_text,
        "speechText": speech_text,
        "codePointCount": len(speech_text),
        "sourceTextHash": sha256_text(source_text),
        "speechTextHash": sha256_text(speech_text),
        "sourceTextIdentityHash": sha256_json(
            {
                "sceneCueRanges": scene_specs,
                "sourceParts": source_parts,
                "originalText": source_text,
            }
        ),
        "sourceTimingIdentityHash": sha256_json(
            {
                "sceneCueRanges": scene_specs,
                "timings": [
                    {
                        "sourceOrdinal": cue["sourceOrdinal"],
                        "startMs": cue["startMs"],
                        "endMs": cue["endMs"],
                    }
                    for cue in validated_cues
                ],
            }
        ),
    }
    validate_speech_units([unit], cue_count=len(validated_cues))
    return [unit]


def _validate_cues(cues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(cues, Sequence) or isinstance(cues, (str, bytes)) or not cues:
        raise VoiceoverValidationError("cues 不能为空")
    validated: list[dict[str, Any]] = []
    previous_end = -1
    for expected, source in enumerate(cues, start=1):
        if not isinstance(source, Mapping):
            raise VoiceoverValidationError(f"cue[{expected - 1}] 必须是对象")
        ordinal = source.get("sourceOrdinal", source.get("index"))
        start = source.get("startMs")
        end = source.get("endMs")
        text = source.get("text")
        if ordinal != expected:
            raise VoiceoverValidationError("sourceOrdinal 必须从 1 起连续")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < previous_end
            or end <= start
        ):
            raise VoiceoverValidationError(f"sourceOrdinal={expected} 时间无效或重叠")
        if not isinstance(text, str) or not text.strip():
            raise VoiceoverValidationError(f"sourceOrdinal={expected} 文本不能为空")
        item = {
            "sourceOrdinal": expected,
            "startMs": start,
            "endMs": end,
            "text": text,
        }
        original_index = source.get("originalIndex")
        if original_index is not None:
            if isinstance(original_index, bool) or not isinstance(original_index, int) or original_index <= 0:
                raise VoiceoverValidationError(f"sourceOrdinal={expected} originalIndex 无效")
            item["originalIndex"] = original_index
        validated.append(item)
        previous_end = end
    return validated


def _scene_assignments(
    cues: Sequence[Mapping[str, Any]], scenes: Sequence[Mapping[str, Any]] | None
) -> list[tuple[str, int, int]]:
    if scenes is None:
        return [("scene-01", 1, len(cues))]
    if not isinstance(scenes, Sequence) or isinstance(scenes, (str, bytes)) or not scenes:
        raise VoiceoverValidationError("scenes 不能为空")
    result: list[tuple[str, int, int]] = []
    expected = 1
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            raise VoiceoverValidationError(f"scenes[{index - 1}] 必须是对象")
        scene_id = scene.get("sceneId", f"scene-{index:02d}")
        cue_range = scene.get("sourceCueRange", scene.get("cueRange"))
        if not isinstance(scene_id, str) or not scene_id:
            raise VoiceoverValidationError("sceneId 不能为空")
        if not isinstance(cue_range, (list, tuple)) or len(cue_range) != 2:
            raise VoiceoverValidationError(f"{scene_id}.sourceCueRange 无效")
        first, last = cue_range
        if (
            isinstance(first, bool)
            or not isinstance(first, int)
            or isinstance(last, bool)
            or not isinstance(last, int)
            or first != expected
            or last < first
            or last > len(cues)
        ):
            raise VoiceoverValidationError(f"{scene_id} cue range 不连续或越界")
        result.append((scene_id, first, last))
        expected = last + 1
    if expected != len(cues) + 1:
        raise VoiceoverValidationError("scene cue ranges 未连续覆盖全部 cues")
    return result


def _make_fragment(cue: Mapping[str, Any], text: str, part_index: int) -> dict[str, Any]:
    if text and text[-1] in _STRONG_BREAKS:
        break_kind = "strong"
    elif text and text[-1] in _SECONDARY_BREAKS:
        break_kind = "secondary"
    else:
        break_kind = "none"
    return {
        "sourceOrdinal": cue["sourceOrdinal"],
        "sourcePartIndex": part_index,
        "sourceText": text,
        "speechText": text,
        "startMs": cue["startMs"],
        "endMs": cue["endMs"],
        "breakKind": break_kind,
    }


def _unit_from_fragments(
    index: int,
    scene_id: str,
    fragments: Sequence[Mapping[str, Any]],
    *,
    segmentation: Mapping[str, Any],
    synthesis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    ordinals = [int(fragment["sourceOrdinal"]) for fragment in fragments]
    source_text = "".join(str(fragment["sourceText"]) for fragment in fragments)
    speech_text = "".join(str(fragment["speechText"]) for fragment in fragments)
    first, last = min(ordinals), max(ordinals)
    unit = {
        "index": index,
        "sceneId": scene_id,
        "sourceCueRange": [first, last],
        "sourceOrdinalRange": [first, last],
        "sourceOrdinals": list(dict.fromkeys(ordinals)),
        "sourceParts": [
            {
                "sourceOrdinal": int(fragment["sourceOrdinal"]),
                "partIndex": int(fragment["sourcePartIndex"]),
            }
            for fragment in fragments
        ],
        "originalText": source_text,
        "speechText": speech_text,
        "codePointCount": len(speech_text),
        "sourceTextHash": sha256_text(source_text),
        "speechTextHash": sha256_text(speech_text),
    }
    unit["sourceTextIdentityHash"] = sha256_json(
        {
            "sceneId": scene_id,
            "sourceParts": unit["sourceParts"],
            "originalText": source_text,
        }
    )
    unit["sourceTimingIdentityHash"] = sha256_json(
        {
            "sceneId": scene_id,
            "sourceParts": unit["sourceParts"],
            "timings": [
                {
                    "sourceOrdinal": int(fragment["sourceOrdinal"]),
                    "startMs": int(fragment["startMs"]),
                    "endMs": int(fragment["endMs"]),
                }
                for fragment in fragments
            ],
        }
    )
    if synthesis is not None:
        unit["voiceSynthesisIdentityHash"] = sha256_json(
            {
                "speechText": speech_text,
                "sourceOrdinalRange": [first, last],
                "sourceParts": unit["sourceParts"],
                "voice": synthesis["voice"],
                "normalizedRate": synthesis["normalizedRate"],
                "language": synthesis["language"],
                "segmentationContractVersion": segmentation["contractVersion"],
                "providerContractVersion": synthesis["providerContractVersion"],
                "providerOptions": synthesis.get("providerOptions", {}),
            }
        )
    return unit


def plan_speech_units(
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]] | None = None,
    *,
    segmentation: Mapping[str, Any] | None = None,
    voice: str | None = None,
    normalized_rate: int | str = 0,
    language: str = "zh-CN",
    provider_contract_version: str = DEFAULT_PROVIDER_CONTRACT_VERSION,
) -> list[dict[str, Any]]:
    """Build deterministic, scene-bounded speech units from shared SRT cues.

    Long cues may occupy adjacent units.  ``sourceParts`` disambiguates those
    fragments so source text is covered exactly once even when their cue range
    necessarily repeats the same ordinal.
    """

    validated_cues = _validate_cues(cues)
    scene_ranges = _scene_assignments(validated_cues, scenes)
    contract = _validate_segmentation(segmentation)
    synthesis: dict[str, Any] | None = None
    if voice is not None:
        if not isinstance(voice, str) or not voice:
            raise VoiceoverValidationError("voice 不能为空")
        if not isinstance(language, str) or not language:
            raise VoiceoverValidationError("language 不能为空")
        if not isinstance(provider_contract_version, str) or not provider_contract_version:
            raise VoiceoverValidationError("provider contract version 不能为空")
        synthesis = {
            "voice": voice,
            "normalizedRate": normalize_rate(normalized_rate),
            "language": language,
            "providerContractVersion": provider_contract_version,
        }

    units: list[dict[str, Any]] = []
    maximum = contract["maxCodePoints"]
    target = contract["targetCodePoints"]
    minimum = contract["minCodePoints"]
    for scene_id, first, last in scene_ranges:
        fragments: list[dict[str, Any]] = []
        has_spoken_content = False
        for cue in validated_cues[first - 1 : last]:
            speech = _normalise_speech_text(cue["text"])
            if _is_punctuation_only(speech):
                # Keep a source part for audit/coverage, then let the normal
                # short-fragment grouping attach it to an adjacent unit.
                fragments.append(_make_fragment(cue, speech, 1))
                continue
            has_spoken_content = True
            parts = _split_long_text(speech, maximum)
            fragments.extend(
                _make_fragment(cue, part, part_index)
                for part_index, part in enumerate(parts, start=1)
            )
        if not has_spoken_content:
            raise VoiceoverValidationError(f"{scene_id} 只包含标点，无法生成朗读单元")

        grouped: list[list[dict[str, Any]]] = []
        buffer: list[dict[str, Any]] = []
        buffer_text = ""
        for fragment in fragments:
            fragment_text = str(fragment["speechText"])
            candidate = _join_text(buffer_text, fragment_text)
            prior_break = buffer[-1].get("breakKind") if buffer else None
            should_merge = not buffer or (
                len(candidate) <= maximum
                and (
                    len(buffer_text) < minimum
                    or len(fragment_text) < minimum
                    or (prior_break != "strong" and len(candidate) <= target)
                )
            )
            if should_merge:
                if buffer:
                    separator = " " if candidate != buffer_text + fragment_text else ""
                    if separator:
                        fragment = dict(fragment)
                        fragment["sourceText"] = separator + str(fragment["sourceText"])
                        fragment["speechText"] = separator + str(fragment["speechText"])
                buffer.append(fragment)
                buffer_text = candidate
            else:
                grouped.append(buffer)
                buffer = [fragment]
                buffer_text = fragment_text
        if buffer:
            grouped.append(buffer)
        # Avoid a tiny tail when it can still fit in the previous unit.
        if len(grouped) >= 2:
            tail_text = "".join(str(item["speechText"]) for item in grouped[-1])
            previous_text = "".join(str(item["speechText"]) for item in grouped[-2])
            if len(tail_text) < minimum and len(_join_text(previous_text, tail_text)) <= maximum:
                grouped[-2].extend(grouped.pop())

        for group in grouped:
            units.append(
                _unit_from_fragments(
                    len(units) + 1,
                    scene_id,
                    group,
                    segmentation=contract,
                    synthesis=synthesis,
                )
            )

    validate_speech_units(units, cue_count=len(validated_cues), scenes=scene_ranges)
    return units


build_speech_units = plan_speech_units


def validate_speech_units(
    units: Sequence[Mapping[str, Any]],
    *,
    cue_count: int,
    scenes: Sequence[tuple[str, int, int]] | None = None,
) -> None:
    if not units:
        raise VoiceoverValidationError("speech units 不能为空")
    covered_parts: set[tuple[int, int]] = set()
    covered_ordinals: set[int] = set()
    scene_lookup: dict[int, str] = {}
    if scenes:
        for scene_id, first, last in scenes:
            for ordinal in range(first, last + 1):
                scene_lookup[ordinal] = scene_id
    for expected, unit in enumerate(units, start=1):
        if unit.get("index") != expected:
            raise VoiceoverValidationError("speech unit index 必须从 1 起连续")
        speech = unit.get("speechText")
        if not isinstance(speech, str) or not speech:
            raise VoiceoverValidationError(f"unit-{expected:04d} speechText 不能为空")
        source_parts = unit.get("sourceParts")
        if not isinstance(source_parts, list) or not source_parts:
            raise VoiceoverValidationError(f"unit-{expected:04d} sourceParts 不能为空")
        ordinals: list[int] = []
        for part in source_parts:
            if not isinstance(part, Mapping):
                raise VoiceoverValidationError("sourceParts 必须是对象列表")
            key = (part.get("sourceOrdinal"), part.get("partIndex"))
            if (
                isinstance(key[0], bool)
                or not isinstance(key[0], int)
                or isinstance(key[1], bool)
                or not isinstance(key[1], int)
                or key[0] <= 0
                or key[1] <= 0
                or key in covered_parts
            ):
                raise VoiceoverValidationError("sourceParts 重复或无效")
            covered_parts.add(key)
            covered_ordinals.add(key[0])
            ordinals.append(key[0])
        if any(right < left for left, right in zip(ordinals, ordinals[1:])):
            raise VoiceoverValidationError("unit 内 sourceOrdinal 必须有序")
        if max(ordinals) - min(ordinals) + 1 != len(set(ordinals)):
            raise VoiceoverValidationError("unit 的 source cue range 不能有空洞")
        expected_range = [min(ordinals), max(ordinals)]
        if unit.get("sourceCueRange") != expected_range or unit.get("sourceOrdinalRange") != expected_range:
            raise VoiceoverValidationError("unit source range 与 sourceParts 不一致")
        if scene_lookup and any(scene_lookup.get(ordinal) != unit.get("sceneId") for ordinal in ordinals):
            raise VoiceoverValidationError("speech unit 不得跨 scene")
    if covered_ordinals != set(range(1, cue_count + 1)):
        raise VoiceoverValidationError("speech units 未连续覆盖全部 source cues")


def source_text_identity_hash(
    cues: Sequence[Mapping[str, Any]], scenes: Sequence[Mapping[str, Any]] | None = None
) -> str:
    validated = _validate_cues(cues)
    assignments = _scene_assignments(validated, scenes)
    by_ordinal = {
        ordinal: scene_id
        for scene_id, first, last in assignments
        for ordinal in range(first, last + 1)
    }
    return sha256_json(
        [
            {
                "sourceOrdinal": cue["sourceOrdinal"],
                "sceneId": by_ordinal[cue["sourceOrdinal"]],
                "text": _normalise_speech_text(cue["text"]),
            }
            for cue in validated
        ]
    )


def source_timing_identity_hash(
    cues: Sequence[Mapping[str, Any]], scenes: Sequence[Mapping[str, Any]] | None = None
) -> str:
    validated = _validate_cues(cues)
    assignments = _scene_assignments(validated, scenes)
    return sha256_json(
        {
            "cues": [
                {
                    "sourceOrdinal": cue["sourceOrdinal"],
                    "startMs": cue["startMs"],
                    "endMs": cue["endMs"],
                }
                for cue in validated
            ],
            "scenes": [
                {"sceneId": scene_id, "sourceCueRange": [first, last]}
                for scene_id, first, last in assignments
            ],
        }
    )


def build_voice_plan(
    *,
    project_id: str,
    source_srt_sha256: str,
    cues: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]] | None = None,
    voice: str = "zh-CN-YunjianNeural",
    language: str = "zh-CN",
    rate: int | str = 0,
    pitch: int | str = 0,
    volume: int | str = 0,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    provider_id: str = "edge-tts",
    protocol: str = "edge-tts",
    provider_contract_version: str = DEFAULT_PROVIDER_CONTRACT_VERSION,
    provider_options: Mapping[str, Any] | None = None,
    source_file: str = "source/source.srt",
    segmentation: Mapping[str, Any] | None = None,
    duration_review_threshold_ratio: float = 0.10,
) -> dict[str, Any]:
    if not isinstance(project_id, str) or not project_id:
        raise VoiceoverValidationError("projectId 不能为空")
    if not all(isinstance(value, str) and value for value in (voice, language, output_format, provider_id, protocol, provider_contract_version)):
        raise VoiceoverValidationError("provider、voice、language、output format 不能为空")
    provider_id = provider_id.lower()
    expected_protocol = SUPPORTED_PROVIDER_PROTOCOLS.get(provider_id)
    if provider_id not in SUPPORTED_AUDIO_PROVIDERS or protocol != expected_protocol:
        raise VoiceoverValidationError(
            "provider/protocol 必须是 edge-tts/edge-tts、minimax/MiniMax 或 doubao/Doubao"
        )
    if isinstance(duration_review_threshold_ratio, bool) or not isinstance(duration_review_threshold_ratio, (int, float)) or duration_review_threshold_ratio != 0.10:
        raise VoiceoverValidationError("首版 durationReviewThresholdRatio 固定为 0.10")
    source_sha = _require_sha256(source_srt_sha256, label="source.sha256")
    contract = _validate_segmentation(segmentation)
    plan = {
        "schemaVersion": VOICE_PLAN_SCHEMA_VERSION,
        "projectId": project_id,
        "mode": provider_id,
        "provider": {
            "id": provider_id,
            "protocol": protocol,
            "contractVersion": provider_contract_version,
            "options": copy.deepcopy(dict(provider_options or {})),
        },
        "selection": {
            "voice": voice,
            "language": language,
            "rate": normalize_rate(rate),
            "pitch": normalize_pitch(pitch),
            "volume": normalize_volume(volume),
            "outputFormat": output_format,
        },
        "source": {
            "file": _validate_relative_file(source_file, label="source.file"),
            "sha256": source_sha,
        },
        "segmentation": contract,
        "timingPolicy": {
            "mode": "audio-authoritative",
            "durationReviewThresholdRatio": 0.10,
        },
        "sourceTextIdentityHash": source_text_identity_hash(cues, scenes),
        "sourceTimingIdentityHash": source_timing_identity_hash(cues, scenes),
    }
    return validate_voice_plan(plan)


def validate_voice_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VoiceoverValidationError("voice plan 必须是对象")
    plan = copy.deepcopy(dict(value))
    if plan.get("schemaVersion") != VOICE_PLAN_SCHEMA_VERSION:
        raise VoiceoverValidationError("voice plan schemaVersion 不受支持")
    if not isinstance(plan.get("projectId"), str) or not plan["projectId"]:
        raise VoiceoverValidationError("voice plan projectId 不能为空")
    if plan.get("mode") not in SUPPORTED_AUDIO_PROVIDERS:
        raise VoiceoverValidationError("voice plan mode 必须是 edge-tts、minimax 或 doubao")
    provider = plan.get("provider")
    selection = plan.get("selection")
    source = plan.get("source")
    timing = plan.get("timingPolicy")
    if not all(isinstance(item, Mapping) for item in (provider, selection, source, timing)):
        raise VoiceoverValidationError("voice plan 缺少 provider/selection/source/timingPolicy")
    provider_id = provider.get("id")
    expected_protocol = SUPPORTED_PROVIDER_PROTOCOLS.get(provider_id)
    if provider_id not in SUPPORTED_AUDIO_PROVIDERS or provider.get("protocol") != expected_protocol:
        raise VoiceoverValidationError("provider/protocol 与支持的 provider 不匹配")
    if plan.get("mode") != provider_id:
        raise VoiceoverValidationError("voice plan mode 必须与 provider.id 一致")
    if not isinstance(provider.get("contractVersion"), str) or not provider["contractVersion"]:
        raise VoiceoverValidationError("provider.contractVersion 不能为空")
    if not isinstance(provider.get("options", {}), Mapping):
        raise VoiceoverValidationError("provider.options 必须是对象")
    if provider_id == "doubao":
        options = provider.get("options", {})
        prompt_spec = options.get("promptSpec")
        if (
            provider.get("contractVersion") != DOUBAO_PROVIDER_CONTRACT_VERSION
            or options.get("model") != DOUBAO_MODEL
            or options.get("endpoint") != DOUBAO_ENDPOINT
            or not isinstance(prompt_spec, Mapping)
            or prompt_spec.get("contractVersion") != DOUBAO_PROMPT_SPEC_VERSION
            or options.get("maxTextPromptCharacters") != 3000
            or options.get("maxAudioDurationSeconds") != 120
            or options.get("nativeWordSubtitlesRequired") is not True
        ):
            raise VoiceoverValidationError("豆包 voice plan 与当前 v2 合同不匹配")
    for field in ("voice", "language", "outputFormat"):
        if not isinstance(selection.get(field), str) or not selection[field]:
            raise VoiceoverValidationError(f"selection.{field} 不能为空")
    selection["rate"] = normalize_rate(selection.get("rate"))
    selection["pitch"] = normalize_pitch(selection.get("pitch"))
    selection["volume"] = normalize_volume(selection.get("volume"))
    source["file"] = _validate_relative_file(source.get("file"), label="source.file")
    source["sha256"] = _require_sha256(source.get("sha256"), label="source.sha256")
    plan["segmentation"] = _validate_segmentation(plan.get("segmentation"))
    if timing.get("mode") != "audio-authoritative" or timing.get("durationReviewThresholdRatio") != 0.10:
        raise VoiceoverValidationError("timingPolicy 必须为 audio-authoritative/0.10")
    for field in ("sourceTextIdentityHash", "sourceTimingIdentityHash"):
        _require_sha256(plan.get(field), label=field)
    return plan


def voice_plan_audit_hash(plan: Mapping[str, Any]) -> str:
    validated = validate_voice_plan(plan)
    validated.pop("voicePlanAuditHash", None)
    return sha256_json(validated)


def synthesis_settings_from_plan(plan: Mapping[str, Any]) -> dict[str, str]:
    validated = validate_voice_plan(plan)
    return {
        "voice": validated["selection"]["voice"],
        "normalizedRate": validated["selection"]["rate"],
        "normalizedPitch": validated["selection"]["pitch"],
        "normalizedVolume": validated["selection"]["volume"],
        "language": validated["selection"]["language"],
        "providerContractVersion": validated["provider"]["contractVersion"],
    }


def bind_synthesis_identities(
    units: Sequence[Mapping[str, Any]], voice_plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    plan = validate_voice_plan(voice_plan)
    settings = synthesis_settings_from_plan(plan)
    contract = plan["segmentation"]
    bound: list[dict[str, Any]] = []
    for source in units:
        unit = copy.deepcopy(dict(source))
        required = ("speechText", "sourceOrdinalRange", "sourceParts")
        if any(field not in unit for field in required):
            raise VoiceoverValidationError("speech unit 缺少 synthesis identity 输入")
        identity_inputs: dict[str, Any] = {
            "speechText": unit["speechText"],
            "sourceOrdinalRange": unit["sourceOrdinalRange"],
            "sourceParts": unit["sourceParts"],
            "voice": settings["voice"],
            "normalizedRate": settings["normalizedRate"],
            "language": settings["language"],
            "segmentationContractVersion": contract["contractVersion"],
            "providerContractVersion": settings["providerContractVersion"],
            "providerOptions": plan["provider"].get("options", {}),
        }
        if plan["provider"]["id"] == "doubao":
            prompt_sha = unit.get("providerTextPromptSha256")
            _require_sha256(prompt_sha, label="providerTextPromptSha256")
            identity_inputs["providerTextPromptSha256"] = prompt_sha
        unit["voiceSynthesisIdentityHash"] = sha256_json(identity_inputs)
        bound.append(unit)
    return bound


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_voice_manifest(
    *,
    project_id: str,
    voice_plan: Mapping[str, Any],
    speech_units: Sequence[Mapping[str, Any]],
    timestamp: str | None = None,
) -> dict[str, Any]:
    plan = validate_voice_plan(voice_plan)
    if project_id != plan["projectId"]:
        raise VoiceoverValidationError("manifest projectId 与 voice plan 不一致")
    bound_units = bind_synthesis_identities(speech_units, plan)
    now = _utc_now() if timestamp is None else timestamp
    if not isinstance(now, str) or not now:
        raise VoiceoverValidationError("timestamp 不能为空")
    manifest = {
        "schemaVersion": VOICE_MANIFEST_SCHEMA_VERSION,
        "projectId": project_id,
        "voicePlan": {
            "file": "planning/voice-plan.json",
            "voicePlanAuditHash": voice_plan_audit_hash(plan),
        },
        "source": copy.deepcopy(plan["source"]),
        "identities": {
            "sourceTextIdentityHash": plan["sourceTextIdentityHash"],
            "sourceTimingIdentityHash": plan["sourceTimingIdentityHash"],
        },
        "sample": {
            "status": "pending",
            "identityHash": None,
            "media": None,
            "approval": {
                "approved": False,
                "identityHash": None,
                "approvalBasis": None,
                "approvedAt": None,
            },
        },
        "runs": [],
        "segments": [
            {
                "index": unit["index"],
                "sceneId": unit["sceneId"],
                "sourceCueRange": unit["sourceCueRange"],
                "sourceOrdinalRange": unit["sourceOrdinalRange"],
                "sourceTextHash": unit["sourceTextHash"],
                "speechTextHash": unit["speechTextHash"],
                "sourceTextIdentityHash": unit["sourceTextIdentityHash"],
                "sourceTimingIdentityHash": unit["sourceTimingIdentityHash"],
                "voiceSynthesisIdentityHash": unit["voiceSynthesisIdentityHash"],
                "providerTextPromptSha256": unit.get("providerTextPromptSha256"),
                "status": "pending",
                "relativePath": f"audio/segments/unit-{unit['index']:04d}.wav",
                "audioMime": None,
                "audioCodec": None,
                "sampleRate": None,
                "channels": None,
                "bytes": None,
                "durationMs": None,
                "sha256": None,
                "attempts": 0,
                "currentAttempt": None,
                "createdAt": None,
                "updatedAt": now,
                "errorStage": None,
                "errorSummary": None,
            }
            for unit in bound_units
        ],
        "composite": {"status": "pending", "relativePath": "audio/narration.wav"},
        "timeline": {"status": "pending", "relativePath": "audio/timeline.json"},
        "narrationSrt": {"status": "pending", "relativePath": "audio/narration.srt"},
        "fullApproval": {
            "approved": False,
            "identityHash": None,
            "durationDecision": None,
            "reviewPolicy": None,
            "approvalBasis": None,
            "reviewBasis": None,
            "approvedAt": None,
        },
        "createdAt": now,
        "updatedAt": now,
    }
    return validate_voice_manifest(manifest, voice_plan=plan, speech_units=bound_units)


def validate_voice_manifest(
    value: Mapping[str, Any],
    *,
    voice_plan: Mapping[str, Any] | None = None,
    speech_units: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VoiceoverValidationError("voice manifest 必须是对象")
    manifest = copy.deepcopy(dict(value))
    if manifest.get("schemaVersion") != VOICE_MANIFEST_SCHEMA_VERSION:
        raise VoiceoverValidationError("voice manifest schemaVersion 不受支持")
    if not isinstance(manifest.get("projectId"), str) or not manifest["projectId"]:
        raise VoiceoverValidationError("voice manifest projectId 不能为空")
    voice_plan_ref = manifest.get("voicePlan")
    source = manifest.get("source")
    if not isinstance(voice_plan_ref, Mapping) or not isinstance(source, Mapping):
        raise VoiceoverValidationError("voice manifest 缺少 voicePlan/source")
    _validate_relative_file(voice_plan_ref.get("file"), label="voicePlan.file")
    _require_sha256(voice_plan_ref.get("voicePlanAuditHash"), label="voicePlanAuditHash")
    _validate_relative_file(source.get("file"), label="source.file")
    _require_sha256(source.get("sha256"), label="source.sha256")
    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        raise VoiceoverValidationError("voice manifest segments 不能为空")
    for expected, segment in enumerate(segments, start=1):
        if not isinstance(segment, Mapping) or segment.get("index") != expected:
            raise VoiceoverValidationError("segment index 必须从 1 起连续")
        if segment.get("status") not in SEGMENT_STATUSES:
            raise VoiceoverValidationError(f"segment-{expected:04d} status 无效")
        _validate_relative_file(segment.get("relativePath"), label="segment.relativePath")
        for field in (
            "sourceTextHash",
            "speechTextHash",
            "sourceTextIdentityHash",
            "sourceTimingIdentityHash",
            "voiceSynthesisIdentityHash",
        ):
            _require_sha256(segment.get(field), label=f"segment.{field}")
        provider_prompt_sha = segment.get("providerTextPromptSha256")
        if provider_prompt_sha is not None:
            _require_sha256(
                provider_prompt_sha, label="segment.providerTextPromptSha256"
            )
        attempts = segment.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise VoiceoverValidationError("segment.attempts 必须是非负整数")
        if segment.get("status") == "validated":
            for field in ("audioMime", "audioCodec", "sampleRate", "channels", "bytes", "durationMs", "sha256"):
                if segment.get(field) in (None, ""):
                    raise VoiceoverValidationError(f"validated segment 缺少 {field}")
            _require_sha256(segment.get("sha256"), label="segment.sha256")
        attempt = segment.get("currentAttempt")
        if attempt is not None:
            if not isinstance(attempt, Mapping):
                raise VoiceoverValidationError("segment.currentAttempt 必须是对象或 null")
            if not isinstance(attempt.get("attemptId"), str) or not attempt["attemptId"]:
                raise VoiceoverValidationError("segment.currentAttempt.attemptId 不能为空")
            if attempt.get("status") != segment.get("status"):
                raise VoiceoverValidationError("segment.currentAttempt.status 与 segment.status 不一致")
            if attempt.get("inputIdentitySha256") != segment.get("voiceSynthesisIdentityHash"):
                raise VoiceoverValidationError("segment attempt 未绑定 current synthesis identity")
            if attempt.get("providerTextPromptSha256") != provider_prompt_sha:
                raise VoiceoverValidationError("segment attempt 未绑定 current text_prompt SHA-256")
            candidate_file = _validate_relative_file(
                attempt.get("candidateFile"), label="segment.currentAttempt.candidateFile"
            )
            if not candidate_file.startswith(".work/"):
                raise VoiceoverValidationError("segment attempt candidate 必须位于项目 .work/")
            formal_file = _validate_relative_file(
                attempt.get("formalFile"), label="segment.currentAttempt.formalFile"
            )
            if formal_file != segment.get("relativePath"):
                raise VoiceoverValidationError("segment attempt formalFile 与正式路径不一致")
            if attempt.get("externalOutcome") not in {
                "not_started", "requesting", "succeeded", "failed", "cancelled", "unknown"
            }:
                raise VoiceoverValidationError("segment attempt externalOutcome 无效")
            candidate_sha = attempt.get("candidateSha256")
            candidate_bytes = attempt.get("candidateBytes")
            if candidate_sha is not None:
                _require_sha256(candidate_sha, label="segment.currentAttempt.candidateSha256")
            if candidate_bytes is not None and (
                isinstance(candidate_bytes, bool)
                or not isinstance(candidate_bytes, int)
                or candidate_bytes <= 0
            ):
                raise VoiceoverValidationError("segment.currentAttempt.candidateBytes 必须为正整数")
            receipt = attempt.get("validatorReceipt")
            if receipt is not None and not isinstance(receipt, Mapping):
                raise VoiceoverValidationError("segment.currentAttempt.validatorReceipt 必须是对象或 null")
            provider_receipt = attempt.get("providerReceipt")
            if provider_receipt is not None and not isinstance(provider_receipt, Mapping):
                raise VoiceoverValidationError("segment.currentAttempt.providerReceipt 必须是对象或 null")
            provider_subtitles_required = attempt.get("providerSubtitlesRequired")
            if provider_subtitles_required is not None and not isinstance(
                provider_subtitles_required, bool
            ):
                raise VoiceoverValidationError(
                    "segment.currentAttempt.providerSubtitlesRequired 必须是布尔值"
                )
            provider_subtitles = attempt.get("providerSubtitles")
            if provider_subtitles is not None and not isinstance(
                provider_subtitles, Mapping
            ):
                raise VoiceoverValidationError(
                    "segment.currentAttempt.providerSubtitles 必须是对象或 null"
                )
            if (
                provider_subtitles_required is True
                and segment.get("status") in {"candidate_ready", "publishing", "validated"}
                and not isinstance(provider_subtitles, Mapping)
            ):
                raise VoiceoverValidationError(
                    "需要 provider 字幕的 segment attempt 缺少字幕 receipt"
                )
        segment_provider_subtitles = segment.get("providerSubtitles")
        if segment_provider_subtitles is not None and not isinstance(
            segment_provider_subtitles, Mapping
        ):
            raise VoiceoverValidationError(
                "segment.providerSubtitles 必须是对象或 null"
            )
        if (
            segment.get("status") == "validated"
            and isinstance(attempt, Mapping)
            and attempt.get("providerSubtitlesRequired") is True
            and not isinstance(segment_provider_subtitles, Mapping)
        ):
            raise VoiceoverValidationError(
                "需要 provider 字幕的 validated segment 缺少正式字幕 binding"
            )
    sample = manifest.get("sample")
    full_approval = manifest.get("fullApproval")
    if not isinstance(sample, Mapping) or not isinstance(sample.get("approval"), Mapping):
        raise VoiceoverValidationError("sample approval 结构无效")
    sample_status = sample.get("status")
    if sample_status not in SAMPLE_STATUSES:
        raise VoiceoverValidationError("sample.status 无效")
    if not isinstance(sample["approval"].get("approved"), bool):
        raise VoiceoverValidationError("sample.approval.approved 必须是布尔值")
    sample_prompt_sha = sample.get("providerTextPromptSha256")
    if sample_prompt_sha is not None:
        _require_sha256(
            sample_prompt_sha, label="sample.providerTextPromptSha256"
        )
    if sample_status == "validated":
        _require_sha256(sample.get("identityHash"), label="sample.identityHash")
        if not isinstance(sample.get("media"), Mapping):
            raise VoiceoverValidationError("validated sample 缺少 media")
    elif sample_status == "unknown_external_outcome":
        if sample.get("identityHash") is not None or sample.get("media") is not None:
            raise VoiceoverValidationError(
                "unknown_external_outcome sample 不得伪造 identity 或 media"
            )
        if sample["approval"].get("approved") is not False:
            raise VoiceoverValidationError(
                "unknown_external_outcome sample 不得保留批准"
            )
        failure = sample.get("failure")
        if not isinstance(failure, Mapping):
            raise VoiceoverValidationError(
                "unknown_external_outcome sample 缺少 failure"
            )
        reason_code = failure.get("reasonCode")
        if (
            failure.get("stage") != "provider_evidence"
            or not isinstance(reason_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", reason_code) is None
            or failure.get("providerResponseReceived") is not True
            or failure.get("externalResultIncomplete") is not True
            or failure.get("retryAllowed") is not False
        ):
            raise VoiceoverValidationError(
                "unknown_external_outcome sample.failure 结构无效"
            )
    if not isinstance(manifest.get("runs"), list):
        raise VoiceoverValidationError("voice manifest runs 必须是数组")
    if not isinstance(full_approval, Mapping) or not isinstance(full_approval.get("approved"), bool):
        raise VoiceoverValidationError("fullApproval.approved 必须是布尔值")
    # Technical validation is deliberately independent from human approval.
    if sample["approval"]["approved"]:
        identity = sample["approval"].get("identityHash")
        _require_sha256(identity, label="sample approval identity")
        if identity != sample.get("identityHash"):
            raise VoiceoverValidationError("sample approval identity 与 current sample 不一致")
        sample_basis = sample["approval"].get("approvalBasis")
        if sample_basis is not None and sample_basis not in SAMPLE_APPROVAL_BASES:
            raise VoiceoverValidationError("sample approvalBasis 无效")
    if full_approval["approved"]:
        full_identity = full_approval.get("identityHash")
        _require_sha256(full_identity, label="full approval identity")
        review_policy = full_approval.get("reviewPolicy")
        if review_policy is not None and review_policy not in REVIEW_POLICIES:
            raise VoiceoverValidationError(
                "已批准的 fullApproval.reviewPolicy 必须是 user_first 或 agent_first"
            )
        approval_basis = full_approval.get("approvalBasis")
        review_basis = full_approval.get("reviewBasis")
        if approval_basis is not None and approval_basis not in FULL_APPROVAL_BASES:
            raise VoiceoverValidationError("fullApproval.approvalBasis 无效")
        if review_basis is not None and review_basis not in FULL_REVIEW_BASES:
            raise VoiceoverValidationError("fullApproval.reviewBasis 无效")
        if approval_basis == "technical_after_user_sample" and review_basis != (
            "user_joint_initial_sample_authorization_and_current_technical_validation"
        ):
            raise VoiceoverValidationError("技术自主 fullApproval 缺少对应 reviewBasis")
        if manifest.get("fullIdentityHash") is not None and full_identity != manifest.get("fullIdentityHash"):
            raise VoiceoverValidationError("full approval identity 与 current full identity 不一致")

    if voice_plan is not None:
        plan = validate_voice_plan(voice_plan)
        if manifest["projectId"] != plan["projectId"]:
            raise VoiceoverValidationError("manifest/plan projectId 不一致")
        if voice_plan_ref["voicePlanAuditHash"] != voice_plan_audit_hash(plan):
            raise VoiceoverValidationError("voicePlanAuditHash 与 current plan 不一致")
        if dict(source) != plan["source"]:
            raise VoiceoverValidationError("manifest source 与 current plan 不一致")
    if speech_units is not None:
        if len(speech_units) != len(segments):
            raise VoiceoverValidationError("segment 数量与 speech units 不一致")
        for unit, segment in zip(speech_units, segments):
            if segment["voiceSynthesisIdentityHash"] != unit.get("voiceSynthesisIdentityHash"):
                raise VoiceoverValidationError("segment synthesis identity 与 speech unit 不一致")
            if segment.get("providerTextPromptSha256") != unit.get(
                "providerTextPromptSha256"
            ):
                raise VoiceoverValidationError(
                    "segment text_prompt SHA-256 与 speech unit 不一致"
                )
    return manifest


def cancellation_requested(token: object | None) -> bool:
    """Interpret common cancellation token shapes without owning their type."""

    if token is None:
        return False
    for name in ("is_cancelled", "cancelled", "is_set"):
        member = getattr(token, name, None)
        if callable(member):
            try:
                return bool(member())
            except TypeError:
                continue
        if isinstance(member, bool):
            return member
    return False


class FakeProviderAdapter:
    """Deterministic no-network adapter implementing the frozen protocol."""

    def __init__(
        self,
        response_bytes: bytes = b"fake-audio",
        declared_format: str = "audio/mpeg",
        provider_request_id: str | None = "fake-request",
        *,
        outcomes: Sequence[
            RawAudioResult
            | BaseException
            | Callable[[SynthesisRequest], RawAudioResult]
        ]
        | None = None,
    ) -> None:
        self._default = RawAudioResult(response_bytes, declared_format, provider_request_id)
        self._outcomes = list(outcomes or [])
        self.requests: list[SynthesisRequest] = []
        self._lock = threading.Lock()

    def synthesize(self, request: SynthesisRequest) -> RawAudioResult:
        if not isinstance(request, SynthesisRequest):
            raise PermanentProviderError("fake provider 收到无效 SynthesisRequest")
        if cancellation_requested(request.cancellationToken):
            raise CancelledError("语音请求已取消")
        with self._lock:
            self.requests.append(request)
            outcome = self._outcomes.pop(0) if self._outcomes else self._default
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome(request)
        if not isinstance(outcome, RawAudioResult):
            raise PermanentProviderError("fake provider outcome 必须返回 RawAudioResult")
        return outcome


__all__ = [
    "CancelledError",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_PROVIDER_CONTRACT_VERSION",
    "FULL_TRACK_SEGMENTATION",
    "FULL_TRACK_SEGMENTATION_CONTRACT_VERSION",
    "DEFAULT_SEGMENTATION",
    "EdgeTtsAdapter",
    "FakeProviderAdapter",
    "PermanentProviderError",
    "ProviderAdapter",
    "ProviderError",
    "RawAudioResult",
    "RetryableProviderError",
    "SAMPLE_STATUSES",
    "SEGMENTATION_CONTRACT_VERSION",
    "SEGMENT_STATUSES",
    "SUPPORTED_AUDIO_PROVIDERS",
    "SUPPORTED_PROVIDER_PROTOCOLS",
    "SynthesisRequest",
    "VoiceoverValidationError",
    "bind_synthesis_identities",
    "build_speech_units",
    "build_voice_plan",
    "cancellation_requested",
    "canonical_json_bytes",
    "create_voice_manifest",
    "normalize_pitch",
    "normalize_rate",
    "normalize_volume",
    "plan_speech_units",
    "plan_full_track_unit",
    "sha256_json",
    "sha256_text",
    "source_text_identity_hash",
    "source_timing_identity_hash",
    "synthesis_settings_from_plan",
    "validate_speech_units",
    "validate_voice_manifest",
    "validate_voice_plan",
    "voice_plan_audit_hash",
]

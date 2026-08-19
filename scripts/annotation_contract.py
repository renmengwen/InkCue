"""Canonical validation and preview normalization for annotation elements.

This module intentionally contains only the visual-elements contract.  Scene
identity, timing-plan bindings and publication remain coordinator concerns.
All consumers (candidate validation, formal timing validation and preview
rendering) can therefore share the same structural checks without allowing a
preview implementation to silently invent a different schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


VISUAL_ELEMENTS_CONTRACT_V1 = "whiteboard-annotation-visual-elements-v1"
VISUAL_ELEMENTS_CONTRACT_V2 = "whiteboard-annotation-visual-elements-v2"
SUPPORTED_VISUAL_ELEMENTS_CONTRACTS = {
    VISUAL_ELEMENTS_CONTRACT_V1,
    VISUAL_ELEMENTS_CONTRACT_V2,
}

ELEMENT_FIELDS = {
    "id",
    "sequence",
    "region",
    "reveal",
    "label",
    "narrativeRole",
    "subtitle",
    "handPath",
}
REVEAL_FIELDS = {"startMs", "durationMs", "protectedRegions", "direction"}
REGION_FIELDS = {"x", "y", "width", "height"}
HAND_PATH_FIELDS = {"start", "end", "easing"}
PROTECTED_REGION_FIELDS = REGION_FIELDS
REVEAL_DIRECTIONS = {
    "left-to-right",
    "right-to-left",
    "top-to-bottom",
    "bottom-to-top",
}
LEGACY_DIRECTION_ALIASES = {
    "left_to_right": "left-to-right",
    "right_to_left": "right-to-left",
    "top_to_bottom": "top-to-bottom",
    "bottom_to_top": "bottom-to-top",
}


class AnnotationContractError(ValueError):
    """Raised when visual-elements data violates the canonical contract."""


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnnotationContractError(f"{label} 必须是整数")
    return value


def _rect(value: Any, label: str, *, canvas: Mapping[str, Any] | None = None) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise AnnotationContractError(f"{label} 必须是对象")
    unknown = set(value) - REGION_FIELDS
    if unknown:
        raise AnnotationContractError(f"{label} 包含未知字段: {', '.join(sorted(unknown))}")
    out = {key: _integer(value.get(key), f"{label}.{key}") for key in REGION_FIELDS}
    if out["x"] < 0 or out["y"] < 0 or out["width"] <= 0 or out["height"] <= 0:
        raise AnnotationContractError(f"{label} 必须位于正向非空区域")
    if canvas is not None:
        width = _integer(canvas.get("width"), "canvas.width")
        height = _integer(canvas.get("height"), "canvas.height")
        if out["x"] + out["width"] > width or out["y"] + out["height"] > height:
            raise AnnotationContractError(f"{label} 越出 annotation canvas")
    return out


def _point(value: Any, label: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise AnnotationContractError(f"{label} 必须是 [x, y]")
    return [_integer(value[0], f"{label}[0]"), _integer(value[1], f"{label}[1]")]


def validate_visual_elements(
    elements: Any,
    *,
    canvas: Mapping[str, Any] | None = None,
    scene_duration_ms: int | None = None,
    max_elements: int = 3,
) -> list[dict[str, Any]]:
    """Validate and return a deep copy of visual elements.

    Optional ``label`` and ``handPath`` are intentionally accepted: preview
    consumers can derive them deterministically.  All structural fields and
    unknown keys are checked here so downstream consumers share one contract.
    """

    if not isinstance(elements, list) or not elements:
        raise AnnotationContractError("annotation elements 必须是非空数组")
    if max_elements and len(elements) > max_elements:
        raise AnnotationContractError(f"annotation elements 最多允许 {max_elements} 个")
    out: list[dict[str, Any]] = []
    previous_end = 0
    for index, raw in enumerate(elements, start=1):
        if not isinstance(raw, Mapping):
            raise AnnotationContractError(f"element-{index} 必须是对象")
        unknown = set(raw) - ELEMENT_FIELDS
        if unknown:
            raise AnnotationContractError(
                f"element-{index} 包含未知字段: {', '.join(sorted(unknown))}"
            )
        sequence = _integer(raw.get("sequence"), f"element-{index}.sequence")
        if sequence != index:
            raise AnnotationContractError("annotation element sequence 必须从 1 起连续")
        region = _rect(raw.get("region"), f"element-{index}.region", canvas=canvas)
        reveal_raw = raw.get("reveal")
        if not isinstance(reveal_raw, Mapping):
            raise AnnotationContractError(f"element-{index} 缺少 reveal")
        reveal_unknown = set(reveal_raw) - REVEAL_FIELDS
        if reveal_unknown:
            raise AnnotationContractError(
                f"element-{index}.reveal 包含未知字段: {', '.join(sorted(reveal_unknown))}"
            )
        start = _integer(reveal_raw.get("startMs"), f"element-{index}.reveal.startMs")
        duration = _integer(reveal_raw.get("durationMs"), f"element-{index}.reveal.durationMs")
        if start < 0 or duration <= 0:
            raise AnnotationContractError(f"element-{index} reveal 必须使用正时长的场景局部毫秒")
        end = start + duration
        if start < previous_end:
            raise AnnotationContractError("annotation elements 必须按场景局部时间串行且不重叠")
        if scene_duration_ms is not None and end > scene_duration_ms - 500:
            raise AnnotationContractError(
                f"element-{index} 结束于 {end}ms，超过 sceneDurationMs - 500 ({scene_duration_ms - 500}ms)"
            )
        previous_end = end
        direction = reveal_raw.get("direction", "left-to-right")
        if not isinstance(direction, str) or direction not in REVEAL_DIRECTIONS:
            raise AnnotationContractError(f"element-{index}.reveal.direction 不受支持")
        protected_raw = reveal_raw.get("protectedRegions", [])
        if not isinstance(protected_raw, list):
            raise AnnotationContractError(f"element-{index} protectedRegions 必须是数组")
        protected = [
            _rect(item, f"element-{index}.protectedRegions[{pidx}]", canvas=canvas)
            for pidx, item in enumerate(protected_raw, start=1)
        ]
        reveal = dict(reveal_raw)
        if "direction" in reveal_raw:
            reveal["direction"] = direction
        if "protectedRegions" in reveal_raw:
            reveal["protectedRegions"] = protected
        element = dict(raw)
        element["sequence"] = sequence
        element["region"] = region
        element["reveal"] = reveal
        if "handPath" in element:
            hand = element["handPath"]
            if hand is not None:
                if not isinstance(hand, Mapping) or set(hand) - HAND_PATH_FIELDS:
                    raise AnnotationContractError(f"element-{index}.handPath 字段不受支持")
                element["handPath"] = {
                    "start": _point(hand.get("start"), f"element-{index}.handPath.start"),
                    "end": _point(hand.get("end"), f"element-{index}.handPath.end"),
                }
                if "easing" in hand:
                    if not isinstance(hand["easing"], str) or not hand["easing"].strip():
                        raise AnnotationContractError(f"element-{index}.handPath.easing 必须是非空字符串")
                    element["handPath"]["easing"] = hand["easing"]
        for field in ("id", "label", "narrativeRole", "subtitle"):
            if field in element and element[field] is not None and not isinstance(element[field], str):
                raise AnnotationContractError(f"element-{index}.{field} 必须是字符串")
        out.append(element)
    return out


def normalize_legacy_visual_elements(
    elements: Any,
    *,
    canvas: Mapping[str, Any] | None = None,
    scene_duration_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Translate the explicitly supported historical formal annotation shape.

    This is deliberately separate from the child candidate validator.  It is
    intended only for the existing schema-v1, explicit read-only compatibility
    gate and must never be used to accept a new annotationDrafting candidate.
    """

    if not isinstance(elements, list):
        raise AnnotationContractError("annotation elements 必须是非空数组")
    translated = deepcopy(elements)
    for raw in translated:
        if not isinstance(raw, dict):
            continue
        # Historical authored examples carried renderer hints that the current
        # runtime does not consume.  Drop only this documented legacy pair.
        raw.pop("type", None)
        reveal = raw.get("reveal")
        if isinstance(reveal, dict):
            reveal.pop("maskPaddingPx", None)
            direction = reveal.get("direction")
            if direction in LEGACY_DIRECTION_ALIASES:
                reveal["direction"] = LEGACY_DIRECTION_ALIASES[direction]
    return validate_visual_elements(
        translated,
        canvas=canvas,
        scene_duration_ms=scene_duration_ms,
        max_elements=0,
    )


def derive_preview_label(element: Mapping[str, Any], index: int) -> str:
    for field in ("label", "narrativeRole", "subtitle"):
        value = element.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"元素 {index}"


def derive_preview_hand_path(element: Mapping[str, Any]) -> tuple[tuple[int, int], tuple[int, int]]:
    authored = element.get("handPath")
    if isinstance(authored, Mapping):
        start, end = authored.get("start"), authored.get("end")
        if isinstance(start, Sequence) and isinstance(end, Sequence) and len(start) == 2 and len(end) == 2:
            return (int(start[0]), int(start[1])), (int(end[0]), int(end[1]))
    region = element["region"]
    x, y, width, height = (region[key] for key in ("x", "y", "width", "height"))
    inset_x = min(max(12, width // 10), max(12, width // 3))
    inset_y = min(max(12, height // 10), max(12, height // 3))
    left, right = x + inset_x, x + width - inset_x
    top, bottom = y + inset_y, y + height - inset_y
    center_x, center_y = x + width // 2, y + height // 2
    direction = element.get("reveal", {}).get("direction", "left-to-right")
    if direction == "right-to-left":
        return (right, center_y), (left, center_y)
    if direction == "top-to-bottom":
        return (center_x, top), (center_x, bottom)
    if direction == "bottom-to-top":
        return (center_x, bottom), (center_x, top)
    return (left, center_y), (right, center_y)


def normalize_visual_elements(
    elements: Any,
    *,
    canvas: Mapping[str, Any] | None = None,
    scene_duration_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Validate and return preview-ready elements with derived metadata.

    Derived label/path are returned transiently and should not be written back
    to formal annotation files unless the caller explicitly chooses to do so.
    """

    normalized = validate_visual_elements(
        elements,
        canvas=canvas,
        scene_duration_ms=scene_duration_ms,
    )
    for index, element in enumerate(normalized, start=1):
        element["_previewLabel"] = derive_preview_label(element, index)
        element["_previewHandPath"] = derive_preview_hand_path(element)
    return normalized

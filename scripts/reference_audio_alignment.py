"""Align an authoritative reference SRT to sentence-level ASR timing.

The reference text always wins.  ASR text is used only to locate acoustic
boundaries in that text.  The first version deliberately fails closed when
there are not enough real ASR boundaries to preserve every confirmed source
cue boundary; it never manufactures timing from character proportions.
"""

from __future__ import annotations

import bisect
import difflib
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from .srt_timeline import SrtValidationError, parse_srt
except ImportError:  # direct scripts import
    from srt_timeline import SrtValidationError, parse_srt


DEFAULT_MIN_MATCH_RATIO = 0.72
DEFAULT_MAX_NORMALIZED_EDIT_RATIO = 0.35
_EXACT_EDIT_DISTANCE_CELL_LIMIT = 4_000_000


class ReferenceAlignmentError(ValueError):
    """The ASR timing cannot safely be bound to the reference transcript."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def _normalised_characters(text: str) -> list[str]:
    normalised = unicodedata.normalize("NFKC", text).casefold()
    return [character for character in normalised if character.isalnum()]


def normalise_alignment_text(text: str) -> str:
    """Return the punctuation-insensitive form used for alignment checks."""

    return "".join(_normalised_characters(text))


def _raw_boundaries(text: str) -> tuple[str, list[int]]:
    """Return normalised text and raw offsets for its semantic boundaries.

    Punctuation between two spoken characters stays with the preceding span,
    which preserves natural Chinese subtitle punctuation when a cue is split.
    """

    characters: list[str] = []
    raw_starts: list[int] = []
    for raw_offset, raw_character in enumerate(text):
        for character in _normalised_characters(raw_character):
            characters.append(character)
            raw_starts.append(raw_offset)
    if not characters:
        return "", [0]
    return "".join(characters), [0, *raw_starts[1:], len(text)]


def _levenshtein_distance(left: str, right: str) -> tuple[int, str]:
    if left == right:
        return 0, "exact"
    if not left:
        return len(right), "exact"
    if not right:
        return len(left), "exact"
    if len(left) * len(right) > _EXACT_EDIT_DISTANCE_CELL_LIMIT:
        matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        return max(len(left), len(right)) - matched, "matching-block-upper-bound"
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for right_index, right_character in enumerate(right, start=1):
        current = [right_index]
        for left_index, left_character in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[left_index] + 1,
                    previous[left_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1], "exact"


def _asr_to_reference_boundary_map(asr_text: str, reference_text: str) -> list[int]:
    """Map every ASR character boundary to a monotonic reference boundary."""

    matcher = difflib.SequenceMatcher(None, asr_text, reference_text, autojunk=False)
    mapped: list[int | None] = [None] * (len(asr_text) + 1)
    for tag, asr_start, asr_end, ref_start, ref_end in matcher.get_opcodes():
        asr_length = asr_end - asr_start
        ref_length = ref_end - ref_start
        if tag == "equal":
            for offset in range(asr_length + 1):
                mapped[asr_start + offset] = ref_start + offset
        elif asr_length == 0:
            mapped[asr_start] = ref_end
        elif ref_length == 0:
            for offset in range(asr_length + 1):
                mapped[asr_start + offset] = ref_start
        else:
            for offset in range(asr_length + 1):
                mapped[asr_start + offset] = round(
                    ref_start + ref_length * offset / asr_length
                )

    mapped[0] = 0
    mapped[-1] = len(reference_text)
    previous = 0
    resolved: list[int] = []
    for value in mapped:
        current = previous if value is None else max(previous, int(value))
        current = min(current, len(reference_text))
        resolved.append(current)
        previous = current
    resolved[-1] = len(reference_text)
    return resolved


def _select_distinct_acoustic_boundaries(
    mapped_positions: Sequence[int], required_positions: Sequence[int]
) -> list[int]:
    """Assign every source cue boundary to a distinct real ASR boundary."""

    internal_count = len(mapped_positions) - 2
    required_count = len(required_positions)
    if required_count > internal_count:
        raise ReferenceAlignmentError(
            "ASR 有效句级边界少于原稿 cue 边界，无法在不估算时间的前提下保持原稿 cue"
        )
    if not required_positions:
        return []

    infinity = float("inf")
    # Dynamic programming over "use or skip this acoustic boundary".
    costs = [[infinity] * (internal_count + 1) for _ in range(required_count + 1)]
    takes = [[False] * (internal_count + 1) for _ in range(required_count + 1)]
    for acoustic_index in range(internal_count + 1):
        costs[0][acoustic_index] = 0.0
    for required_index in range(1, required_count + 1):
        for acoustic_count in range(1, internal_count + 1):
            skip = costs[required_index][acoustic_count - 1]
            take = costs[required_index - 1][acoustic_count - 1] + abs(
                mapped_positions[acoustic_count] - required_positions[required_index - 1]
            )
            if take < skip:
                costs[required_index][acoustic_count] = take
                takes[required_index][acoustic_count] = True
            else:
                costs[required_index][acoustic_count] = skip

    selected: list[int] = []
    required_index = required_count
    acoustic_count = internal_count
    while required_index:
        if acoustic_count <= 0:
            raise ReferenceAlignmentError("无法为全部原稿 cue 找到递增的 ASR 声学边界")
        if takes[required_index][acoustic_count]:
            selected.append(acoustic_count)
            required_index -= 1
        acoustic_count -= 1
    selected.reverse()
    return selected


def _resolve_scene_ranges(
    scene_specs: Sequence[Mapping[str, Any]], reference_cue_count: int
) -> list[tuple[str, int, int]]:
    if not scene_specs:
        raise ReferenceAlignmentError("scene_specs 不能为空")
    ranges: list[tuple[str, int, int]] = []
    expected = 1
    for index, scene in enumerate(scene_specs, start=1):
        if not isinstance(scene, Mapping):
            raise ReferenceAlignmentError(f"scene_specs[{index - 1}] 必须是对象")
        scene_id = scene.get("sceneId", f"scene-{index:02d}")
        raw_range = scene.get("sourceCueRange", scene.get("cueRange"))
        if (
            not isinstance(scene_id, str)
            or not scene_id.strip()
            or not isinstance(raw_range, (list, tuple))
            or len(raw_range) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_range)
        ):
            raise ReferenceAlignmentError(f"scene_specs[{index - 1}] 的 sceneId/cueRange 无效")
        first, last = raw_range
        if first != expected or last < first or last > reference_cue_count:
            raise ReferenceAlignmentError(
                f"{scene_id} 的 cue 范围必须从 {expected} 开始并位于原稿范围内"
            )
        ranges.append((scene_id, first, last))
        expected = last + 1
    if expected != reference_cue_count + 1:
        raise ReferenceAlignmentError("scene_specs 未连续覆盖全部原稿 cue")
    return ranges


def _source_scene_lookup(
    scene_ranges: Sequence[tuple[str, int, int]], reference_cue_count: int
) -> list[str]:
    lookup = [""] * (reference_cue_count + 1)
    for scene_id, first, last in scene_ranges:
        for ordinal in range(first, last + 1):
            lookup[ordinal] = scene_id
    return lookup


def align_reference_audio(
    reference_srt: str,
    asr_srt: str,
    scene_specs: Sequence[Mapping[str, Any]],
    audio_duration_ms: int,
    *,
    min_match_ratio: float = DEFAULT_MIN_MATCH_RATIO,
    max_normalized_edit_ratio: float = DEFAULT_MAX_NORMALIZED_EDIT_RATIO,
) -> dict[str, Any]:
    """Bind authoritative reference text to sentence-level ASR timing.

    The returned cues are continuous from 0 through ``audio_duration_ms``.
    Each cue contains an exact slice of one source cue and therefore never
    crosses a confirmed scene.  Text equality and all timing invariants are
    checked again before returning.
    """

    if (
        isinstance(audio_duration_ms, bool)
        or not isinstance(audio_duration_ms, int)
        or audio_duration_ms <= 0
    ):
        raise ReferenceAlignmentError("audio_duration_ms 必须是正整数")
    if not 0.0 <= min_match_ratio <= 1.0:
        raise ReferenceAlignmentError("min_match_ratio 必须位于 0 到 1")
    if not 0.0 <= max_normalized_edit_ratio <= 1.0:
        raise ReferenceAlignmentError("max_normalized_edit_ratio 必须位于 0 到 1")
    try:
        reference_cues = parse_srt(reference_srt)
        parsed_asr_cues = parse_srt(asr_srt)
    except SrtValidationError as exc:
        raise ReferenceAlignmentError(f"SRT 无效: {exc}") from exc

    scene_ranges = _resolve_scene_ranges(scene_specs, len(reference_cues))
    scene_by_source = _source_scene_lookup(scene_ranges, len(reference_cues))

    reference_parts: list[str] = []
    reference_boundaries = [0]
    raw_boundaries_by_cue: list[list[int]] = []
    for cue in reference_cues:
        normalised, raw_boundaries = _raw_boundaries(cue["text"])
        if not normalised:
            raise ReferenceAlignmentError(
                f"原稿 cue {cue['sourceOrdinal']} 只有标点或空白，无法绑定真实语音"
            )
        reference_parts.append(normalised)
        raw_boundaries_by_cue.append(raw_boundaries)
        reference_boundaries.append(reference_boundaries[-1] + len(normalised))
    reference_text = "".join(reference_parts)

    lexical_asr_cues: list[dict[str, Any]] = []
    ignored_asr_cues: list[int] = []
    asr_parts: list[str] = []
    asr_boundaries = [0]
    for cue in parsed_asr_cues:
        normalised = normalise_alignment_text(cue["text"])
        if not normalised:
            ignored_asr_cues.append(cue["sourceOrdinal"])
            continue
        if cue["endMs"] > audio_duration_ms:
            raise ReferenceAlignmentError(
                f"ASR cue {cue['sourceOrdinal']} 超出音频总时长 {audio_duration_ms}ms"
            )
        lexical_asr_cues.append(cue)
        asr_parts.append(normalised)
        asr_boundaries.append(asr_boundaries[-1] + len(normalised))
    if not lexical_asr_cues:
        raise ReferenceAlignmentError("ASR SRT 没有可对齐的文字 cue")
    asr_text = "".join(asr_parts)

    matcher = difflib.SequenceMatcher(None, asr_text, reference_text, autojunk=False)
    matched_characters = sum(block.size for block in matcher.get_matching_blocks())
    match_ratio = matched_characters / max(len(reference_text), len(asr_text))
    edit_distance, edit_method = _levenshtein_distance(asr_text, reference_text)
    edit_ratio = edit_distance / max(len(reference_text), len(asr_text))
    diagnostics: dict[str, Any] = {
        "status": "PASS",
        "referenceCueCount": len(reference_cues),
        "asrCueCount": len(parsed_asr_cues),
        "lexicalAsrCueCount": len(lexical_asr_cues),
        "ignoredPunctuationAsrCueOrdinals": ignored_asr_cues,
        "referenceNormalizedCharacters": len(reference_text),
        "asrNormalizedCharacters": len(asr_text),
        "matchedCharacters": matched_characters,
        "matchRatio": round(match_ratio, 6),
        "normalizedEditDistance": edit_distance,
        "normalizedEditRatio": round(edit_ratio, 6),
        "editDistanceMethod": edit_method,
        "thresholds": {
            "minMatchRatio": min_match_ratio,
            "maxNormalizedEditRatio": max_normalized_edit_ratio,
        },
        "timingFallbackUsed": False,
    }
    if match_ratio < min_match_ratio or edit_ratio > max_normalized_edit_ratio:
        diagnostics["status"] = "FAIL"
        raise ReferenceAlignmentError(
            "ASR 与已确认原稿的匹配质量过低，拒绝生成推测时间轴",
            diagnostics=diagnostics,
        )

    character_map = _asr_to_reference_boundary_map(asr_text, reference_text)
    mapped_positions = [character_map[position] for position in asr_boundaries]
    required_positions = reference_boundaries[1:-1]
    try:
        selected_boundaries = _select_distinct_acoustic_boundaries(
            mapped_positions, required_positions
        )
    except ReferenceAlignmentError as exc:
        diagnostics["status"] = "FAIL"
        diagnostics["availableInternalAcousticBoundaries"] = len(mapped_positions) - 2
        diagnostics["requiredSourceCueBoundaries"] = len(required_positions)
        raise ReferenceAlignmentError(str(exc), diagnostics=diagnostics) from exc

    snapped = dict(zip(selected_boundaries, required_positions, strict=True))
    snapped_positions = list(mapped_positions)
    for acoustic_index, reference_position in snapped.items():
        snapped_positions[acoustic_index] = reference_position
    anchors = [(0, 0), *sorted(snapped.items()), (len(snapped_positions) - 1, len(reference_text))]
    for (left_index, left_position), (right_index, right_position) in zip(anchors, anchors[1:]):
        previous = left_position
        for acoustic_index in range(left_index + 1, right_index):
            position = min(max(snapped_positions[acoustic_index], previous), right_position)
            snapped_positions[acoustic_index] = position
            previous = position
        snapped_positions[left_index] = left_position
        snapped_positions[right_index] = right_position

    # Acoustic intervals begin at each lexical ASR cue.  Empty mapped intervals
    # are absorbed by the preceding returned cue rather than inventing text.
    pieces: list[dict[str, Any]] = []
    source_ends = reference_boundaries[1:]
    for acoustic_index, (start_position, end_position) in enumerate(
        zip(snapped_positions, snapped_positions[1:])
    ):
        if end_position <= start_position:
            continue
        source_index = bisect.bisect_right(source_ends, start_position)
        if source_index >= len(reference_cues):
            raise ReferenceAlignmentError("内部错误：对齐位置超出原稿")
        source_start = reference_boundaries[source_index]
        source_end = reference_boundaries[source_index + 1]
        if end_position > source_end:
            raise ReferenceAlignmentError("内部错误：最终 cue 跨越原稿 cue 边界")
        local_start = start_position - source_start
        local_end = end_position - source_start
        raw_boundaries = raw_boundaries_by_cue[source_index]
        text = reference_cues[source_index]["text"][
            raw_boundaries[local_start] : raw_boundaries[local_end]
        ]
        if not text:
            raise ReferenceAlignmentError("内部错误：非空语义 span 产生了空字幕")
        pieces.append(
            {
                "acousticIndex": acoustic_index,
                "sourceCueOrdinal": source_index + 1,
                "sceneId": scene_by_source[source_index + 1],
                "text": text,
            }
        )
    if not pieces:
        raise ReferenceAlignmentError("对齐后没有生成任何字幕 cue")

    acoustic_starts = [0] + [cue["startMs"] for cue in lexical_asr_cues[1:]]
    cues: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces, start=1):
        start_ms = 0 if index == 1 else cues[-1]["endMs"]
        end_ms = (
            audio_duration_ms
            if index == len(pieces)
            else acoustic_starts[pieces[index]["acousticIndex"]]
        )
        if end_ms <= start_ms:
            raise ReferenceAlignmentError("ASR 声学边界无法形成递增正时长字幕")
        cues.append(
            {
                "index": index,
                "sourceOrdinal": index,
                "sourceCueOrdinal": piece["sourceCueOrdinal"],
                "sourceCueRange": [piece["sourceCueOrdinal"], piece["sourceCueOrdinal"]],
                "sceneId": piece["sceneId"],
                "startMs": start_ms,
                "endMs": end_ms,
                "text": piece["text"],
            }
        )

    final_raw_text = "".join(cue["text"] for cue in cues)
    reference_raw_text = "".join(cue["text"] for cue in reference_cues)
    if final_raw_text != reference_raw_text:
        raise ReferenceAlignmentError("最终字幕未逐字、按序覆盖权威原稿（含原始标点）")
    if normalise_alignment_text(final_raw_text) != reference_text:
        raise ReferenceAlignmentError("最终字幕规范化文本与权威原稿不一致")
    if cues[0]["startMs"] != 0 or cues[-1]["endMs"] != audio_duration_ms:
        raise ReferenceAlignmentError("最终字幕没有从 0 连续收口到音频总时长")
    for previous, current in zip(cues, cues[1:]):
        if previous["endMs"] != current["startMs"]:
            raise ReferenceAlignmentError("最终字幕时间不连续")
        if previous["sceneId"] == current["sceneId"]:
            continue
        if previous["sourceCueOrdinal"] >= current["sourceCueOrdinal"]:
            raise ReferenceAlignmentError("scene 切换没有遵循原稿 cue 顺序")

    scenes: list[dict[str, Any]] = []
    for scene_id, first_source, last_source in scene_ranges:
        scene_cues = [
            cue
            for cue in cues
            if first_source <= cue["sourceCueOrdinal"] <= last_source
        ]
        if not scene_cues:
            raise ReferenceAlignmentError(f"{scene_id} 没有最终字幕 cue")
        scenes.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [first_source, last_source],
                "narrationCueRange": [scene_cues[0]["index"], scene_cues[-1]["index"]],
                "startMs": scene_cues[0]["startMs"],
                "endMs": scene_cues[-1]["endMs"],
                "sceneDurationMs": scene_cues[-1]["endMs"] - scene_cues[0]["startMs"],
            }
        )
    if scenes[0]["startMs"] != 0 or scenes[-1]["endMs"] != audio_duration_ms:
        raise ReferenceAlignmentError("scene 未覆盖完整音频")
    for previous, current in zip(scenes, scenes[1:]):
        if previous["endMs"] != current["startMs"]:
            raise ReferenceAlignmentError("scene 时间不连续")

    diagnostics["outputCueCount"] = len(cues)
    diagnostics["snappedSourceBoundaries"] = [
        {
            "sourceCueEndOrdinal": index + 1,
            "acousticBoundaryIndex": acoustic_index,
            "timeMs": acoustic_starts[acoustic_index],
        }
        for index, acoustic_index in enumerate(selected_boundaries)
    ]
    return {
        "schemaVersion": 1,
        "cues": cues,
        "scenes": scenes,
        "diagnostics": diagnostics,
    }


__all__ = [
    "DEFAULT_MAX_NORMALIZED_EDIT_RATIO",
    "DEFAULT_MIN_MATCH_RATIO",
    "ReferenceAlignmentError",
    "align_reference_audio",
    "normalise_alignment_text",
]

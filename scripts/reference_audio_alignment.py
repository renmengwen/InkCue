"""Align an authoritative reference SRT to token-level ASR timing.

The reference text always wins.  ASR text is used only to locate acoustic
boundaries in that text. The aligner fails closed when real token boundaries
cannot preserve every semantic caption boundary; it never manufactures timing
from character proportions.
"""

from __future__ import annotations

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
# Legacy public thresholds are retained for import compatibility. The active
# token-alignment path uses the caption-specific limits declared below.
DEFAULT_MAX_CUE_MS_PER_CHARACTER = 1_200.0
DEFAULT_MAX_CUE_RATE_MULTIPLIER = 4.0
DEFAULT_MIN_RATE_OUTLIER_DURATION_MS = 10_000
DEFAULT_MAX_DOMINANT_CUE_DURATION_SHARE = 0.5
DEFAULT_MAX_DOMINANT_CUE_CHARACTER_SHARE = 0.35
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


def _select_distinct_acoustic_boundaries(
    mapped_positions: Sequence[int], required_positions: Sequence[int]
) -> list[int]:
    """Assign every source cue boundary to a distinct real ASR boundary."""

    internal_count = len(mapped_positions) - 2
    required_count = len(required_positions)
    if required_count > internal_count:
        raise ReferenceAlignmentError(
            "ASR 有效 token 边界少于必需的语义字幕边界，"
            "无法在不估算时间的前提下安全切分"
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


def _validate_timing_plausibility(
    cues: Sequence[Mapping[str, Any]],
    reference_cues: Sequence[Mapping[str, Any]],
    audio_duration_ms: int,
    diagnostics: dict[str, Any],
) -> None:
    """拒绝少量原稿占据整轨大部分时长等明显错误声学边界。"""

    character_counts = [
        len(normalise_alignment_text(str(cue["text"]))) for cue in reference_cues
    ]
    total_characters = sum(character_counts)
    overall_ms_per_character = audio_duration_ms / total_characters
    metrics: list[dict[str, Any]] = []
    implausible: list[dict[str, Any]] = []
    for source_ordinal, character_count in enumerate(character_counts, start=1):
        source_cues = [
            cue for cue in cues if cue["sourceCueOrdinal"] == source_ordinal
        ]
        if not source_cues:
            raise ReferenceAlignmentError(
                f"内部错误：原稿 cue {source_ordinal} 没有最终字幕"
            )
        duration_ms = source_cues[-1]["endMs"] - source_cues[0]["startMs"]
        ms_per_character = duration_ms / character_count
        duration_share = duration_ms / audio_duration_ms
        character_share = character_count / total_characters
        reasons: list[str] = []
        if (
            duration_ms >= DEFAULT_MIN_RATE_OUTLIER_DURATION_MS
            and ms_per_character
            > max(
                DEFAULT_MAX_CUE_MS_PER_CHARACTER,
                overall_ms_per_character * DEFAULT_MAX_CUE_RATE_MULTIPLIER,
            )
        ):
            reasons.append("reading_rate_outlier")
        if (
            len(reference_cues) > 1
            and duration_share > DEFAULT_MAX_DOMINANT_CUE_DURATION_SHARE
            and character_share < DEFAULT_MAX_DOMINANT_CUE_CHARACTER_SHARE
        ):
            reasons.append("disproportionate_track_share")
        metric = {
            "sourceCueOrdinal": source_ordinal,
            "durationMs": duration_ms,
            "normalizedCharacters": character_count,
            "msPerCharacter": round(ms_per_character, 3),
            "durationShare": round(duration_share, 6),
            "characterShare": round(character_share, 6),
        }
        metrics.append(metric)
        if reasons:
            implausible.append({**metric, "reasons": reasons})

    diagnostics["timingPlausibility"] = {
        "overallMsPerCharacter": round(overall_ms_per_character, 3),
        "sourceCues": metrics,
        "implausibleSourceCues": implausible,
        "thresholds": {
            "maxCueMsPerCharacter": DEFAULT_MAX_CUE_MS_PER_CHARACTER,
            "maxCueRateMultiplier": DEFAULT_MAX_CUE_RATE_MULTIPLIER,
            "minRateOutlierDurationMs": DEFAULT_MIN_RATE_OUTLIER_DURATION_MS,
            "maxDominantCueDurationShare": DEFAULT_MAX_DOMINANT_CUE_DURATION_SHARE,
            "maxDominantCueCharacterShare": DEFAULT_MAX_DOMINANT_CUE_CHARACTER_SHARE,
        },
    }
    if implausible:
        diagnostics["status"] = "FAIL"
        first = implausible[0]
        raise ReferenceAlignmentError(
            "ASR 声学边界明显失真："
            f"原稿 cue {first['sourceCueOrdinal']} 的 {first['durationMs']}ms "
            "与文本长度不相称",
            diagnostics=diagnostics,
        )


_CAPTION_BREAKS = frozenset("，,。.!！？?；;：:")
_CAPTION_CLOSERS = frozenset("”’）》】〉」』")
_CAPTION_TARGET_TOKENS = 28
_CAPTION_HARD_MAX_TOKENS = 48
_MAX_BOUNDARY_DISPLACEMENT_TOKENS = 12
_MAX_CAPTION_CHARACTERS_PER_SECOND = 9.0
# 单向上限只能发现 token 被压缩；30 秒连续语音回归曾把块前半段拉慢、
# 后半段追平而仍低于上限。下限与同窗口规模最快/最慢比共同拒绝这种失真。
_MIN_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND = 2.0
_MAX_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND = 8.5
_MAX_LOCAL_ACOUSTIC_RATE_VARIATION_RATIO = 3.0
_LOCAL_ACOUSTIC_WINDOW_TOKENS = (16, 32, 48)


def _alignment_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    ascii_buffer: list[str] = []

    def flush_ascii() -> None:
        if ascii_buffer:
            tokens.append("".join(ascii_buffer).casefold())
            ascii_buffer.clear()

    for character in unicodedata.normalize("NFKC", text):
        if character.isascii() and character.isalnum():
            ascii_buffer.append(character)
            continue
        flush_ascii()
        if "\u3400" <= character <= "\u9fff":
            tokens.append(character)
        elif character.isalnum():
            tokens.append(character.casefold())
    flush_ascii()
    return tokens


def _caption_atoms(text: str) -> list[str]:
    atoms: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in _CAPTION_BREAKS:
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in _CAPTION_CLOSERS:
            end += 1
        atoms.append(text[start:end])
        start = end
        index = end
    if start < len(text):
        atoms.append(text[start:])
    return [atom for atom in atoms if atom]


def _caption_spans(text: str, *, source_ordinal: int) -> list[str]:
    atoms = _caption_atoms(text)
    if not atoms:
        raise ReferenceAlignmentError(f"原稿 cue {source_ordinal} 没有可朗读文字")
    for atom in atoms:
        if len(_alignment_tokens(atom)) > _CAPTION_HARD_MAX_TOKENS:
            raise ReferenceAlignmentError(
                f"原稿 cue {source_ordinal} 存在超过 {_CAPTION_HARD_MAX_TOKENS} token 且无安全标点的长句；"
                "拒绝从词语中间估算切分"
            )

    spans: list[str] = []
    current = ""
    for atom in atoms:
        combined = current + atom
        if current and len(_alignment_tokens(combined)) > _CAPTION_TARGET_TOKENS:
            spans.append(current)
            current = atom
        else:
            current = combined
    if current:
        spans.append(current)
    if "".join(spans) != text:
        raise ReferenceAlignmentError("内部错误：语义字幕切分未逐字覆盖原稿")
    return spans


def _token_boundary_map(asr_tokens: Sequence[str], reference_tokens: Sequence[str]) -> list[int]:
    matcher = difflib.SequenceMatcher(None, asr_tokens, reference_tokens, autojunk=False)
    mapped: list[int | None] = [None] * (len(asr_tokens) + 1)
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
    mapped[-1] = len(reference_tokens)
    previous = 0
    resolved: list[int] = []
    for value in mapped:
        current = previous if value is None else max(previous, int(value))
        current = min(current, len(reference_tokens))
        resolved.append(current)
        previous = current
    resolved[-1] = len(reference_tokens)
    return resolved


def _validate_local_acoustic_rate(
    lexical_asr_cues: Sequence[Mapping[str, Any]],
    diagnostics: dict[str, Any],
) -> None:
    """用滑动 token 窗口拒绝局部拉伸、压缩和长段累计漂移。"""

    fast_outliers: list[dict[str, Any]] = []
    slow_outliers: list[dict[str, Any]] = []
    variation_outliers: list[dict[str, Any]] = []
    maximum_rate = 0.0
    maximum_window: dict[str, Any] | None = None
    minimum_rate: float | None = None
    minimum_window: dict[str, Any] | None = None
    for window_tokens in _LOCAL_ACOUSTIC_WINDOW_TOKENS:
        window_metrics: list[dict[str, Any]] = []
        if len(lexical_asr_cues) < window_tokens:
            continue
        for start_index in range(0, len(lexical_asr_cues) - window_tokens + 1):
            window = lexical_asr_cues[start_index : start_index + window_tokens]
            start_ms = window[0]["startMs"]
            end_ms = window[-1]["endMs"]
            duration_ms = end_ms - start_ms
            if duration_ms <= 0:
                raise ReferenceAlignmentError("ASR 局部 token 窗口无法形成正时长")
            semantic_characters = sum(
                len(normalise_alignment_text(str(cue["text"]))) for cue in window
            )
            rate = semantic_characters * 1000 / duration_ms
            metric = {
                "firstAsrCueOrdinal": window[0]["sourceOrdinal"],
                "lastAsrCueOrdinal": window[-1]["sourceOrdinal"],
                "windowTokens": window_tokens,
                "semanticCharacters": semantic_characters,
                "startMs": start_ms,
                "endMs": end_ms,
                "durationMs": duration_ms,
                "charactersPerSecond": round(rate, 3),
            }
            if rate > maximum_rate:
                maximum_rate = rate
                maximum_window = metric
            if minimum_rate is None or rate < minimum_rate:
                minimum_rate = rate
                minimum_window = metric
            if rate > _MAX_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND:
                fast_outliers.append(metric)
            if rate < _MIN_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND:
                slow_outliers.append(metric)
            window_metrics.append(metric)

        if window_metrics:
            slowest = min(window_metrics, key=lambda item: item["charactersPerSecond"])
            fastest = max(window_metrics, key=lambda item: item["charactersPerSecond"])
            variation_ratio = (
                fastest["charactersPerSecond"] / slowest["charactersPerSecond"]
            )
            if variation_ratio > _MAX_LOCAL_ACOUSTIC_RATE_VARIATION_RATIO:
                variation_outliers.append(
                    {
                        "windowTokens": window_tokens,
                        "ratio": round(variation_ratio, 3),
                        "slowest": slowest,
                        "fastest": fastest,
                    }
                )

    diagnostics["localAcousticRate"] = {
        "windowTokenCounts": list(_LOCAL_ACOUSTIC_WINDOW_TOKENS),
        "minCharactersPerSecond": _MIN_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND,
        "maxCharactersPerSecond": _MAX_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND,
        "maxVariationRatio": _MAX_LOCAL_ACOUSTIC_RATE_VARIATION_RATIO,
        "observedMinCharactersPerSecond": (
            round(minimum_rate, 3) if minimum_rate is not None else None
        ),
        "observedMinWindow": minimum_window,
        "observedMaxCharactersPerSecond": round(maximum_rate, 3),
        "observedMaxWindow": maximum_window,
        "rateFloorPassed": not slow_outliers,
        "rateCeilingPassed": not fast_outliers,
        "rateVariationPassed": not variation_outliers,
        "slowOutlierCount": len(slow_outliers),
        "fastOutlierCount": len(fast_outliers),
        "variationOutlierCount": len(variation_outliers),
        "outlierCount": (
            len(slow_outliers) + len(fast_outliers) + len(variation_outliers)
        ),
        "slowOutliers": slow_outliers[:20],
        "fastOutliers": fast_outliers[:20],
        "variationOutliers": variation_outliers[:20],
    }
    if slow_outliers or fast_outliers or variation_outliers:
        diagnostics["status"] = "FAIL"
        if slow_outliers:
            first = slow_outliers[0]
            reason = (
                f"{first['startMs']}–{first['endMs']}ms 的 "
                f"{first['charactersPerSecond']} chars/s 低于下限"
            )
        elif fast_outliers:
            first = fast_outliers[0]
            reason = (
                f"{first['startMs']}–{first['endMs']}ms 的 "
                f"{first['charactersPerSecond']} chars/s 超过上限"
            )
        else:
            first = variation_outliers[0]
            reason = (
                f"{first['windowTokens']}-token 窗口的最快/最慢语速比 "
                f"{first['ratio']} 超过上限"
            )
        raise ReferenceAlignmentError(
            f"ASR token 时间存在局部异常拉伸、压缩或累计漂移：{reason}",
            diagnostics=diagnostics,
        )


def align_reference_audio(
    reference_srt: str,
    asr_srt: str,
    scene_specs: Sequence[Mapping[str, Any]],
    audio_duration_ms: int,
    *,
    min_match_ratio: float = DEFAULT_MIN_MATCH_RATIO,
    max_normalized_edit_ratio: float = DEFAULT_MAX_NORMALIZED_EDIT_RATIO,
    timing_validation_profile: str = "funasr-token-revalidation",
) -> dict[str, Any]:
    """Bind authoritative text to one-token-per-cue acoustic timestamps.

    Caption text is segmented only at punctuation from the approved source.
    Real leading, trailing and inter-caption pauses remain gaps in the SRT.
    Sentence-level ASR input is rejected because it cannot prove word timing.
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
    if timing_validation_profile not in {
        "funasr-token-revalidation",
        "minimax-provider-native-word",
    }:
        raise ReferenceAlignmentError("timing_validation_profile 无效")
    try:
        reference_cues = parse_srt(reference_srt)
        parsed_asr_cues = parse_srt(asr_srt)
    except SrtValidationError as exc:
        raise ReferenceAlignmentError(f"SRT 无效: {exc}") from exc

    scene_ranges = _resolve_scene_ranges(scene_specs, len(reference_cues))
    scene_by_source = _source_scene_lookup(scene_ranges, len(reference_cues))
    reference_tokens: list[str] = []
    captions: list[dict[str, Any]] = []
    required_positions: list[int] = []
    for source_cue in reference_cues:
        source_ordinal = source_cue["sourceOrdinal"]
        for text in _caption_spans(source_cue["text"], source_ordinal=source_ordinal):
            span_tokens = _alignment_tokens(text)
            if not span_tokens:
                raise ReferenceAlignmentError(
                    f"原稿 cue {source_ordinal} 的语义字幕只有标点或空白"
                )
            start_token = len(reference_tokens)
            reference_tokens.extend(span_tokens)
            captions.append(
                {
                    "sourceCueOrdinal": source_ordinal,
                    "sceneId": scene_by_source[source_ordinal],
                    "text": text,
                    "startToken": start_token,
                    "endToken": len(reference_tokens),
                }
            )
            required_positions.append(len(reference_tokens))
    required_positions = required_positions[:-1]

    lexical_asr_cues: list[dict[str, Any]] = []
    ignored_asr_cues: list[int] = []
    asr_tokens: list[str] = []
    acoustic_token_boundaries = [0]
    for cue in parsed_asr_cues:
        tokens = _alignment_tokens(cue["text"])
        if not tokens:
            ignored_asr_cues.append(cue["sourceOrdinal"])
            continue
        if (
            timing_validation_profile == "funasr-token-revalidation"
            and len(tokens) != 1
        ):
            raise ReferenceAlignmentError(
                f"ASR cue {cue['sourceOrdinal']} 含 {len(tokens)} 个语义 token；"
                "新对齐合同要求一条 cue 对应一个真实 token 时间戳"
            )
        if cue["endMs"] > audio_duration_ms:
            raise ReferenceAlignmentError(
                f"ASR cue {cue['sourceOrdinal']} 超出音频总时长 {audio_duration_ms}ms"
            )
        lexical_asr_cues.append(cue)
        asr_tokens.extend(tokens)
        acoustic_token_boundaries.append(len(asr_tokens))
    if not lexical_asr_cues:
        raise ReferenceAlignmentError("ASR SRT 没有可对齐的 token cue")

    reference_text = "".join(reference_tokens)
    asr_text = "".join(asr_tokens)
    character_matcher = difflib.SequenceMatcher(None, asr_text, reference_text, autojunk=False)
    matched_characters = sum(block.size for block in character_matcher.get_matching_blocks())
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
        "timingFallbackUsed": False,
        "tokenTimingUsed": True,
        "timingValidationProfile": timing_validation_profile,
        "captionSegmentationContract": "reference-punctuation-caption-v1",
        "thresholds": {
            "minMatchRatio": min_match_ratio,
            "maxNormalizedEditRatio": max_normalized_edit_ratio,
            "maxBoundaryDisplacementTokens": _MAX_BOUNDARY_DISPLACEMENT_TOKENS,
            "maxCaptionCharactersPerSecond": _MAX_CAPTION_CHARACTERS_PER_SECOND,
            "minLocalAcousticCharactersPerSecond": _MIN_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND,
            "maxLocalAcousticCharactersPerSecond": _MAX_LOCAL_ACOUSTIC_CHARACTERS_PER_SECOND,
            "maxLocalAcousticRateVariationRatio": _MAX_LOCAL_ACOUSTIC_RATE_VARIATION_RATIO,
            "localAcousticWindowTokens": list(_LOCAL_ACOUSTIC_WINDOW_TOKENS),
        },
    }
    if match_ratio < min_match_ratio or edit_ratio > max_normalized_edit_ratio:
        diagnostics["status"] = "FAIL"
        raise ReferenceAlignmentError(
            "ASR 与已确认原稿的匹配质量过低，拒绝生成推测时间轴",
            diagnostics=diagnostics,
        )

    if timing_validation_profile == "funasr-token-revalidation":
        _validate_local_acoustic_rate(lexical_asr_cues, diagnostics)
    else:
        # MiniMax word 时间戳与音频来自同一次 provider 合成。这里保留原稿
        # 覆盖、真实边界、字幕阅读上限、scene 连续性和媒体 binding，但不再
        # 用另一套 ASR 的经验语速下限推翻 provider 自己的时间戳。
        diagnostics["localAcousticRate"] = {
            "policy": "not_applicable_provider_native_word_timing",
            "rateFloorPassed": None,
            "rateCeilingPassed": None,
            "rateVariationPassed": None,
            "outlierCount": None,
        }

    token_mapped_positions = _token_boundary_map(asr_tokens, reference_tokens)
    mapped_positions = (
        token_mapped_positions
        if timing_validation_profile == "funasr-token-revalidation"
        else [token_mapped_positions[index] for index in acoustic_token_boundaries]
    )
    try:
        selected_boundaries = _select_distinct_acoustic_boundaries(
            mapped_positions, required_positions
        )
    except ReferenceAlignmentError as exc:
        diagnostics["status"] = "FAIL"
        diagnostics["availableInternalAcousticBoundaries"] = len(mapped_positions) - 2
        diagnostics["requiredCaptionBoundaries"] = len(required_positions)
        raise ReferenceAlignmentError(str(exc), diagnostics=diagnostics) from exc

    boundary_displacements = [
        abs(mapped_positions[acoustic_index] - required_position)
        for acoustic_index, required_position in zip(
            selected_boundaries, required_positions, strict=True
        )
    ]
    max_displacement = max(boundary_displacements, default=0)
    diagnostics["maxBoundaryDisplacementTokens"] = max_displacement
    if max_displacement > _MAX_BOUNDARY_DISPLACEMENT_TOKENS:
        diagnostics["status"] = "FAIL"
        raise ReferenceAlignmentError(
            f"字幕语义边界与声学 token 边界最多偏移 {max_displacement} token，拒绝发布",
            diagnostics=diagnostics,
        )

    acoustic_boundaries = [0, *selected_boundaries, len(lexical_asr_cues)]
    cues: list[dict[str, Any]] = []
    rate_outliers: list[dict[str, Any]] = []
    for index, (caption, left, right) in enumerate(
        zip(captions, acoustic_boundaries, acoustic_boundaries[1:]), start=1
    ):
        if right <= left:
            raise ReferenceAlignmentError("ASR token 边界无法形成正时长字幕")
        start_ms = lexical_asr_cues[left]["startMs"]
        end_ms = lexical_asr_cues[right - 1]["endMs"]
        if end_ms <= start_ms:
            raise ReferenceAlignmentError("ASR token 时间无法形成正时长字幕")
        semantic_characters = len(normalise_alignment_text(caption["text"]))
        characters_per_second = semantic_characters * 1000 / (end_ms - start_ms)
        if characters_per_second > _MAX_CAPTION_CHARACTERS_PER_SECOND:
            rate_outliers.append(
                {
                    "index": index,
                    "charactersPerSecond": round(characters_per_second, 3),
                    "startMs": start_ms,
                    "endMs": end_ms,
                }
            )
        cues.append(
            {
                "index": index,
                "sourceOrdinal": index,
                "sourceCueOrdinal": caption["sourceCueOrdinal"],
                "sourceCueRange": [caption["sourceCueOrdinal"], caption["sourceCueOrdinal"]],
                "sceneId": caption["sceneId"],
                "startMs": start_ms,
                "endMs": end_ms,
                "text": caption["text"],
            }
        )
    if rate_outliers:
        diagnostics["status"] = "FAIL"
        diagnostics["captionRateOutliers"] = rate_outliers
        raise ReferenceAlignmentError(
            "字幕局部阅读速度超过声学对齐上限，拒绝发布",
            diagnostics=diagnostics,
        )

    reference_raw_text = "".join(cue["text"] for cue in reference_cues)
    if "".join(cue["text"] for cue in cues) != reference_raw_text:
        raise ReferenceAlignmentError("最终字幕未逐字、按序覆盖权威原稿（含原始标点）")
    for previous, current in zip(cues, cues[1:]):
        if current["startMs"] < previous["endMs"]:
            raise ReferenceAlignmentError("最终字幕时间重叠或乱序")
        if previous["sceneId"] != current["sceneId"] and (
            previous["sourceCueOrdinal"] >= current["sourceCueOrdinal"]
        ):
            raise ReferenceAlignmentError("scene 切换没有遵循原稿 cue 顺序")

    gaps = [
        current["startMs"] - previous["endMs"]
        for previous, current in zip(cues, cues[1:])
        if current["startMs"] > previous["endMs"]
    ]
    diagnostics["outputCueCount"] = len(cues)
    diagnostics["subtitleGaps"] = {
        "count": len(gaps),
        "totalMs": sum(gaps),
        "leadingMs": cues[0]["startMs"],
        "trailingMs": audio_duration_ms - cues[-1]["endMs"],
    }
    diagnostics["qualityGatePassed"] = True
    diagnostics["selectedCaptionBoundaries"] = [
        {
            "captionEndIndex": index + 1,
            "acousticBoundaryIndex": acoustic_index,
            "timeMs": lexical_asr_cues[acoustic_index]["startMs"],
            "displacementTokens": boundary_displacements[index],
        }
        for index, acoustic_index in enumerate(selected_boundaries)
    ]

    scenes: list[dict[str, Any]] = []
    for scene_index, (scene_id, first_source, last_source) in enumerate(scene_ranges):
        scene_cues = [
            cue
            for cue in cues
            if first_source <= cue["sourceCueOrdinal"] <= last_source
        ]
        if not scene_cues:
            raise ReferenceAlignmentError(f"{scene_id} 没有最终字幕 cue")
        start_ms = 0 if scene_index == 0 else scenes[-1]["endMs"]
        if scene_index == len(scene_ranges) - 1:
            end_ms = audio_duration_ms
            last_narrated_end_ms = scene_cues[-1]["endMs"]
            next_narrated_start_ms = None
            available_pause_ms = audio_duration_ms - last_narrated_end_ms
            boundary_basis = "canonical-audio-end"
        else:
            next_first_source = scene_ranges[scene_index + 1][1]
            next_cue = next(
                cue for cue in cues if cue["sourceCueOrdinal"] == next_first_source
            )
            last_narrated_end_ms = scene_cues[-1]["endMs"]
            next_narrated_start_ms = next_cue["startMs"]
            available_pause_ms = next_narrated_start_ms - last_narrated_end_ms
            if available_pause_ms < 0:
                raise ReferenceAlignmentError(
                    f"{scene_id} 的最后旁白尚未结束，下一 scene 旁白已经开始"
                )
            # scene N 至少保留到本幕最后一个真实声学 token 结束。若中间
            # 存在真实静音，只在该静音内取中点，让前后两幕各获得可审计的
            # 尾音/预备时间；不使用统一固定延迟掩盖对齐错误。
            end_ms = last_narrated_end_ms + available_pause_ms // 2
            boundary_basis = (
                "midpoint-of-real-token-gap"
                if available_pause_ms > 0
                else "shared-real-token-boundary"
            )
        if end_ms <= start_ms:
            raise ReferenceAlignmentError(f"{scene_id} 无法形成连续正时长场景")
        scenes.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [first_source, last_source],
                "narrationCueRange": [scene_cues[0]["index"], scene_cues[-1]["index"]],
                "startMs": start_ms,
                "endMs": end_ms,
                "sceneDurationMs": end_ms - start_ms,
                "lastNarratedTokenEndMs": last_narrated_end_ms,
                "nextNarratedTokenStartMs": next_narrated_start_ms,
                "availablePauseMs": available_pause_ms,
                "boundaryBasis": boundary_basis,
            }
        )
    return {
        "schemaVersion": 2,
        "cues": cues,
        "scenes": scenes,
        "diagnostics": diagnostics,
    }


__all__ = [
    "DEFAULT_MAX_CUE_MS_PER_CHARACTER",
    "DEFAULT_MAX_CUE_RATE_MULTIPLIER",
    "DEFAULT_MAX_NORMALIZED_EDIT_RATIO",
    "DEFAULT_MIN_MATCH_RATIO",
    "ReferenceAlignmentError",
    "align_reference_audio",
    "normalise_alignment_text",
]

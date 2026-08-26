#!/usr/bin/env python3
"""Strict, shared SRT parsing and source-timeline construction.

This module is deliberately independent from the project workspace loader.  It
is the single source of truth used by planning, subtitles and voice-over code
for cue identity and source timing.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_SOURCE_FPS = 60

_TIMESTAMP = r"(?P<{name}_h>\d+):(?P<{name}_m>\d{{2}}):(?P<{name}_s>\d{{2}})[,.](?P<{name}_ms>\d{{1,3}})"
_TIMELINE_RE = re.compile(
    r"^\s*"
    + _TIMESTAMP.format(name="start")
    + r"\s*-->\s*"
    + _TIMESTAMP.format(name="end")
    + r"\s*$"
)
_INTEGER_RE = re.compile(r"^[0-9]+$")


class SrtValidationError(ValueError):
    """Raised when an SRT or scene boundary violates the frozen contract."""


def _timestamp_to_ms(match: re.Match[str], name: str) -> int:
    hours = int(match.group(f"{name}_h"))
    minutes = int(match.group(f"{name}_m"))
    seconds = int(match.group(f"{name}_s"))
    millis_text = match.group(f"{name}_ms")
    if minutes > 59 or seconds > 59:
        raise SrtValidationError("SRT 时间戳的分和秒必须在 00..59")
    millis = int(millis_text.ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _normalise_newlines(text: str) -> str:
    if not isinstance(text, str):
        raise SrtValidationError("SRT 内容必须是字符串")
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse and strictly validate SRT while preserving cue text verbatim.

    ``sourceOrdinal`` is always the one-based parse order.  ``index`` remains
    as a compatibility alias for that stable ordinal.  A positive integer SRT
    index is retained separately as ``originalIndex``; it never determines cue
    identity.
    """

    normalised = _normalise_newlines(text)
    if not normalised.strip():
        raise SrtValidationError("SRT 不能为空")
    blocks = re.split(r"\n[ \t]*\n+", normalised.strip("\n"))
    cues: list[dict[str, Any]] = []

    for block_number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if not lines or all(not line.strip() for line in lines):
            continue

        timeline_index = 0
        original_index: int | None = None
        if "-->" not in lines[0]:
            index_text = lines[0].strip()
            if not _INTEGER_RE.fullmatch(index_text) or int(index_text) <= 0:
                raise SrtValidationError(f"第 {block_number} 个 cue 的原始编号无效")
            original_index = int(index_text)
            timeline_index = 1
        if timeline_index >= len(lines):
            raise SrtValidationError(f"第 {block_number} 个 cue 缺少时间轴")

        timeline_match = _TIMELINE_RE.fullmatch(lines[timeline_index])
        if timeline_match is None:
            raise SrtValidationError(f"第 {block_number} 个 cue 时间轴格式无效")
        start_ms = _timestamp_to_ms(timeline_match, "start")
        end_ms = _timestamp_to_ms(timeline_match, "end")
        if end_ms <= start_ms:
            reason = "零时长" if end_ms == start_ms else "结束时间早于开始时间"
            raise SrtValidationError(f"第 {block_number} 个 cue {reason}")

        text_lines = lines[timeline_index + 1 :]
        body = "\n".join(text_lines)
        if not body.strip():
            raise SrtValidationError(f"第 {block_number} 个 cue 文本不能为空")

        ordinal = len(cues) + 1
        if cues and start_ms < cues[-1]["endMs"]:
            raise SrtValidationError(
                f"第 {block_number} 个 cue 与 sourceOrdinal={cues[-1]['sourceOrdinal']} 重叠"
            )
        cue: dict[str, Any] = {
            "index": ordinal,
            "sourceOrdinal": ordinal,
            "startMs": start_ms,
            "endMs": end_ms,
            "durMs": end_ms - start_ms,
            "text": body,
        }
        if original_index is not None:
            cue["originalIndex"] = original_index
        cues.append(cue)

    if not cues:
        raise SrtValidationError("未解析到任何 SRT cue")
    return cues


def _format_timestamp(milliseconds: int) -> str:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
        raise SrtValidationError("SRT 时间必须是非负整数毫秒")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def serialize_srt(cues: Iterable[Mapping[str, Any]]) -> str:
    """Write cues deterministically without interpreting or escaping text."""

    serialised: list[str] = []
    previous_end = -1
    for ordinal, cue in enumerate(cues, start=1):
        try:
            start_ms = cue["startMs"]
            end_ms = cue["endMs"]
            body = cue["text"]
        except KeyError as exc:
            raise SrtValidationError(f"sourceOrdinal={ordinal} 缺少字段 {exc.args[0]}") from exc
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise SrtValidationError(f"sourceOrdinal={ordinal} 时间无效")
        if start_ms < previous_end:
            raise SrtValidationError(f"sourceOrdinal={ordinal} 与前一 cue 重叠")
        if not isinstance(body, str) or not body.strip():
            raise SrtValidationError(f"sourceOrdinal={ordinal} 文本不能为空")
        original = cue.get("originalIndex", ordinal)
        if isinstance(original, bool) or not isinstance(original, int) or original <= 0:
            raise SrtValidationError(f"sourceOrdinal={ordinal} originalIndex 无效")
        serialised.append(
            f"{original}\n{_format_timestamp(start_ms)} --> {_format_timestamp(end_ms)}\n{body}"
        )
        previous_end = end_ms
    if not serialised:
        raise SrtValidationError("不能写出空 SRT")
    return "\n\n".join(serialised) + "\n"


def write_srt(path: str | Path, cues: Iterable[Mapping[str, Any]]) -> None:
    Path(path).write_text(serialize_srt(cues), encoding="utf-8", newline="\n")


def _ceil_frame(end_ms: int, fps: int | float) -> int:
    # Multiplication before division keeps the integer path exact for integer
    # fps, which is the persisted v2 render profile used by this skill.
    return math.ceil(end_ms * fps / 1000)


def _scene_from_bucket(
    bucket: Sequence[Mapping[str, Any]],
    *,
    scene_index: int,
    start_ms: int,
    end_ms: int,
    start_frame: int,
    fps: int | float,
) -> dict[str, Any]:
    end_frame = _ceil_frame(end_ms, fps)
    first_ordinal = int(bucket[0]["sourceOrdinal"])
    last_ordinal = int(bucket[-1]["sourceOrdinal"])
    return {
        "sceneIndex": scene_index,
        "sceneId": f"scene-{scene_index:02d}",
        "startMs": start_ms,
        "endMs": end_ms,
        "sceneDurationMs": end_ms - start_ms,
        "cueRange": [first_ordinal, last_ordinal],
        "sourceCueRange": [first_ordinal, last_ordinal],
        "startFrame": start_frame,
        "endFrameExclusive": end_frame,
        "frameCount": end_frame - start_frame,
        "text": " ".join(str(cue["text"]).replace("\n", " ") for cue in bucket).strip(),
    }


def group_scenes(
    cues: list[dict[str, Any]], target_sec: float, min_sec: float, max_sec: float
) -> list[dict[str, Any]]:
    """Group cues and preserve the complete global source clock.

    Inter-scene gaps belong to the preceding scene.  Consequently scene zero
    begins at global 0, each later scene begins at its first cue, and the final
    scene closes exactly at the final source cue end.
    """

    if not cues:
        return []
    if not (0 < min_sec <= target_sec <= max_sec):
        raise SrtValidationError("分镜时长必须满足 0 < min <= target <= max")
    # Re-validate the public cue shape sufficiently to protect all consumers.
    previous_end = -1
    for expected, cue in enumerate(cues, start=1):
        if cue.get("sourceOrdinal", cue.get("index")) != expected:
            raise SrtValidationError("cue sourceOrdinal 必须从 1 起连续")
        if cue.get("startMs", -1) < previous_end or cue.get("endMs", 0) <= cue.get("startMs", -1):
            raise SrtValidationError("cue 时间必须为正时长、按序且不重叠")
        cue.setdefault("sourceOrdinal", expected)
        previous_end = cue["endMs"]

    target_ms = target_sec * 1000
    min_ms = min_sec * 1000
    max_ms = max_sec * 1000
    buckets: list[list[dict[str, Any]]] = []
    bucket: list[dict[str, Any]] = []
    for cue in cues:
        if bucket and cue["endMs"] - bucket[0]["startMs"] > max_ms:
            buckets.append(bucket)
            bucket = []
        bucket.append(cue)
        span = bucket[-1]["endMs"] - bucket[0]["startMs"]
        if span >= target_ms and span >= min_ms:
            buckets.append(bucket)
            bucket = []
    if bucket:
        buckets.append(bucket)

    scenes: list[dict[str, Any]] = []
    start_ms = 0
    start_frame = 0
    for index, current in enumerate(buckets, start=1):
        end_ms = buckets[index][0]["startMs"] if index < len(buckets) else cues[-1]["endMs"]
        scene = _scene_from_bucket(
            current,
            scene_index=index,
            start_ms=start_ms,
            end_ms=end_ms,
            start_frame=start_frame,
            fps=DEFAULT_SOURCE_FPS,
        )
        scenes.append(scene)
        start_ms = end_ms
        start_frame = scene["endFrameExclusive"]
    return scenes


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_range(value: Any, *, label: str) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    elif isinstance(value, Mapping):
        start = value.get("start", value.get("startOrdinal"))
        end = value.get("end", value.get("endOrdinal"))
    else:
        raise SrtValidationError(f"{label} 必须是含首尾 ordinal 的二元范围")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start <= 0
        or end < start
    ):
        raise SrtValidationError(f"{label} 必须是递增的正整数范围")
    return start, end


def _ordinals_from_subtitle_range(
    value: Any, cues: Sequence[Mapping[str, Any]], *, label: str
) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        raise SrtValidationError(f"{label} 必须是含 startMs/endMs 的对象")
    start_ms, end_ms = value.get("startMs"), value.get("endMs")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        raise SrtValidationError(f"{label} 时间范围无效")
    selected = [
        cue for cue in cues if cue["startMs"] >= start_ms and cue["endMs"] <= end_ms
    ]
    if not selected:
        raise SrtValidationError(f"{label} 未完整包含任何 cue")
    return int(selected[0]["sourceOrdinal"]), int(selected[-1]["sourceOrdinal"])


def _resolve_scene_ranges(
    scene_specs: Sequence[Mapping[str, Any]], cues: Sequence[Mapping[str, Any]]
) -> list[tuple[str, int, int]]:
    if not scene_specs:
        raise SrtValidationError("scene_specs 不能为空")
    resolved: list[tuple[str, int, int]] = []
    for index, spec in enumerate(scene_specs, start=1):
        if not isinstance(spec, Mapping):
            raise SrtValidationError(f"scene_specs[{index - 1}] 必须是对象")
        scene_id = spec.get("sceneId", f"scene-{index:02d}")
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise SrtValidationError(f"scene_specs[{index - 1}].sceneId 无效")
        if "cueRange" in spec:
            first, last = _parse_range(spec["cueRange"], label=f"{scene_id}.cueRange")
        elif "sourceCueRange" in spec:
            first, last = _parse_range(
                spec["sourceCueRange"], label=f"{scene_id}.sourceCueRange"
            )
        elif "subtitleRange" in spec:
            first, last = _ordinals_from_subtitle_range(
                spec["subtitleRange"], cues, label=f"{scene_id}.subtitleRange"
            )
        else:
            raise SrtValidationError(f"{scene_id} 缺少 cueRange 或 subtitleRange")
        resolved.append((scene_id, first, last))

    expected = 1
    for scene_id, first, last in resolved:
        if first != expected:
            raise SrtValidationError(f"{scene_id} 的 source cue 范围不连续，期望从 {expected} 开始")
        if last > len(cues):
            raise SrtValidationError(f"{scene_id} 的 source cue 范围超出 SRT")
        expected = last + 1
    if expected != len(cues) + 1:
        raise SrtValidationError("scene_specs 未覆盖全部 source cue")
    return resolved


def build_source_timing_plan(
    *,
    project_id: str,
    source_srt_path: str | Path,
    scene_specs: Sequence[Mapping[str, Any]],
    render_profile: Mapping[str, Any],
    voiceover_mode: str = "disabled",
) -> dict[str, Any]:
    """Build the plan-6.0 source timing snapshot used before audio approval.

    An Edge project also starts with a source-SRT provisional timeline.  Its
    current, audio-authoritative timing plan replaces this snapshot only after
    full narration and real duration have received explicit approval.
    """

    if voiceover_mode not in {"disabled", "edge-tts", "minimax", "doubao"}:
        raise SrtValidationError("voiceover_mode 只允许 disabled、edge-tts、minimax 或 doubao")
    if not isinstance(project_id, str) or not project_id:
        raise SrtValidationError("project_id 不能为空")
    source_path = Path(source_srt_path)
    try:
        raw = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SrtValidationError(f"无法读取 source SRT: {exc}") from exc
    cues = parse_srt(raw)
    ranges = _resolve_scene_ranges(scene_specs, cues)

    fps = render_profile.get("fps") if isinstance(render_profile, Mapping) else None
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        raise SrtValidationError("render_profile.fps 必须为正数")
    source_sha = _sha256_file(source_path)
    render_sha = _canonical_json_sha256(render_profile)

    scenes: list[dict[str, Any]] = []
    start_ms = 0
    start_frame = 0
    for index, (scene_id, first, last) in enumerate(ranges):
        # The next scene begins at its first semantic cue.  This assigns the
        # complete inter-scene subtitle gap to the preceding scene.
        end_ms = cues[ranges[index + 1][1] - 1]["startMs"] if index + 1 < len(ranges) else cues[-1]["endMs"]
        end_frame = _ceil_frame(end_ms, fps)
        scenes.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [first, last],
                "startMs": start_ms,
                "endMs": end_ms,
                "sceneDurationMs": end_ms - start_ms,
                "startFrame": start_frame,
                "endFrameExclusive": end_frame,
                "frameCount": end_frame - start_frame,
            }
        )
        start_ms = end_ms
        start_frame = end_frame

    return {
        "schemaVersion": 1,
        "projectId": project_id,
        "voiceoverMode": voiceover_mode,
        "sourceSrtSha256": source_sha,
        "renderProfileSha256": render_sha,
        "activeTimeline": {
            "kind": "source-srt",
            "file": "source/source.srt",
            "sha256": source_sha,
        },
        "scenes": scenes,
    }


__all__ = [
    "DEFAULT_SOURCE_FPS",
    "SrtValidationError",
    "build_source_timing_plan",
    "group_scenes",
    "parse_srt",
    "serialize_srt",
    "write_srt",
]

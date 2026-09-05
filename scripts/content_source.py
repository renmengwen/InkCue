#!/usr/bin/env python3
"""确定性校验 content draft，并派生 provisional SRT 与生图计划。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from project_workspace import (
    FIXED_CANVAS,
    ProjectValidationError,
    safe_project_path,
    sha256_file,
    sha256_json,
    validate_generation_plan_data,
)
from srt_timeline import SrtValidationError, parse_srt, serialize_srt
from visual_style_presets import (
    VisualStylePresetError,
    resolve_visual_style_preset,
)


PROVISIONAL_TIMING_ALGORITHM = "provisionalCumulativeMilliseconds"
PROVISIONAL_TIMING_ALGORITHM_VERSION = 1
MIN_TARGET_SECONDS = 15
MAX_TARGET_SECONDS = 600
MAX_TOPIC_CHARACTERS = 200
MAX_BODY_UTF8_BYTES = 128 * 1024
MIN_CUE_DURATION_MS = 400
SPOKEN_CHARACTER_WEIGHT = 100
SENTENCE_PAUSE_WEIGHT = 20
DOUBAO_MAX_SPOKEN_CHARACTERS_PER_SECOND = 3.2

_TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "inputMode",
    "topic",
    "body",
    "rewritePolicy",
    "targetDurationSeconds",
    "voiceoverMode",
    "visualStylePreset",
    "narrationCues",
    "scenes",
}
_OPTIONAL_LEGACY_TOP_LEVEL_FIELDS = {"visualStylePreset"}
_CUE_FIELDS = {"cueId", "sceneId", "text"}
_SCENE_FIELDS = {"sceneId", "name", "coreIdea", "visualSubject", "imagePrompt"}
_DRIVE_PATH_RE = re.compile(r"(?i)(?:^|\s)[a-z]:[\\/]")
_SENTENCE_PAUSE_RE = re.compile(r"[。！？!?；;…]")
_IMAGE_PROMPT_CROSS_REQUEST_MARKERS = (
    "延续",
    "沿用",
    "同上",
    "上一幕",
    "前一幕",
    "上一张",
    "前一张",
    "上图",
    "前图",
    "保持上一",
    "保持前一",
    "参照前",
    "如前",
    "same as previous",
    "previous scene",
    "previous image",
    "as above",
    "continue from",
)


class ContentSourceError(ProjectValidationError):
    """内容草案或 source 准备包违反冻结合同。"""


@dataclass(frozen=True)
class SourcePackage:
    directory: Path
    draft: dict[str, Any]
    source_srt: str
    generation_plan: dict[str, Any]
    manifest: dict[str, Any]

    @property
    def content_draft_identity(self) -> str:
        return self.manifest["contentDraftIdentitySha256"]


def _normalise_text(value: Any, *, label: str, allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    if not isinstance(value, str):
        raise ContentSourceError(f"{label} 必须是字符串")
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ContentSourceError(f"{label} 不能为空")
    if "\x00" in text:
        raise ContentSourceError(f"{label} 不得包含 NUL")
    if _DRIVE_PATH_RE.search(text):
        raise ContentSourceError(f"{label} 不得包含本机盘符绝对路径")
    return text


def _normalise_image_prompt(value: Any, *, label: str) -> str:
    text = _normalise_text(value, label=label)
    assert text is not None
    folded = text.casefold()
    for marker in _IMAGE_PROMPT_CROSS_REQUEST_MARKERS:
        if marker.casefold() in folded:
            raise ContentSourceError(
                f"{label} 必须是独立请求可用的自包含提示词，不得使用跨请求指代: {marker}"
            )
    return text


def _require_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    *,
    label: str,
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    unknown = set(value) - allowed
    missing = (allowed - set(value)) - set(optional)
    if unknown:
        raise ContentSourceError(f"{label} 含未知字段: {', '.join(sorted(unknown))}")
    if missing:
        raise ContentSourceError(f"{label} 缺少字段: {', '.join(sorted(missing))}")


def _collect_field_structure_errors(
    value: Any,
    allowed: set[str],
    *,
    label: str,
    optional: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """收集单个对象的结构错误，不让 unknown 遮住 missing。"""
    if not isinstance(value, Mapping):
        return [f"{label} 必须是对象"]
    unknown = set(value) - allowed
    missing = (allowed - set(value)) - set(optional)
    errors: list[str] = []
    if unknown:
        errors.append(f"{label} 含未知字段: {', '.join(sorted(unknown))}")
    if missing:
        errors.append(f"{label} 缺少字段: {', '.join(sorted(missing))}")
    return errors


def _collect_content_draft_structure_errors(value: Any) -> list[str]:
    """在值校验前一次性收集 content draft 的全部对象形状错误。"""
    errors = _collect_field_structure_errors(
        value,
        _TOP_LEVEL_FIELDS,
        label="content draft",
        optional=_OPTIONAL_LEGACY_TOP_LEVEL_FIELDS,
    )
    if not isinstance(value, Mapping):
        return errors

    for collection_name, fields in (
        ("narrationCues", _CUE_FIELDS),
        ("scenes", _SCENE_FIELDS),
    ):
        if collection_name not in value:
            continue
        collection = value.get(collection_name)
        if not isinstance(collection, list):
            errors.append(f"{collection_name} 必须是数组")
            continue
        for index, item in enumerate(collection):
            errors.extend(
                _collect_field_structure_errors(
                    item,
                    fields,
                    label=f"{collection_name}[{index}]",
                )
            )
    return errors


def _normalise_target_seconds(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContentSourceError("targetDurationSeconds 必须是有限数字且不能是布尔值")
    if value < MIN_TARGET_SECONDS or value > MAX_TARGET_SECONDS:
        raise ContentSourceError("targetDurationSeconds 必须在 15 到 600 秒之间")
    milliseconds = round(float(value) * 1000)
    if not math.isclose(float(value) * 1000, milliseconds, rel_tol=0, abs_tol=1e-7):
        raise ContentSourceError("targetDurationSeconds 最多精确到毫秒")
    return milliseconds // 1000 if milliseconds % 1000 == 0 else milliseconds / 1000


def _semantic_skeleton(text: str) -> str:
    return "".join(ch.casefold() for ch in text if unicodedata.category(ch)[0] in {"L", "N"})


def validate_content_draft(value: Any) -> dict[str, Any]:
    """返回规范化且完全脱离输入对象的 schemaVersion=1 content draft。"""
    structure_errors = _collect_content_draft_structure_errors(value)
    if structure_errors:
        raise ContentSourceError(
            "content draft 结构错误: " + "; ".join(structure_errors)
        )
    if value.get("schemaVersion") != 1:
        raise ContentSourceError("content draft schemaVersion 必须为 1")

    input_mode = value.get("inputMode")
    rewrite_policy = value.get("rewritePolicy")
    allowed_policy = {"topic": "generate", "text": {"preserve", "polish"}}
    if input_mode not in allowed_policy:
        raise ContentSourceError("content draft inputMode 只允许 topic 或 text")
    if input_mode == "topic" and rewrite_policy != allowed_policy["topic"]:
        raise ContentSourceError("topic 只允许 rewritePolicy=generate")
    if input_mode == "text" and rewrite_policy not in allowed_policy["text"]:
        raise ContentSourceError("text 只允许 rewritePolicy=preserve 或 polish")
    if value.get("voiceoverMode") not in {"edge-tts", "minimax", "doubao"}:
        raise ContentSourceError("非 SRT 输入只允许 voiceoverMode=edge-tts、minimax 或 doubao")
    try:
        visual_style_preset = resolve_visual_style_preset(value.get("visualStylePreset"))
    except VisualStylePresetError as exc:
        raise ContentSourceError(str(exc)) from exc

    topic = _normalise_text(value.get("topic"), label="topic", allow_null=input_mode == "text")
    body = _normalise_text(value.get("body"), label="body", allow_null=input_mode == "topic")
    if input_mode == "topic" and body is not None:
        raise ContentSourceError("topic 模式的 body 必须为 null")
    if input_mode == "text" and topic is not None and len(topic) > MAX_TOPIC_CHARACTERS:
        raise ContentSourceError("topic 超过 200 个 Unicode 字符")
    if topic is not None and len(topic) > MAX_TOPIC_CHARACTERS:
        raise ContentSourceError("topic 超过 200 个 Unicode 字符")
    if input_mode == "text" and body is None:
        raise ContentSourceError("text 模式必须提供 body")
    if body is not None and len(body.encode("utf-8")) > MAX_BODY_UTF8_BYTES:
        raise ContentSourceError("body 超过 128 KiB UTF-8")

    raw_scenes = value.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ContentSourceError("scenes 必须是非空数组")
    scenes: list[dict[str, str]] = []
    for ordinal, raw_scene in enumerate(raw_scenes, start=1):
        if not isinstance(raw_scene, Mapping):
            raise ContentSourceError(f"scenes[{ordinal - 1}] 必须是对象")
        _require_fields(raw_scene, _SCENE_FIELDS, label=f"scenes[{ordinal - 1}]")
        expected_id = f"scene-{ordinal:02d}"
        if raw_scene.get("sceneId") != expected_id:
            raise ContentSourceError(f"scene ID 必须从 scene-01 连续编号，期望 {expected_id}")
        scenes.append(
            {
                "sceneId": expected_id,
                "name": _normalise_text(raw_scene.get("name"), label=f"{expected_id}.name"),
                "coreIdea": _normalise_text(
                    raw_scene.get("coreIdea"), label=f"{expected_id}.coreIdea"
                ),
                "visualSubject": _normalise_text(
                    raw_scene.get("visualSubject"), label=f"{expected_id}.visualSubject"
                ),
                "imagePrompt": _normalise_image_prompt(
                    raw_scene.get("imagePrompt"), label=f"{expected_id}.imagePrompt"
                ),
            }
        )

    raw_cues = value.get("narrationCues")
    if not isinstance(raw_cues, list) or not raw_cues:
        raise ContentSourceError("narrationCues 必须是非空数组")
    scene_ids = {scene["sceneId"] for scene in scenes}
    cues: list[dict[str, str]] = []
    seen_scene_order: list[str] = []
    for ordinal, raw_cue in enumerate(raw_cues, start=1):
        if not isinstance(raw_cue, Mapping):
            raise ContentSourceError(f"narrationCues[{ordinal - 1}] 必须是对象")
        _require_fields(raw_cue, _CUE_FIELDS, label=f"narrationCues[{ordinal - 1}]")
        expected_id = f"cue-{ordinal:03d}"
        if raw_cue.get("cueId") != expected_id:
            raise ContentSourceError(f"cue ID 必须从 cue-001 连续编号，期望 {expected_id}")
        scene_id = raw_cue.get("sceneId")
        if scene_id not in scene_ids:
            raise ContentSourceError(f"{expected_id}.sceneId 不存在: {scene_id}")
        text = _normalise_text(raw_cue.get("text"), label=f"{expected_id}.text")
        if re.search(r"\n[ \t]*\n", text):
            raise ContentSourceError(f"{expected_id}.text 不得包含空白行")
        if not seen_scene_order or seen_scene_order[-1] != scene_id:
            if scene_id in seen_scene_order:
                raise ContentSourceError("cue 的 scene 映射不能跨 scene 后返回旧 scene")
            seen_scene_order.append(scene_id)
        cues.append({"cueId": expected_id, "sceneId": scene_id, "text": text})
    expected_scene_order = [scene["sceneId"] for scene in scenes]
    if seen_scene_order != expected_scene_order:
        raise ContentSourceError("每个 scene 必须按顺序包含至少一个连续 cue")
    if input_mode == "text" and rewrite_policy == "preserve":
        if _semantic_skeleton(body or "") != _semantic_skeleton("".join(c["text"] for c in cues)):
            raise ContentSourceError("text+preserve 的 cue 必须保留正文中的文字、数字与顺序")

    target_seconds = _normalise_target_seconds(value.get("targetDurationSeconds"))
    if value.get("voiceoverMode") == "doubao":
        spoken_characters = sum(
            1
            for cue in cues
            for character in cue["text"]
            if unicodedata.category(character)[0] in {"L", "N"}
        )
        maximum_characters = math.floor(
            float(target_seconds) * DOUBAO_MAX_SPOKEN_CHARACTERS_PER_SECOND
        )
        if spoken_characters > maximum_characters:
            raise ContentSourceError(
                "豆包旁白超过 prompt 时间轴的自然朗读预算："
                f"{spoken_characters} 个有效字符 > {maximum_characters}；"
                "请在阶段 0 压缩正文后重新生成 candidate"
            )

    return {
        "schemaVersion": 1,
        "inputMode": input_mode,
        "topic": topic,
        "body": body,
        "rewritePolicy": rewrite_policy,
        "targetDurationSeconds": target_seconds,
        "voiceoverMode": value["voiceoverMode"],
        "visualStylePreset": visual_style_preset.id,
        "narrationCues": cues,
        "scenes": scenes,
    }


def content_draft_identity(draft: Mapping[str, Any]) -> str:
    normalised = validate_content_draft(draft)
    return sha256_json(
        {key: value for key, value in normalised.items() if key != "visualStylePreset"}
    )


def spoken_text_weight(text: str) -> int:
    """Return the deterministic readable-text weight used for timing estimates."""

    spoken = sum(1 for ch in text if unicodedata.category(ch)[0] in {"L", "N"})
    pause = len(_SENTENCE_PAUSE_RE.findall(text))
    return max(
        1,
        spoken * SPOKEN_CHARACTER_WEIGHT + pause * SENTENCE_PAUSE_WEIGHT,
    )


def _cue_weight(text: str) -> int:
    return spoken_text_weight(text)


def build_provisional_cues(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalised = validate_content_draft(draft)
    target_ms = round(float(normalised["targetDurationSeconds"]) * 1000)
    source_cues = normalised["narrationCues"]
    minimum_total = len(source_cues) * MIN_CUE_DURATION_MS
    if minimum_total > target_ms:
        raise ContentSourceError(
            f"target 时长不足以为 {len(source_cues)} 个 cue 分配最短 {MIN_CUE_DURATION_MS}ms"
        )
    weights = [_cue_weight(cue["text"]) for cue in source_cues]
    weight_total = sum(weights)
    distributable = target_ms - minimum_total
    result: list[dict[str, Any]] = []
    cumulative_weight = 0
    start_ms = 0
    for ordinal, (source, weight) in enumerate(zip(source_cues, weights), start=1):
        cumulative_weight += weight
        end_ms = ordinal * MIN_CUE_DURATION_MS + (
            distributable * cumulative_weight // weight_total
        )
        if ordinal == len(source_cues):
            end_ms = target_ms
        result.append(
            {
                "originalIndex": ordinal,
                "sourceOrdinal": ordinal,
                "index": ordinal,
                "startMs": start_ms,
                "endMs": end_ms,
                "durMs": end_ms - start_ms,
                "text": source["text"],
            }
        )
        start_ms = end_ms
    return result


def build_provisional_srt(draft: Mapping[str, Any]) -> str:
    cues = build_provisional_cues(draft)
    try:
        serialised = serialize_srt(cues)
        parsed = parse_srt(serialised)
    except SrtValidationError as exc:
        raise ContentSourceError(f"provisional SRT 严格 round-trip 失败: {exc}") from exc
    expected = [
        (cue["startMs"], cue["endMs"], cue["text"]) for cue in cues
    ]
    actual = [(cue["startMs"], cue["endMs"], cue["text"]) for cue in parsed]
    if actual != expected:
        raise ContentSourceError("provisional SRT round-trip 后内容或时序发生变化")
    return serialised


def _safe_scene_filename(scene: Mapping[str, str]) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", scene["name"])
    name = re.sub(r"\s+", "-", name).strip(" .-")[:48]
    return f"{scene['sceneId']}-{name or 'scene'}.png"


def build_generation_plan(
    draft: Mapping[str, Any],
    *,
    forbid_text: bool = False,
    global_prompt: str | None = None,
    include_visual_style_snapshot: bool = True,
) -> dict[str, Any]:
    normalised = validate_content_draft(draft)
    try:
        visual_style_preset = resolve_visual_style_preset(normalised["visualStylePreset"])
    except VisualStylePresetError as exc:
        raise ContentSourceError(str(exc)) from exc
    if global_prompt is None:
        global_prompt = visual_style_preset.prompt_recipe
    if include_visual_style_snapshot and global_prompt != visual_style_preset.prompt_recipe:
        raise ContentSourceError("新 generation plan 的 globalPrompt 必须冻结所选模板 promptRecipe")
    cues = build_provisional_cues(normalised)
    scenes: list[dict[str, Any]] = []
    for scene in normalised["scenes"]:
        ordinals = [
            index
            for index, cue in enumerate(normalised["narrationCues"], start=1)
            if cue["sceneId"] == scene["sceneId"]
        ]
        first, last = ordinals[0], ordinals[-1]
        start_ms = cues[first - 1]["startMs"]
        end_ms = cues[last - 1]["endMs"]
        scenes.append(
            {
                "sceneId": scene["sceneId"],
                "name": scene["name"],
                "coreIdea": scene["coreIdea"],
                "visualSubject": scene["visualSubject"],
                "cueRange": [first, last],
                "subtitleRange": {"startMs": start_ms, "endMs": end_ms},
                "sceneDurationMs": end_ms - start_ms,
                "prompt": scene["imagePrompt"],
                "outputFile": _safe_scene_filename(scene),
            }
        )
    plan = {
        "schemaVersion": 1,
        "projectId": "",
        "outputCanvas": dict(FIXED_CANVAS),
        "globalPrompt": global_prompt,
        "constraints": {"forbidText": forbid_text},
        "scenesDirectory": "scenes",
        "manifestFile": "manifests/generation-manifest.json",
        "scenes": scenes,
    }
    if include_visual_style_snapshot:
        plan.update(
            {
                "visualStylePreset": visual_style_preset.id,
                "visualStyleDisplayName": visual_style_preset.display_name,
                "visualStylePromptRecipeSha256": visual_style_preset.recipe_sha256,
            }
        )
    validate_generation_plan_data(plan, project_id="")
    return plan


def _json_file_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_source_package(
    draft: Mapping[str, Any],
    *,
    forbid_text: bool = False,
    global_prompt: str | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    normalised = validate_content_draft(draft)
    persisted_draft = dict(normalised)
    source_srt = build_provisional_srt(normalised)
    generation_plan = build_generation_plan(
        normalised,
        forbid_text=forbid_text,
        global_prompt=global_prompt,
        include_visual_style_snapshot=True,
    )
    input_bytes = _json_file_bytes(persisted_draft)
    srt_bytes = source_srt.encode("utf-8")
    plan_bytes = _json_file_bytes(generation_plan)
    cue_binding = [
        {"cueId": cue["cueId"], "sceneId": cue["sceneId"], "text": cue["text"]}
        for cue in normalised["narrationCues"]
    ]
    manifest_core: dict[str, Any] = {
        "schemaVersion": 1,
        # 视觉模板只影响 generation plan 与图片链；content identity 继续绑定
        # 文案/cue/scene 边界，避免纯模板切换误伤音频与真实时间轴。
        "contentDraftIdentitySha256": sha256_json(
            {key: value for key, value in persisted_draft.items() if key != "visualStylePreset"}
        ),
        "narrationCueIdentitySha256": sha256_json(cue_binding),
        "inputMode": normalised["inputMode"],
        "rewritePolicy": normalised["rewritePolicy"],
        "targetDurationSeconds": normalised["targetDurationSeconds"],
        "voiceoverMode": normalised["voiceoverMode"],
        "timingAlgorithm": {
            "algorithm": PROVISIONAL_TIMING_ALGORITHM,
            "version": PROVISIONAL_TIMING_ALGORITHM_VERSION,
            "parameters": {
                "minimumCueDurationMs": MIN_CUE_DURATION_MS,
                "spokenCharacterWeight": SPOKEN_CHARACTER_WEIGHT,
                "sentencePauseWeight": SENTENCE_PAUSE_WEIGHT,
            },
        },
        "files": {
            "input.json": {"sha256": _sha256_bytes(input_bytes)},
            "source.srt": {"sha256": _sha256_bytes(srt_bytes)},
            "generation-plan.json": {"sha256": _sha256_bytes(plan_bytes)},
        },
    }
    manifest_core.update(
        {
            "visualStylePreset": normalised["visualStylePreset"],
            "visualStylePromptRecipeSha256": generation_plan[
                "visualStylePromptRecipeSha256"
            ],
        }
    )
    manifest_core["sourcePackageIdentitySha256"] = sha256_json(manifest_core)
    return persisted_draft, source_srt, generation_plan, manifest_core


def validate_source_package(
    source_input: str | Path,
    source_manifest: str | Path,
    source_srt: str | Path,
    generation_plan: str | Path,
) -> SourcePackage:
    paths = [Path(item).resolve(strict=False) for item in (
        source_input, source_manifest, source_srt, generation_plan
    )]
    if len({path.parent for path in paths}) != 1:
        raise ContentSourceError("input、manifest、SRT 与 generation plan 必须来自同一准备包目录")
    expected_names = ["input.json", "manifest.json", "source.srt", "generation-plan.json"]
    if [path.name for path in paths] != expected_names:
        raise ContentSourceError("source 准备包文件名必须为 input.json/manifest.json/source.srt/generation-plan.json")
    try:
        raw_draft = json.loads(paths[0].read_text(encoding="utf-8"))
        raw_manifest = json.loads(paths[1].read_text(encoding="utf-8"))
        raw_srt = paths[2].read_text(encoding="utf-8")
        raw_plan = json.loads(paths[3].read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentSourceError(f"source 准备包缺少文件: {exc.filename}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContentSourceError(f"无法读取 source 准备包: {exc}") from exc
    raw_constraints = raw_plan.get("constraints") if isinstance(raw_plan, Mapping) else None
    raw_forbid_text = (
        raw_constraints.get("forbidText")
        if isinstance(raw_constraints, Mapping)
        and isinstance(raw_constraints.get("forbidText"), bool)
        else False
    )
    expected_draft, expected_srt, expected_plan, expected_manifest = build_source_package(
        raw_draft,
        forbid_text=raw_forbid_text,
        global_prompt=None,
    )
    if paths[0].read_bytes() != _json_file_bytes(expected_draft):
        raise ContentSourceError("input.json 字节不是规范化确定性输出")
    if paths[2].read_bytes() != expected_srt.encode("utf-8"):
        raise ContentSourceError("source.srt 字节不是确定性输出")
    if paths[3].read_bytes() != _json_file_bytes(expected_plan):
        raise ContentSourceError("generation-plan.json 字节不是规范化确定性输出")
    if paths[1].read_bytes() != _json_file_bytes(expected_manifest):
        raise ContentSourceError("manifest.json 字节不是规范化确定性输出")
    if raw_draft != expected_draft:
        raise ContentSourceError("input.json 不是规范化的 content draft")
    if raw_srt != expected_srt:
        raise ContentSourceError("source.srt 与 content draft 的确定性派生结果不一致")
    if raw_plan != expected_plan:
        raise ContentSourceError("generation-plan.json 与 content draft 的确定性派生结果不一致")
    if raw_manifest != expected_manifest:
        raise ContentSourceError("manifest.json 的 hash 或绑定关系无效")
    return SourcePackage(
        paths[0].parent,
        validate_content_draft(raw_draft),
        expected_srt,
        expected_plan,
        expected_manifest,
    )


def validate_project_source_binding(
    project_root: str | Path,
    content_source: Mapping[str, Any],
    *,
    project_id: str,
    source_srt_sha256: str,
) -> None:
    """重验正式项目内可选 content source 证据及其与 current plan/SRT 的绑定。"""
    root = Path(project_root).resolve(strict=False)
    input_path = safe_project_path(root, content_source["inputFile"])
    manifest_path = safe_project_path(root, content_source["manifestFile"])
    source_path = safe_project_path(root, "source/source.srt")
    plan_path = safe_project_path(root, "planning/generation-plan.json")
    try:
        raw_draft = json.loads(input_path.read_text(encoding="utf-8"))
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_srt = source_path.read_text(encoding="utf-8")
        raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentSourceError(f"正式项目缺少 content source 证据: {exc.filename}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContentSourceError(f"无法读取正式项目 content source 证据: {exc}") from exc
    raw_constraints = raw_plan.get("constraints") if isinstance(raw_plan, Mapping) else None
    raw_forbid_text = (
        raw_constraints.get("forbidText")
        if isinstance(raw_constraints, Mapping)
        and isinstance(raw_constraints.get("forbidText"), bool)
        else False
    )
    expected_draft, expected_srt, expected_plan, expected_manifest = build_source_package(
        raw_draft,
        forbid_text=raw_forbid_text,
        global_prompt=None,
    )
    if raw_draft != expected_draft or raw_manifest != expected_manifest or raw_srt != expected_srt:
        raise ContentSourceError("正式项目 content source 输入、manifest 或 SRT 已失配")
    expected_plan["projectId"] = project_id
    validate_generation_plan_data(expected_plan, project_id=project_id)
    if raw_plan != expected_plan:
        raise ContentSourceError("正式项目 generation plan 与 content source 派生结果不一致")
    if input_path.read_bytes() != _json_file_bytes(expected_draft):
        raise ContentSourceError("正式项目 input.json 字节不是规范化确定性输出")
    if manifest_path.read_bytes() != _json_file_bytes(expected_manifest):
        raise ContentSourceError("正式项目 source manifest 字节不是规范化确定性输出")
    if source_path.read_bytes() != expected_srt.encode("utf-8"):
        raise ContentSourceError("正式项目 source SRT 字节不是确定性输出")
    if sha256_file(plan_path) != content_source["generationPlanSha256"]:
        raise ContentSourceError("正式项目 generation plan SHA-256 与 project.json 不一致")
    if sha256_file(source_path) != source_srt_sha256:
        raise ContentSourceError("正式项目 source SRT 与 project.json 绑定不一致")
    if sha256_file(input_path) != content_source["inputSha256"]:
        raise ContentSourceError("正式项目 input.json SHA-256 与 project.json 不一致")
    if sha256_file(manifest_path) != content_source["manifestSha256"]:
        raise ContentSourceError("正式项目 source manifest SHA-256 与 project.json 不一致")
    if expected_manifest["contentDraftIdentitySha256"] != content_source["inputIdentitySha256"]:
        raise ContentSourceError("正式项目 content draft identity 与 project.json 不一致")
    if expected_manifest["sourcePackageIdentitySha256"] != content_source["sourcePackageIdentitySha256"]:
        raise ContentSourceError("正式项目 source package identity 与 project.json 不一致")


__all__ = [
    "DOUBAO_MAX_SPOKEN_CHARACTERS_PER_SECOND",
    "PROVISIONAL_TIMING_ALGORITHM",
    "PROVISIONAL_TIMING_ALGORITHM_VERSION",
    "ContentSourceError",
    "SourcePackage",
    "build_generation_plan",
    "build_provisional_cues",
    "build_provisional_srt",
    "build_source_package",
    "content_draft_identity",
    "validate_content_draft",
    "validate_project_source_binding",
    "validate_source_package",
]

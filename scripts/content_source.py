#!/usr/bin/env python3
"""确定性校验 content-draft-v1，并派生 provisional SRT 与生图计划。"""
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
    DEFAULT_GLOBAL_PROMPT,
    FIXED_CANVAS,
    ProjectValidationError,
    safe_project_path,
    sha256_file,
    sha256_json,
    validate_generation_plan_data,
)
from srt_timeline import SrtValidationError, parse_srt, serialize_srt


CONTENT_DRAFT_CONTRACT_VERSION = "whiteboard-content-draft-v1"
SOURCE_PACKAGE_CONTRACT_VERSION = "whiteboard-source-package-v1"
PROVISIONAL_TIMING_VERSION = "provisional-cumulative-ms-v1"
PREPARE_SOURCE_TOOL_VERSION = "prepare-source-v1"
MIN_TARGET_SECONDS = 15
MAX_TARGET_SECONDS = 600
MAX_TOPIC_CHARACTERS = 200
MAX_BODY_UTF8_BYTES = 128 * 1024
MIN_CUE_DURATION_MS = 400

_TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "contractVersion",
    "inputMode",
    "topic",
    "body",
    "rewritePolicy",
    "targetDurationSeconds",
    "voiceoverMode",
    "narrationCues",
    "scenes",
}
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


def _require_fields(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown:
        raise ContentSourceError(f"{label} 含未知字段: {', '.join(sorted(unknown))}")
    if missing:
        raise ContentSourceError(f"{label} 缺少字段: {', '.join(sorted(missing))}")


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
    """返回规范化且完全脱离输入对象的 content-draft-v1。"""
    if not isinstance(value, Mapping):
        raise ContentSourceError("content draft 顶层必须是 JSON 对象")
    _require_fields(value, _TOP_LEVEL_FIELDS, label="content draft")
    if value.get("schemaVersion") != 1:
        raise ContentSourceError("content draft schemaVersion 必须为 1")
    if value.get("contractVersion") != CONTENT_DRAFT_CONTRACT_VERSION:
        raise ContentSourceError(
            f"content draft contractVersion 必须为 {CONTENT_DRAFT_CONTRACT_VERSION}"
        )

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

    return {
        "schemaVersion": 1,
        "contractVersion": CONTENT_DRAFT_CONTRACT_VERSION,
        "inputMode": input_mode,
        "topic": topic,
        "body": body,
        "rewritePolicy": rewrite_policy,
        "targetDurationSeconds": _normalise_target_seconds(value.get("targetDurationSeconds")),
        "voiceoverMode": value["voiceoverMode"],
        "narrationCues": cues,
        "scenes": scenes,
    }


def content_draft_identity(draft: Mapping[str, Any]) -> str:
    return sha256_json(validate_content_draft(draft))


def _cue_weight(text: str) -> int:
    spoken = sum(1 for ch in text if unicodedata.category(ch)[0] in {"L", "N"})
    pause = len(_SENTENCE_PAUSE_RE.findall(text))
    return max(1, spoken * 100 + pause * 20)


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


def build_generation_plan(draft: Mapping[str, Any]) -> dict[str, Any]:
    normalised = validate_content_draft(draft)
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
        "globalPrompt": DEFAULT_GLOBAL_PROMPT,
        "constraints": {"forbidText": True},
        "scenesDirectory": "scenes",
        "manifestFile": "manifests/generation-manifest.json",
        "scenes": scenes,
    }
    validate_generation_plan_data(plan, project_id="")
    return plan


def _json_file_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_source_package(draft: Mapping[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    normalised = validate_content_draft(draft)
    source_srt = build_provisional_srt(normalised)
    generation_plan = build_generation_plan(normalised)
    input_bytes = _json_file_bytes(normalised)
    srt_bytes = source_srt.encode("utf-8")
    plan_bytes = _json_file_bytes(generation_plan)
    cue_binding = [
        {"cueId": cue["cueId"], "sceneId": cue["sceneId"], "text": cue["text"]}
        for cue in normalised["narrationCues"]
    ]
    manifest_core: dict[str, Any] = {
        "schemaVersion": 1,
        "contractVersion": SOURCE_PACKAGE_CONTRACT_VERSION,
        "contentDraftContractVersion": CONTENT_DRAFT_CONTRACT_VERSION,
        "contentDraftIdentitySha256": sha256_json(normalised),
        "narrationCueIdentitySha256": sha256_json(cue_binding),
        "inputMode": normalised["inputMode"],
        "rewritePolicy": normalised["rewritePolicy"],
        "targetDurationSeconds": normalised["targetDurationSeconds"],
        "voiceoverMode": normalised["voiceoverMode"],
        "timingAlgorithmVersion": PROVISIONAL_TIMING_VERSION,
        "toolVersion": PREPARE_SOURCE_TOOL_VERSION,
        "files": {
            "input.json": {"sha256": _sha256_bytes(input_bytes)},
            "source.srt": {"sha256": _sha256_bytes(srt_bytes)},
            "generation-plan.json": {"sha256": _sha256_bytes(plan_bytes)},
        },
    }
    manifest_core["sourcePackageIdentitySha256"] = sha256_json(manifest_core)
    return normalised, source_srt, generation_plan, manifest_core


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
    expected_draft, expected_srt, expected_plan, expected_manifest = build_source_package(raw_draft)
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
    return SourcePackage(paths[0].parent, expected_draft, expected_srt, expected_plan, expected_manifest)


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
    expected_draft, expected_srt, expected_plan, expected_manifest = build_source_package(raw_draft)
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
    "CONTENT_DRAFT_CONTRACT_VERSION",
    "SOURCE_PACKAGE_CONTRACT_VERSION",
    "PROVISIONAL_TIMING_VERSION",
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

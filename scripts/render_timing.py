#!/usr/bin/env python3
"""Project-aware annotation timing validation and formal scene render identity."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from .project_workspace import Project, sha256_file, sha256_json, write_json_atomic
    from .annotation_contract import (
        AnnotationContractError,
        normalize_legacy_visual_elements,
        validate_visual_elements,
    )
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import Project, sha256_file, sha256_json, write_json_atomic
    from annotation_contract import (
        AnnotationContractError,
        normalize_legacy_visual_elements,
        validate_visual_elements,
    )


RENDER_CONTRACT_VERSION = "whiteboard-project-scene-render-v1"
RENDER_MANIFEST_FILE = "manifests/render-manifest.json"


class RenderTimingError(ValueError):
    """The project, annotation, or current timing identity is not renderable."""


@dataclass(frozen=True)
class FormalValidationContext:
    """一次 batch 冻结并复用的正式渲染全局证据。"""

    timing_plan_sha256: str
    timing_plan_file: str | None
    render_profile_sha256: str
    active_timeline: dict[str, Any]
    audio_sha256: str | None
    full_approval_identity_hash: str | None
    voice_manifest_sha256: str | None = None


@dataclass(frozen=True)
class FormalSceneRender:
    project: Project
    scene_id: str
    image_path: Path
    annotation_path: Path
    output_path: Path
    timing_scene: dict[str, Any]
    timing_plan_sha256: str
    timing_plan_file: str | None
    render_profile_sha256: str
    active_timeline: dict[str, Any]
    audio_sha256: str | None
    full_approval_identity_hash: str | None
    annotation: dict[str, Any]
    compatibility_mode: str | None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderTimingError(f"无法读取{label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderTimingError(f"{label}顶层必须是 JSON 对象")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _scene(project: Project, scene_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    generation = next((item for item in project.plan["scenes"] if item.get("sceneId") == scene_id), None)
    timing = next((item for item in project.timing_plan["scenes"] if item.get("sceneId") == scene_id), None)
    if generation is None or timing is None:
        raise RenderTimingError(f"sceneId 不存在于 current generation/timing plan: {scene_id}")
    return generation, timing


def _validate_audio_approval(project: Project) -> tuple[str, str]:
    try:
        try:
            from .generate_voiceover import validate_current_voiceover
        except ImportError:  # pragma: no cover - direct script execution
            from generate_voiceover import validate_current_voiceover
        current = validate_current_voiceover(project, require_full=True)
    except Exception as exc:
        raise RenderTimingError(f"正式音频渲染要求 current approve-full: {exc}") from exc
    if current.get("fullApproved") is not True:
        raise RenderTimingError("正式音频渲染要求 current approve-full")
    audio_sha = current.get("audioSha256")
    approval_identity = current.get("fullIdentityHash")
    if not _is_sha256(audio_sha) or not _is_sha256(approval_identity):
        raise RenderTimingError("Edge current audio/full approval identity 无效")
    return audio_sha, approval_identity


def build_formal_validation_context(project: Project) -> FormalValidationContext:
    """深验一次全局 timing/voice evidence，供逐幕校验复用。"""

    timing_plan_sha = (
        sha256_file(project.timing_plan_path)
        if project.timing_plan_persisted
        else sha256_json(project.timing_plan)
    )
    active = copy.deepcopy(project.timing_plan["activeTimeline"])
    audio_sha: str | None = None
    approval_identity: str | None = None
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        if active.get("kind") not in {"edge-tts-audio-timeline", "audio-authoritative-timeline"}:
            raise RenderTimingError("正式音频渲染只接受 current audio timeline timing plan")
        audio_sha, approval_identity = _validate_audio_approval(project)
    elif active.get("kind") != "source-srt":
        raise RenderTimingError("Disabled 正式渲染只接受 current source-srt timing plan")
    voice_manifest_sha: str | None = None
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        voice_manifest = project.path("manifests/voice-manifest.json")
        if voice_manifest.is_file():
            voice_manifest_sha = sha256_file(voice_manifest)
    return FormalValidationContext(
        timing_plan_sha256=timing_plan_sha,
        timing_plan_file=("planning/timing-plan.json" if project.timing_plan_persisted else None),
        render_profile_sha256=sha256_json(project.render_profile),
        active_timeline=active,
        audio_sha256=audio_sha,
        full_approval_identity_hash=approval_identity,
        voice_manifest_sha256=voice_manifest_sha,
    )


def validate_formal_context_current(
    project: Project,
    context: FormalValidationContext,
) -> None:
    """只做字节/binding current 核对，不再次调用语音 deep validator。"""

    timing_sha = (
        sha256_file(project.timing_plan_path)
        if project.timing_plan_persisted
        else sha256_json(project.timing_plan)
    )
    if timing_sha != context.timing_plan_sha256:
        raise RenderTimingError("batch 期间 timing plan 已变化")
    if sha256_json(project.render_profile) != context.render_profile_sha256:
        raise RenderTimingError("batch 期间 render profile 已变化")
    if project.timing_plan.get("activeTimeline") != context.active_timeline:
        raise RenderTimingError("batch 期间 active timeline 已变化")
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        audio_path = project.path("audio/narration.wav")
        if not audio_path.is_file() or sha256_file(audio_path) != context.audio_sha256:
            raise RenderTimingError("batch 期间 current narration.wav 已变化")
        manifest_path = project.path("manifests/voice-manifest.json")
        manifest = _load_json(manifest_path, "voice manifest")
        if sha256_file(manifest_path) != context.voice_manifest_sha256:
            raise RenderTimingError("batch 期间 voice manifest 已变化")
        approval = manifest.get("fullApproval")
        if (
            not isinstance(approval, Mapping)
            or approval.get("approved") is not True
            or approval.get("identityHash") != context.full_approval_identity_hash
        ):
            raise RenderTimingError("batch 期间 full approval identity 已变化")


def _validate_frame_range(annotation: Mapping[str, Any], timing_scene: Mapping[str, Any]) -> None:
    frame_range = annotation.get("sceneFrameRange")
    expected = {
        "startFrame": timing_scene["startFrame"],
        "endFrameExclusive": timing_scene["endFrameExclusive"],
        "frameCount": timing_scene["frameCount"],
    }
    if frame_range != expected:
        raise RenderTimingError("annotation sceneFrameRange 与 current timing plan 不一致")


def _validate_timing_source(
    annotation: Mapping[str, Any],
    *,
    project: Project,
    timing_scene: Mapping[str, Any],
    active_timeline: Mapping[str, Any],
    audio_sha256: str | None,
) -> None:
    source = annotation.get("timingSource")
    if not isinstance(source, Mapping):
        raise RenderTimingError("annotation 缺少 timingSource")
    expected_common = {
        "kind": active_timeline["kind"],
        "timelineFile": active_timeline["file"],
        "timelineSha256": active_timeline["sha256"],
        "sceneId": timing_scene["sceneId"],
        "sceneStartMs": timing_scene["startMs"],
        "sceneEndMs": timing_scene["endMs"],
    }
    for key, expected in expected_common.items():
        if source.get(key) != expected:
            raise RenderTimingError(f"annotation timingSource.{key} 与 current timing plan 不一致")
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        if source.get("audioSha256") != audio_sha256:
            raise RenderTimingError("annotation timingSource.audioSha256 与 current narration.wav 不一致")
    elif "audioSha256" in source and source.get("audioSha256") not in (None, ""):
        raise RenderTimingError("Disabled annotation 不得绑定 Edge audioSha256")


def validate_annotation(
    annotation: Mapping[str, Any],
    *,
    project: Project,
    timing_scene: Mapping[str, Any],
    timing_plan_sha256: str,
    render_profile_sha256: str,
    active_timeline: Mapping[str, Any],
    audio_sha256: str | None,
    allow_v1_disabled_compat: bool,
) -> dict[str, Any]:
    """Validate global timeline bindings and scene-local reveal coordinates."""
    value = copy.deepcopy(dict(annotation))
    if value.get("sceneId") != timing_scene["sceneId"]:
        raise RenderTimingError("annotation sceneId 与请求场景不一致")
    duration = timing_scene["sceneDurationMs"]
    if value.get("sceneDurationMs") != duration:
        raise RenderTimingError("annotation sceneDurationMs 与 current timing scene 不一致")

    compatibility = project.schema_version == 1
    if compatibility:
        if project.voiceover_mode != "disabled" or not allow_v1_disabled_compat:
            raise RenderTimingError("schema v1 仅允许 --allow-v1-disabled-compat 明确只读兼容渲染")
    else:
        if value.get("timingPlanSha256") != timing_plan_sha256:
            raise RenderTimingError("annotation timingPlanSha256 stale")
        if value.get("renderProfileSha256") != render_profile_sha256:
            raise RenderTimingError("annotation renderProfileSha256 stale")
        _validate_frame_range(value, timing_scene)
        _validate_timing_source(
            value,
            project=project,
            timing_scene=timing_scene,
            active_timeline=active_timeline,
            audio_sha256=audio_sha256,
        )

    canvas = value.get("canvas")
    profile = project.render_profile
    if canvas != {"width": profile["width"], "height": profile["height"]}:
        raise RenderTimingError("annotation canvas 必须与 project renderProfile 尺寸一致")
    elements = value.get("elements")
    try:
        validator = normalize_legacy_visual_elements if compatibility else validate_visual_elements
        elements = validator(elements, canvas=canvas, scene_duration_ms=duration)
    except AnnotationContractError as exc:
        raise RenderTimingError(str(exc)) from exc
    value["elements"] = elements
    previous_end = 0
    for index, element in enumerate(sorted(elements, key=lambda item: item.get("sequence", 0)), start=1):
        if not isinstance(element, Mapping) or element.get("sequence") != index:
            raise RenderTimingError("annotation element sequence 必须从 1 起连续")
        reveal = element.get("reveal")
        if not isinstance(reveal, Mapping):
            raise RenderTimingError(f"element-{index} 缺少 reveal")
        start = reveal.get("startMs")
        length = reveal.get("durationMs")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or start < 0
            or length <= 0
        ):
            raise RenderTimingError(f"element-{index} reveal 必须使用正时长的场景局部毫秒")
        end = start + length
        if start < previous_end:
            raise RenderTimingError("annotation elements 必须按场景局部时间串行且不重叠")
        if end > duration - 500:
            raise RenderTimingError(
                f"element-{index} 结束于 {end}ms，超过 sceneDurationMs - 500 ({duration - 500}ms)"
            )
        previous_end = end
        region = element.get("region")
        if not isinstance(region, Mapping):
            raise RenderTimingError(f"element-{index} 缺少 region")
        coords = [region.get(key) for key in ("x", "y", "width", "height")]
        if any(isinstance(item, bool) or not isinstance(item, int) for item in coords):
            raise RenderTimingError(f"element-{index} region 必须是整数像素")
        x, y, width, height = coords
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > canvas["width"] or y + height > canvas["height"]:
            raise RenderTimingError(f"element-{index} region 越出 annotation canvas")
        protected = reveal.get("protectedRegions", [])
        if not isinstance(protected, list):
            raise RenderTimingError(f"element-{index} protectedRegions 必须是数组")
        for protected_index, protected_region in enumerate(protected, start=1):
            if not isinstance(protected_region, Mapping):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 必须是对象"
                )
            protected_coords = [
                protected_region.get(key) for key in ("x", "y", "width", "height")
            ]
            if any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in protected_coords
            ):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 必须是整数像素"
                )
            px, py, pwidth, pheight = protected_coords
            if (
                px < 0
                or py < 0
                or pwidth <= 0
                or pheight <= 0
                or px + pwidth > canvas["width"]
                or py + pheight > canvas["height"]
            ):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 越出 annotation canvas"
                )
    return value


def resolve_formal_scenes(
    project: Project,
    scene_ids: list[str] | tuple[str, ...],
    *,
    context: FormalValidationContext | None = None,
    allow_v1_disabled_compat: bool = False,
) -> tuple[FormalSceneRender, ...]:
    """按请求顺序解析多幕；全局 evidence 在本调用内只深验一次。"""

    frozen = context or build_formal_validation_context(project)
    validate_formal_context_current(project, frozen)
    resolved: list[FormalSceneRender] = []
    for scene_id in scene_ids:
        generation_scene, timing_scene = _scene(project, scene_id)
        output_file = generation_scene["outputFile"]
        image_path = project.path(Path("scenes") / output_file)
        annotation_path = project.path(
            Path("scenes") / f"{Path(output_file).stem}.annotation.json"
        )
        output_path = project.path(
            Path("scenes") / f"{Path(output_file).stem}-whiteboard.mp4"
        )
        if not image_path.is_file():
            raise RenderTimingError(f"场景图片不存在: {image_path}")
        if not annotation_path.is_file():
            raise RenderTimingError(f"场景 annotation 不存在: {annotation_path}")
        annotation = validate_annotation(
            _load_json(annotation_path, "annotation"),
            project=project,
            timing_scene=timing_scene,
            timing_plan_sha256=frozen.timing_plan_sha256,
            render_profile_sha256=frozen.render_profile_sha256,
            active_timeline=frozen.active_timeline,
            audio_sha256=frozen.audio_sha256,
            allow_v1_disabled_compat=allow_v1_disabled_compat,
        )
        resolved.append(
            FormalSceneRender(
                project=project,
                scene_id=scene_id,
                image_path=image_path,
                annotation_path=annotation_path,
                output_path=output_path,
                timing_scene=copy.deepcopy(timing_scene),
                timing_plan_sha256=frozen.timing_plan_sha256,
                timing_plan_file=frozen.timing_plan_file,
                render_profile_sha256=frozen.render_profile_sha256,
                active_timeline=copy.deepcopy(frozen.active_timeline),
                audio_sha256=frozen.audio_sha256,
                full_approval_identity_hash=frozen.full_approval_identity_hash,
                annotation=annotation,
                compatibility_mode=(
                    "schema-v1-disabled-readonly" if project.schema_version == 1 else None
                ),
            )
        )
    return tuple(resolved)


def resolve_formal_scene(
    project: Project,
    scene_id: str,
    *,
    context: FormalValidationContext | None = None,
    allow_v1_disabled_compat: bool = False,
) -> FormalSceneRender:
    """兼容单幕入口；显式 context 时不重复全局深验。"""

    return resolve_formal_scenes(
        project,
        [scene_id],
        context=context,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )[0]


def local_frame_boundary(local_ms: int, *, scene_start_ms: int, scene_start_frame: int, fps: int) -> int:
    """Map a scene-local millisecond boundary onto the cumulative global frame clock."""
    return ((scene_start_ms + local_ms) * fps + 999) // 1000 - scene_start_frame


def render_identity(context: FormalSceneRender, *, render_options: Mapping[str, Any]) -> str:
    scene = context.timing_scene
    return sha256_json(
        {
            "contractVersion": RENDER_CONTRACT_VERSION,
            "projectId": context.project.project_id,
            "sceneId": context.scene_id,
            "imageSha256": sha256_file(context.image_path),
            "annotationSha256": sha256_file(context.annotation_path),
            "timingPlanSha256": context.timing_plan_sha256,
            "renderProfileSha256": context.render_profile_sha256,
            "activeTimeline": context.active_timeline,
            "audioSha256": context.audio_sha256,
            "fullApprovalIdentityHash": context.full_approval_identity_hash,
            "frameRange": {
                "startFrame": scene["startFrame"],
                "endFrameExclusive": scene["endFrameExclusive"],
                "frameCount": scene["frameCount"],
            },
            "renderOptions": dict(render_options),
        }
    )


def update_render_manifest(
    context: FormalSceneRender,
    *,
    media: Mapping[str, Any],
    render_options: Mapping[str, Any],
) -> dict[str, Any]:
    path = context.project.path(RENDER_MANIFEST_FILE)
    if path.is_file():
        manifest = _load_json(path, "render manifest")
        if manifest.get("schemaVersion") != 1 or manifest.get("projectId") != context.project.project_id:
            raise RenderTimingError("既有 render manifest 与 current project 不一致")
    else:
        manifest = {
            "schemaVersion": 1,
            "contractVersion": RENDER_CONTRACT_VERSION,
            "projectId": context.project.project_id,
            "scenes": {},
            "sceneReviewApproval": None,
        }
    scene = context.timing_scene
    options = dict(render_options)
    identity = render_identity(context, render_options=options)
    manifest["scenes"][context.scene_id] = {
        "renderIdentityHash": identity,
        "outputFile": context.output_path.relative_to(context.project.root).as_posix(),
        "image": {
            "file": context.image_path.relative_to(context.project.root).as_posix(),
            "sha256": sha256_file(context.image_path),
        },
        "annotation": {
            "file": context.annotation_path.relative_to(context.project.root).as_posix(),
            "sha256": sha256_file(context.annotation_path),
        },
        "timingPlan": {
            "file": context.timing_plan_file,
            "sha256": context.timing_plan_sha256,
            "activeTimeline": context.active_timeline,
        },
        "renderProfileSha256": context.render_profile_sha256,
        "audioSha256": context.audio_sha256,
        "fullApprovalIdentityHash": context.full_approval_identity_hash,
        "frameRange": {
            "startFrame": scene["startFrame"],
            "endFrameExclusive": scene["endFrameExclusive"],
            "frameCount": scene["frameCount"],
        },
        "renderOptions": options,
        "compatibilityMode": context.compatibility_mode,
        "media": copy.deepcopy(dict(media)),
    }
    # 每次成功发布正式 scene 都代表用户需要重新审阅整批 current bundle。
    # 即使确定性重渲染碰巧产生相同字节，也不得沿用旧人工批准。
    manifest["sceneReviewApproval"] = None
    write_json_atomic(path, manifest)
    return manifest


__all__ = [
    "FormalValidationContext",
    "FormalSceneRender",
    "RENDER_CONTRACT_VERSION",
    "RENDER_MANIFEST_FILE",
    "RenderTimingError",
    "build_formal_validation_context",
    "local_frame_boundary",
    "render_identity",
    "resolve_formal_scene",
    "resolve_formal_scenes",
    "update_render_manifest",
    "validate_annotation",
    "validate_formal_context_current",
]

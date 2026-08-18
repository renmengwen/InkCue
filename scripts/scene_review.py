#!/usr/bin/env python3
"""构建并检查正式场景批量人工 review 的 current identity。"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .media_validation import MediaValidationError, bind_validated_video, validate_video
    from .project_workspace import (
        Project,
        ProjectValidationError,
        ProjectWorkspace,
        WorkspaceError,
        sha256_file,
        sha256_json,
    )
    from .render_timing import (
        RENDER_MANIFEST_FILE,
        RenderTimingError,
        build_formal_validation_context,
        render_identity,
        resolve_formal_scenes,
    )
except ImportError:  # pragma: no cover - direct script execution
    from media_validation import MediaValidationError, bind_validated_video, validate_video
    from project_workspace import (
        Project,
        ProjectValidationError,
        ProjectWorkspace,
        WorkspaceError,
        sha256_file,
        sha256_json,
    )
    from render_timing import (
        RENDER_MANIFEST_FILE,
        RenderTimingError,
        build_formal_validation_context,
        render_identity,
        resolve_formal_scenes,
    )


SCENE_REVIEW_CONTRACT_VERSION = "whiteboard-scene-review-bundle-v1"


class SceneReviewStaleError(ValueError):
    """正式场景 bundle 缺失、stale 或与 current 项目不一致。"""


class SceneReviewGateError(SceneReviewStaleError):
    """current 场景 bundle 尚未获得 identity 绑定的人工批准。"""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SceneReviewStaleError(f"{label} 缺失或不是对象")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise SceneReviewStaleError(f"{label} 不是有效 SHA-256")
    return value


def load_render_manifest(project: Project) -> tuple[Path, dict[str, Any]]:
    path = project.path(RENDER_MANIFEST_FILE)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SceneReviewStaleError("缺少 current render manifest") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SceneReviewStaleError(f"无法读取 current render manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SceneReviewStaleError("render manifest 顶层必须是对象")
    if manifest.get("schemaVersion") != 1 or manifest.get("projectId") != project.project_id:
        raise SceneReviewStaleError("render manifest 与 current project 不一致")
    if not isinstance(manifest.get("scenes"), dict):
        raise SceneReviewStaleError("render manifest.scenes 缺失或不是对象")
    return path, manifest


def _current_generation_plan_sha256(project: Project) -> str:
    plan_path = project.path("planning/generation-plan.json")
    if plan_path.is_file():
        return sha256_file(plan_path)
    return sha256_json(project.plan)


def _current_timing_plan(project: Project) -> tuple[str | None, str]:
    if project.timing_plan_persisted:
        return "planning/timing-plan.json", sha256_file(project.timing_plan_path)
    return None, sha256_json(project.timing_plan)


def _deep_receipt(scene_record: Mapping[str, Any]) -> Mapping[str, Any]:
    media = _mapping(scene_record.get("media"), "render manifest scene.media")
    validation = _mapping(media.get("validation"), "render manifest scene.media.validation")
    return _mapping(validation.get("deepReceipt"), "render manifest scene deep receipt")


def _expected_output(project: Project, generation_scene: Mapping[str, Any]) -> Path:
    output_file = generation_scene.get("outputFile")
    if not isinstance(output_file, str) or not output_file:
        raise SceneReviewStaleError("generation plan scene.outputFile 无效")
    return project.path(Path("scenes") / f"{Path(output_file).stem}-whiteboard.mp4")


def build_scene_review_bundle(
    project: Project,
    *,
    force_deep: bool = False,
    include_bound_media: bool = False,
) -> dict[str, Any]:
    """重算全部正式 scene 的 current bundle；不读取或写入人工批准。"""

    _, manifest = load_render_manifest(project)
    plan_scenes = project.plan.get("scenes")
    timing_scenes = project.timing_plan.get("scenes")
    if not isinstance(plan_scenes, list) or not plan_scenes:
        raise SceneReviewStaleError("current generation plan 没有场景")
    if not isinstance(timing_scenes, list) or len(timing_scenes) != len(plan_scenes):
        raise SceneReviewStaleError("generation/timing plan 场景数量不一致")
    scene_ids = [item.get("sceneId") for item in plan_scenes if isinstance(item, Mapping)]
    if len(scene_ids) != len(plan_scenes) or any(not isinstance(value, str) for value in scene_ids):
        raise SceneReviewStaleError("generation plan sceneId 无效")
    if len(set(scene_ids)) != len(scene_ids):
        raise SceneReviewStaleError("generation plan sceneId 重复")

    try:
        context = build_formal_validation_context(project)
        formal_scenes = resolve_formal_scenes(
            project,
            scene_ids,
            context=context,
            allow_v1_disabled_compat=(project.schema_version == 1),
        )
    except RenderTimingError as exc:
        raise SceneReviewStaleError(f"formal scene binding stale: {exc}") from exc

    timing_file, timing_sha256 = _current_timing_plan(project)
    render_profile_sha256 = sha256_json(project.render_profile)
    manifest_scenes = manifest["scenes"]
    bundle_scenes: list[dict[str, Any]] = []
    validated_scene_media: list[dict[str, Any]] = []
    for generation_scene, timing_scene, formal in zip(
        plan_scenes, timing_scenes, formal_scenes, strict=True
    ):
        scene_id = formal.scene_id
        if timing_scene.get("sceneId") != scene_id:
            raise SceneReviewStaleError("generation/timing plan 场景顺序不一致")
        record = _mapping(manifest_scenes.get(scene_id), f"render manifest scene {scene_id}")
        expected_output = _expected_output(project, generation_scene)
        output_file = expected_output.relative_to(project.root).as_posix()
        if record.get("outputFile") != output_file or formal.output_path != expected_output:
            raise SceneReviewStaleError(f"{scene_id} outputFile stale")
        render_options = _mapping(record.get("renderOptions"), f"{scene_id}.renderOptions")
        current_render_identity = render_identity(formal, render_options=render_options)
        recorded_render_identity = _sha256(
            record.get("renderIdentityHash"), f"{scene_id}.renderIdentityHash"
        )
        if recorded_render_identity != current_render_identity:
            raise SceneReviewStaleError(f"{scene_id} render identity stale")
        if record.get("renderProfileSha256") != render_profile_sha256:
            raise SceneReviewStaleError(f"{scene_id} render profile binding stale")
        record_timing = _mapping(record.get("timingPlan"), f"{scene_id}.timingPlan")
        if (
            record_timing.get("file") != timing_file
            or record_timing.get("sha256") != timing_sha256
            or record_timing.get("activeTimeline") != project.timing_plan.get("activeTimeline")
        ):
            raise SceneReviewStaleError(f"{scene_id} timing plan binding stale")
        expected_frame_range = {
            "startFrame": timing_scene.get("startFrame"),
            "endFrameExclusive": timing_scene.get("endFrameExclusive"),
            "frameCount": timing_scene.get("frameCount"),
        }
        if record.get("frameRange") != expected_frame_range:
            raise SceneReviewStaleError(f"{scene_id} frame range stale")
        if not expected_output.is_file():
            raise SceneReviewStaleError(f"{scene_id} 正式 scene 媒体缺失")
        media_record = _mapping(record.get("media"), f"{scene_id}.media")
        media_sha256 = sha256_file(expected_output)
        media_bytes = expected_output.stat().st_size
        if media_record.get("sha256") != media_sha256 or media_record.get("bytes") != media_bytes:
            raise SceneReviewStaleError(f"{scene_id} 正式 scene 媒体字节 stale")
        if force_deep:
            bound_media = validate_video(
                expected_output,
                render_profile=project.render_profile,
                expected_frame_count=timing_scene["frameCount"],
                expected_audio_streams=0,
                deep_receipt=_deep_receipt(record),
                force_deep=True,
            )
        else:
            bound_media = bind_validated_video(
                expected_output,
                render_profile=project.render_profile,
                expected_frame_count=timing_scene["frameCount"],
                expected_audio_streams=0,
                deep_receipt=_deep_receipt(record),
            )
        if bound_media.get("sha256") != media_sha256 or bound_media.get("bytes") != media_bytes:
            raise SceneReviewStaleError(f"{scene_id} 技术 receipt 未绑定 current 媒体")
        bundle_scenes.append(
            {
                "sceneId": scene_id,
                "outputFile": output_file,
                "renderIdentityHash": current_render_identity,
                "mediaSha256": media_sha256,
                "mediaBytes": media_bytes,
                "frameRange": expected_frame_range,
            }
        )
        if include_bound_media:
            validated_scene_media.append(
                {
                    "sceneId": scene_id,
                    "outputFile": output_file,
                    "media": copy.deepcopy(dict(bound_media)),
                }
            )

    identity_payload = {
        "contractVersion": SCENE_REVIEW_CONTRACT_VERSION,
        "projectId": project.project_id,
        "generationPlanSha256": _current_generation_plan_sha256(project),
        "sceneOrder": scene_ids,
        "timingPlan": {
            "file": timing_file,
            "sha256": timing_sha256,
            "activeTimeline": copy.deepcopy(project.timing_plan.get("activeTimeline")),
        },
        "renderProfileSha256": render_profile_sha256,
        "scenes": bundle_scenes,
    }
    result = {
        **identity_payload,
        "identityHash": sha256_json(identity_payload),
    }
    if include_bound_media:
        # 仅供同一进程的 merge 消费；不进入 scene review identity 或人工批准。
        result["validatedSceneMedia"] = validated_scene_media
    return result


def inspect_scene_review(project: Project) -> dict[str, Any]:
    bundle = build_scene_review_bundle(project)
    _, manifest = load_render_manifest(project)
    approval = manifest.get("sceneReviewApproval")
    approved = bool(
        isinstance(approval, Mapping)
        and approval.get("approved") is True
        and approval.get("identityHash") == bundle["identityHash"]
        and approval.get("contractVersion") == SCENE_REVIEW_CONTRACT_VERSION
    )
    return {
        "ok": True,
        "projectId": project.project_id,
        "sceneCount": len(bundle["scenes"]),
        "sceneReviewIdentityHash": bundle["identityHash"],
        "approved": approved,
        "approval": copy.deepcopy(dict(approval)) if isinstance(approval, Mapping) else None,
        "bundle": bundle,
        "approvalWritten": False,
        "userConfirmationRequired": not approved,
    }


def assert_current_scene_review_approval(
    project: Project,
    *,
    inputs: Sequence[Path] | None = None,
    force_deep: bool = False,
) -> dict[str, Any]:
    bundle = build_scene_review_bundle(
        project,
        force_deep=force_deep,
        include_bound_media=True,
    )
    if inputs is not None:
        actual = [path.resolve() for path in inputs]
        expected = [project.path(item["outputFile"]).resolve() for item in bundle["scenes"]]
        if actual != expected:
            raise SceneReviewGateError("合并输入未按 current scene review bundle 顺序完整绑定")
    _, manifest = load_render_manifest(project)
    approval = manifest.get("sceneReviewApproval")
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        raise SceneReviewGateError("缺少 current scene review 人工批准")
    if (
        approval.get("contractVersion") != SCENE_REVIEW_CONTRACT_VERSION
        or approval.get("identityHash") != bundle["identityHash"]
    ):
        raise SceneReviewGateError("scene review approval stale 或 identity 不匹配")
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查正式场景批量 review bundle current identity")
    parser.add_argument("--project", required=True, help="项目根目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = ProjectWorkspace.from_config()
        project = workspace.load_project(args.project)
        result = inspect_scene_review(project)
    except (WorkspaceError, ProjectValidationError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except SceneReviewStaleError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except (MediaValidationError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"SCENE_REVIEW_IDENTITY={result['sceneReviewIdentityHash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

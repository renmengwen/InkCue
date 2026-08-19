#!/usr/bin/env python3
"""按 current timing plan 合并静音场景并发布 clean video 母版。"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from media_validation import (
    MediaValidationError,
    atomic_publish,
    bind_validated_video,
    validate_video,
)
from project_workspace import (
    Project,
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    sha256_file,
    sha256_json,
    write_json_atomic,
)
from scene_review import SceneReviewGateError, SceneReviewStaleError, assert_current_scene_review_approval
from cover_frame import attach_cover_manifest, attach_cover_review_manifest, cover_record, replace_first_frame


DELIVERY_MANIFEST_KEYS = {
    "schemaVersion",
    "projectId",
    "voiceoverMode",
    "timingPlan",
    "cleanVideo",
    "subtitles",
    "captionedVideo",
    "final",
    "finalApproval",
    "cover",
}
DELIVERY_MANIFEST_OPTIONAL_KEYS = {"coverReview"}


def _write_concat_list(inputs: list[Path], list_path: Path) -> None:
    with list_path.open("x", encoding="utf-8", newline="\n") as handle:
        for input_path in inputs:
            escaped = input_path.resolve().as_posix().replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")


def _ffmpeg_concat_copy(inputs: list[Path], output: Path, list_path: Path) -> bool:
    """优先按码流复制拼接；失败由调用方尝试明确重编码。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaValidationError("缺少必需的可执行文件: ffmpeg")
    _write_concat_list(inputs, list_path)
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode == 0


def _ffmpeg_concat_reencode(inputs: list[Path], output: Path, list_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaValidationError("缺少必需的可执行文件: ffmpeg")
    if not list_path.is_file():
        _write_concat_list(inputs, list_path)
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            str(output),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        summary = completed.stderr.strip()[-1000:]
        raise MediaValidationError(f"FFmpeg 场景重编码合并失败: {summary or '无错误输出'}")


def _find_project_root(
    explicit: str | None,
    inputs: list[Path],
    output: Path | None,
) -> Path:
    if explicit:
        return Path(explicit).resolve()
    probes = [*inputs]
    if output is not None:
        probes.insert(0, output)
    roots: list[Path] = []
    for probe in probes:
        current = probe.resolve().parent
        for candidate in (current, *current.parents):
            if (candidate / "project.json").is_file():
                roots.append(candidate)
                break
    if not roots or any(root != roots[0] for root in roots):
        raise ProjectValidationError("无法从输入/输出确定唯一项目根目录；请传入 --project")
    return roots[0]


def _project_owned_path(project: Project, path: Path) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(project.root)
    except ValueError as exc:
        raise ProjectValidationError(f"路径不在项目根目录内: {resolved}") from exc
    if not relative.parts:
        raise ProjectValidationError("文件路径不得指向项目根目录")
    return project.path(relative)


def _timing_plan_identity(project: Project) -> tuple[str | None, str]:
    if project.timing_plan_persisted:
        return "planning/timing-plan.json", sha256_file(project.timing_plan_path)
    return None, sha256_json(project.timing_plan)


def _new_delivery_manifest(project: Project) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "timingPlan": None,
        "cleanVideo": None,
        "subtitles": None,
        "captionedVideo": None,
        "final": None,
        "finalApproval": None,
        "cover": None,
        "coverReview": None,
    }


def _load_delivery_manifest(project: Project) -> dict[str, Any]:
    path = project.path("manifests/delivery-manifest.json")
    if not path.exists():
        return _new_delivery_manifest(project)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"无法读取 delivery manifest: {exc}") from exc
    if not isinstance(manifest, dict) or not DELIVERY_MANIFEST_KEYS.issubset(manifest) or (
        set(manifest) - DELIVERY_MANIFEST_KEYS - DELIVERY_MANIFEST_OPTIONAL_KEYS
    ):
        raise ProjectValidationError("delivery manifest 顶层字段不符合冻结合同")
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("projectId") != project.project_id
        or manifest.get("voiceoverMode") != project.voiceover_mode
    ):
        raise ProjectValidationError("delivery manifest 项目身份不匹配")
    return manifest


def _clean_video_entry(media: dict[str, Any]) -> dict[str, Any]:
    video = media["streams"]["video"][0]
    return {
        "file": "output/final-video-only.mp4",
        "sha256": media["sha256"],
        "bytes": media["bytes"],
        "durationMs": media["durationMs"],
        "frameCount": video["frameCount"],
        "fps": video["fps"],
        "technicalValidation": media["validation"],
    }


def _update_delivery_manifest(project: Project, media: dict[str, Any]) -> None:
    manifest = _load_delivery_manifest(project)
    timing_file, timing_hash = _timing_plan_identity(project)
    timing_plan = project.timing_plan
    scenes = timing_plan["scenes"]
    frame_count = scenes[-1]["endFrameExclusive"]
    manifest["timingPlan"] = {
        "file": timing_file,
        "sha256": timing_hash,
        "voiceoverMode": project.voiceover_mode,
        "activeTimeline": timing_plan["activeTimeline"],
        "renderProfileSha256": timing_plan["renderProfileSha256"],
        "frameRounding": project.render_profile["frameRounding"],
        "frameCount": frame_count,
    }
    manifest["cleanVideo"] = _clean_video_entry(media)
    attach_cover_manifest(manifest, cover_record(project))
    attach_cover_review_manifest(manifest, project)
    write_json_atomic(project.path("manifests/delivery-manifest.json"), manifest)


def _produce_candidate(
    inputs: list[Path],
    candidate: Path,
    list_path: Path,
    *,
    project: Project,
    expected_frame_count: int,
) -> dict[str, Any]:
    if len(inputs) == 1:
        shutil.copyfile(inputs[0], candidate)
    else:
        copied = _ffmpeg_concat_copy(inputs, candidate, list_path)
        if copied:
            try:
                media = validate_video(
                    candidate,
                    render_profile=project.render_profile,
                    expected_frame_count=expected_frame_count,
                    expected_audio_streams=0,
                )
            except MediaValidationError:
                candidate.unlink(missing_ok=True)
            else:
                # A valid copy still needs the optional cover replacement below.
                pass
        if not candidate.is_file():
            _ffmpeg_concat_reencode(inputs, candidate, list_path)
    # The optional cover is inserted only after the authoritative scene
    # concat, so scene timing and total frame count remain unchanged.
    if cover_record(project) is not None:
        replaced = candidate.with_name(candidate.stem + ".cover.mp4")
        replace_first_frame(
            candidate,
            replaced,
            project=project,
            expected_frame_count=expected_frame_count,
        )
        candidate.unlink(missing_ok=True)
        replaced.replace(candidate)
    media = validate_video(
        candidate,
        render_profile=project.render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=0,
    )
    return media


def _validate_scene_input_shape(project: Project, inputs: list[Path]) -> None:
    """只校验集合形状；媒体 current/deep 证据由 scene review Gate 单次提供。"""
    scenes = project.timing_plan["scenes"]
    if not scenes:
        raise ProjectValidationError("current timing plan 没有可合并场景")
    if len(inputs) != len(scenes):
        raise ProjectValidationError(
            f"输入场景数必须为 {len(scenes)}，实际为 {len(inputs)}"
        )
    if len(set(inputs)) != len(inputs):
        raise ProjectValidationError("输入场景路径不得重复")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并静音场景为 final-video-only.mp4")
    parser.add_argument("--inputs", nargs="+", required=True, help="按 timing plan 顺序的场景 MP4")
    parser.add_argument("--output", help="输出路径；默认 <project>/output/final-video-only.mp4")
    parser.add_argument("--project", help="项目根目录；省略时从输入/输出向上查找 project.json")
    parser.add_argument("--force-deep", action="store_true", help="忽略旧 receipt 并重新深验输入")
    args = parser.parse_args(argv)

    input_args = [Path(value) for value in args.inputs]
    output_arg = Path(args.output) if args.output else None
    try:
        project_root = _find_project_root(args.project, input_args, output_arg)
        workspace = ProjectWorkspace.from_config()
        project = workspace.load_project(project_root)
        inputs = [_project_owned_path(project, path) for path in input_args]
        output = (
            _project_owned_path(project, output_arg)
            if output_arg is not None
            else project.path("output/final-video-only.mp4")
        )
        formal_output = project.path("output/final-video-only.mp4")
        if output != formal_output:
            raise ProjectValidationError(
                "静音场景合并的正式输出必须为 output/final-video-only.mp4"
            )
        if output in inputs:
            raise ProjectValidationError("正式 clean video 输出不得覆盖输入场景")
        _validate_scene_input_shape(project, inputs)
        approved_bundle = assert_current_scene_review_approval(
            project,
            inputs=inputs,
            force_deep=args.force_deep,
        )
        validated_media = approved_bundle.get("validatedSceneMedia")
        if validated_media is not None and (
            not isinstance(validated_media, list) or len(validated_media) != len(inputs)
        ):
            raise SceneReviewStaleError("scene review Gate 未返回完整的逐幕媒体 binding")
        expected_frame_count = project.timing_plan["scenes"][-1]["endFrameExclusive"]
        run_dir = project.create_run_dir(f"merge-{uuid.uuid4().hex}")
    except (SceneReviewGateError, SceneReviewStaleError) as exc:
        print(f"[err] scene review Gate 拒绝合并: {exc}", file=sys.stderr)
        return 5
    except (OSError, WorkspaceError, ProjectValidationError, MediaValidationError, KeyError, TypeError) as exc:
        print(f"[err] 项目、输入或时序无效: {exc}", file=sys.stderr)
        return 2

    candidate = run_dir / "final-video-only.candidate.mp4"
    list_path = run_dir / "concat.txt"
    try:
        media = _produce_candidate(
            inputs,
            candidate,
            list_path,
            project=project,
            expected_frame_count=expected_frame_count,
        )
        atomic_publish(candidate, output)
        media = bind_validated_video(
            output,
            render_profile=project.render_profile,
            expected_frame_count=expected_frame_count,
            expected_audio_streams=0,
            deep_receipt=media["validation"],
        )
        _update_delivery_manifest(project, media)
    except (OSError, ProjectValidationError, MediaValidationError, KeyError, TypeError) as exc:
        print(f"[err] 合并或媒体验证失败: {exc}", file=sys.stderr)
        print(f"FAILED_WORK_DIR={run_dir}", file=sys.stderr)
        return 4

    list_path.unlink(missing_ok=True)
    try:
        run_dir.rmdir()
    except OSError:
        print(f"[warn] 本次合并临时目录未空，已保留: {run_dir}", file=sys.stderr)
    print(f"OUTPUT={output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

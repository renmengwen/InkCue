#!/usr/bin/env python3
"""Render one approved formal scene from four project-local video clips.

This is a narrow scene renderer, not a second delivery pipeline.  It reuses the
current formal timing/annotation bindings, the existing scene media validator,
the render manifest identity, and the existing scene-bundle approval gate.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

import media_validation  # noqa: E402
import project_workspace  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import render_timing  # noqa: E402


CONTRACT_VERSION = "whiteboard-video-mosaic-scene-v1"


class MosaicRenderError(ValueError):
    pass


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MosaicRenderError(f"{label}必须是对象")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MosaicRenderError(f"{label}必须是正整数")
    return value


def _load_config(project: project_workspace.Project, relative: str) -> tuple[Path, dict[str, Any]]:
    path = project.path(relative)
    if path.is_symlink() or not path.is_file():
        raise MosaicRenderError("mosaic config 必须是项目内普通文件")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MosaicRenderError("无法读取 mosaic config") from exc
    value = dict(_mapping(raw, "mosaic config"))
    required = {
        "schemaVersion",
        "contractVersion",
        "sceneId",
        "marginPx",
        "horizontalGapPx",
        "verticalGapPx",
        "backgroundHex",
        "sources",
    }
    if set(value) != required:
        raise MosaicRenderError("mosaic config 字段必须与合同完全一致")
    if value["schemaVersion"] != 1 or value["contractVersion"] != CONTRACT_VERSION:
        raise MosaicRenderError("mosaic config 合同版本无效")
    if not isinstance(value["sceneId"], str) or not value["sceneId"]:
        raise MosaicRenderError("sceneId 必须是非空字符串")
    for key in ("marginPx", "horizontalGapPx", "verticalGapPx"):
        _positive_int(value[key], key)
    background = value["backgroundHex"]
    if not isinstance(background, str) or len(background) != 7 or not background.startswith("#"):
        raise MosaicRenderError("backgroundHex 必须是 #RRGGBB")
    try:
        int(background[1:], 16)
    except ValueError as exc:
        raise MosaicRenderError("backgroundHex 必须是 #RRGGBB") from exc
    canvas = project.plan.get("outputCanvas")
    plan_background = canvas.get("background") if isinstance(canvas, dict) else None
    if background != plan_background:
        raise MosaicRenderError(
            "mosaic backgroundHex 必须与 current generation plan background 一致"
        )
    sources = value["sources"]
    if not isinstance(sources, list) or len(sources) != 4:
        raise MosaicRenderError("首版视频四宫格必须恰好包含 4 个 source")
    normalized_sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_source in enumerate(sources, start=1):
        source = dict(_mapping(raw_source, f"sources[{index}]"))
        if set(source) != {"file"} or not isinstance(source["file"], str):
            raise MosaicRenderError(f"sources[{index}] 只允许项目相对 file")
        if source["file"] in seen:
            raise MosaicRenderError("mosaic source 不得重复")
        seen.add(source["file"])
        source_path = project.path(source["file"])
        if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size <= 0:
            raise MosaicRenderError(f"mosaic source {index} 必须是项目内非空普通文件")
        normalized_sources.append(
            {
                "file": source_path.relative_to(project.root).as_posix(),
                "sha256": project_workspace.sha256_file(source_path),
            }
        )
    value["sources"] = normalized_sources
    return path, value


def _cell_layout(profile: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, int]:
    width = _positive_int(profile.get("width"), "renderProfile.width")
    height = _positive_int(profile.get("height"), "renderProfile.height")
    margin = config["marginPx"]
    gap_x = config["horizontalGapPx"]
    gap_y = config["verticalGapPx"]
    usable_width = width - 2 * margin - gap_x
    usable_height = height - 2 * margin - gap_y
    if usable_width <= 0 or usable_height <= 0 or usable_width % 2 or usable_height % 2:
        raise MosaicRenderError("mosaic margin/gap 无法整分当前画布")
    return {
        "cellWidth": usable_width // 2,
        "cellHeight": usable_height // 2,
        "leftX": margin,
        "rightX": margin + usable_width // 2 + gap_x,
        "topY": margin,
        "bottomY": margin + usable_height // 2 + gap_y,
    }


def _ffmpeg_command(
    ffmpeg: str,
    project: project_workspace.Project,
    config: Mapping[str, Any],
    layout: Mapping[str, int],
    candidate: Path,
    *,
    frame_count: int,
) -> list[str]:
    fps = _positive_int(project.render_profile.get("fps"), "renderProfile.fps")
    background = "0x" + config["backgroundHex"][1:]
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for source in config["sources"]:
        command.extend(["-stream_loop", "-1", "-i", str(project.path(source["file"]))])
    filters: list[str] = []
    for index in range(4):
        filters.append(
            f"[{index}:v]fps={fps},"
            f"scale={layout['cellWidth']}:{layout['cellHeight']}:force_original_aspect_ratio=decrease,"
            f"pad={layout['cellWidth']}:{layout['cellHeight']}:(ow-iw)/2:(oh-ih)/2:color={background},"
            f"setsar=1[v{index}]"
        )
    positions = (
        f"{layout['leftX']}_{layout['topY']}|"
        f"{layout['rightX']}_{layout['topY']}|"
        f"{layout['leftX']}_{layout['bottomY']}|"
        f"{layout['rightX']}_{layout['bottomY']}"
    )
    filters.append(
        "[v0][v1][v2][v3]"
        f"xstack=inputs=4:layout={positions}:fill={background},"
        f"pad={project.render_profile['width']}:{project.render_profile['height']}:0:0:color={background},"
        f"fade=t=in:st=0:d=0.4:color={background},"
        f"trim=end_frame={frame_count},setpts=PTS-STARTPTS[outv]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-frames:v",
            str(frame_count),
            "-movflags",
            "+faststart",
            "-y",
            str(candidate),
        ]
    )
    return command


def validate_current_render_options(
    project: project_workspace.Project,
    render_options: Mapping[str, Any],
) -> None:
    """Fail closed when a persisted mosaic config or source clip has changed."""

    if render_options.get("renderMode") != CONTRACT_VERSION:
        return
    config_relative = render_options.get("mosaicConfig")
    if not isinstance(config_relative, str):
        raise MosaicRenderError("mosaic renderOptions 缺少 config")
    config_path, config = _load_config(project, config_relative)
    if project_workspace.sha256_file(config_path) != render_options.get("mosaicConfigSha256"):
        raise MosaicRenderError("mosaic config bytes stale")
    if config.get("sources") != render_options.get("sources"):
        raise MosaicRenderError("mosaic source binding stale")
    if _cell_layout(project.render_profile, config) != render_options.get("layout"):
        raise MosaicRenderError("mosaic layout binding stale")
    if config.get("backgroundHex") != render_options.get("backgroundHex"):
        raise MosaicRenderError("mosaic background binding stale")


def render(project_root: str, scene_id: str, config_file: str) -> dict[str, Any]:
    project = project_workspace.load_project(project_root)
    config_path, config = _load_config(project, config_file)
    if config["sceneId"] != scene_id:
        raise MosaicRenderError("CLI sceneId 与 mosaic config 不一致")
    frozen = render_timing.build_formal_validation_context(project)
    context = render_timing.resolve_formal_scene(project, scene_id, context=frozen)
    render_timing.validate_formal_context_current(project, frozen)
    layout = _cell_layout(project.render_profile, config)
    source_bindings = list(config["sources"])
    config_sha256 = project_workspace.sha256_file(config_path)
    run_dir = project.create_run_dir(f"render-mosaic-{uuid.uuid4().hex}")
    candidate = run_dir / "scene.mosaic.candidate.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MosaicRenderError("缺少必需的 ffmpeg")
    command = _ffmpeg_command(
        ffmpeg,
        project,
        config,
        layout,
        candidate,
        frame_count=context.timing_scene["frameCount"],
    )
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown ffmpeg error"
        raise MosaicRenderError(f"视频四宫格编码失败: {message}")
    render_timing.validate_formal_context_current(project, frozen)
    current_sources: list[dict[str, str]] = []
    for source in source_bindings:
        source_path = project.path(source["file"])
        current_sources.append(
            {"file": source["file"], "sha256": project_workspace.sha256_file(source_path)}
        )
    if current_sources != source_bindings or project_workspace.sha256_file(config_path) != config_sha256:
        raise MosaicRenderError("mosaic config/source 在渲染期间发生变化")
    candidate_media = media_validation.validate_video(
        candidate,
        render_profile=project.render_profile,
        expected_frame_count=context.timing_scene["frameCount"],
        expected_audio_streams=0,
    )
    media = render_stream_whiteboard._publish_and_bind_scene(
        candidate,
        context.output_path,
        render_profile=project.render_profile,
        expected_frame_count=context.timing_scene["frameCount"],
        deep_receipt=render_stream_whiteboard._deep_receipt(candidate_media),
    )
    render_options = {
        "renderMode": CONTRACT_VERSION,
        "mosaicConfig": config_path.relative_to(project.root).as_posix(),
        "mosaicConfigSha256": config_sha256,
        "sources": source_bindings,
        "layout": dict(layout),
        "backgroundHex": config["backgroundHex"],
        "cleanFirstFrame": "fade-from-paper-0.4s",
        "audioStreams": 0,
    }
    manifest = render_timing.update_render_manifest(
        context,
        media=media,
        render_options=render_options,
    )
    try:
        run_dir.rmdir()
    except OSError:
        pass
    return {
        "contractVersion": CONTRACT_VERSION,
        "status": "PASS",
        "projectId": project.project_id,
        "sceneId": scene_id,
        "outputFile": context.output_path.relative_to(project.root).as_posix(),
        "frameCount": context.timing_scene["frameCount"],
        "renderIdentityHash": manifest["scenes"][scene_id]["renderIdentityHash"],
        "mediaSha256": media["sha256"],
        "sourceCount": 4,
        "approvalWritten": False,
        "userConfirmationRequired": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把四段项目内视频渲染为一个正式四宫格 scene")
    parser.add_argument("--project", required=True)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--config", required=True, help="项目内相对 mosaic config 路径")
    args = parser.parse_args(argv)
    try:
        result = render(args.project, args.scene_id, args.config)
    except (MosaicRenderError, project_workspace.WorkspaceError, render_timing.RenderTimingError, media_validation.MediaValidationError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 4
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

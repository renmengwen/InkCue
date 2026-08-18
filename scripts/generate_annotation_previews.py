#!/usr/bin/env python3
"""Phase 5 batch annotation previews and ordered contact sheet generation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

try:
    from .bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from .annotation_review import write_annotation_review_technical
    from .project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
    )
    from .render_annotation_preview import render_annotation_preview
    from .render_timing import (
        FormalSceneRender,
        RenderTimingError,
        build_formal_validation_context,
        resolve_formal_scenes,
        validate_formal_context_current,
    )
except ImportError:  # pragma: no cover - direct script execution
    from bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from annotation_review import write_annotation_review_technical
    from project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
    )
    from render_annotation_preview import render_annotation_preview
    from render_timing import (
        FormalSceneRender,
        RenderTimingError,
        build_formal_validation_context,
        resolve_formal_scenes,
        validate_formal_context_current,
    )


PREVIEW_BATCH_CONTRACT = "whiteboard-annotation-preview-batch-v1"
EXPECTED_SIZE = (1920, 1080)
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/][^\s\"']+)")
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\s*[:=]\s*[^\s,;}]+"
)


class AnnotationPreviewBatchError(ValueError):
    """Stable local contract error for Phase 5 preview generation."""


@dataclass(frozen=True)
class AnnotationPreviewTask:
    sequence: int
    scene_id: str
    scene_name: str
    duration_ms: int
    element_count: int
    formal: FormalSceneRender
    annotation_sha256: str
    candidate_path: Path
    output_path: Path


@dataclass(frozen=True)
class AnnotationPreviewCandidate:
    task: AnnotationPreviewTask
    sha256: str
    byte_count: int


def annotation_binding_sha256(formals: Sequence[FormalSceneRender]) -> str:
    return sha256_json(
        [
            {
                "sceneId": formal.scene_id,
                "annotationSha256": sha256_file(formal.annotation_path),
                "timingPlanSha256": formal.timing_plan_sha256,
                "renderProfileSha256": formal.render_profile_sha256,
                "audioSha256": formal.audio_sha256,
            }
            for formal in formals
        ]
    )


def _sanitize_error(error: BaseException | str) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ")
    message = _SENSITIVE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    return _WINDOWS_PATH_RE.sub("<path>", message)


def _scene_name(scene: Mapping[str, Any]) -> str:
    for key in ("name", "title", "coreIdea", "visualSubject"):
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    output = scene.get("outputFile")
    return Path(output).stem if isinstance(output, str) else str(scene.get("sceneId", "scene"))


def _save_candidate_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=False)
    image.save(path, format="PNG", compress_level=1, optimize=False)


def _validate_preview_candidate(path: Path) -> tuple[str, int]:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG":
                raise AnnotationPreviewBatchError("preview candidate 必须是 PNG")
            if image.mode != "RGB":
                raise AnnotationPreviewBatchError("preview candidate 必须是 RGB")
            if image.size != EXPECTED_SIZE:
                raise AnnotationPreviewBatchError("preview candidate 必须是 1920x1080")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        if isinstance(exc, AnnotationPreviewBatchError):
            raise
        raise AnnotationPreviewBatchError("preview candidate 无法完整解码") from exc
    return sha256_file(path), path.stat().st_size


def _render_task(task: AnnotationPreviewTask) -> WorkerOutcome[AnnotationPreviewCandidate]:
    try:
        with Image.open(task.formal.image_path) as source:
            source.load()
            rendered = render_annotation_preview(source, task.formal.annotation)
        _save_candidate_png(rendered, task.candidate_path)
        digest, byte_count = _validate_preview_candidate(task.candidate_path)
        return WorkerOutcome.success(AnnotationPreviewCandidate(task, digest, byte_count))
    except Exception as exc:
        return WorkerOutcome.failed(
            WorkerFailure(type(exc).__name__, _sanitize_error(exc), retryable=False)
        )


def _publish_bytes_atomic(candidate: Path, target: Path, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        data = candidate.read_bytes()
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(temporary) != expected_sha256:
            raise AnnotationPreviewBatchError("preview 原子发布前 SHA 核对失败")
        os.replace(temporary, target)
        if sha256_file(target) != expected_sha256:
            raise AnnotationPreviewBatchError("正式 preview SHA 与候选不一致")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _contact_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)


def build_annotation_preview_contact_sheet(
    candidates: Sequence[AnnotationPreviewCandidate],
) -> Image.Image:
    if not candidates:
        raise AnnotationPreviewBatchError("contact sheet 至少需要一个 preview")
    columns = min(2, len(candidates))
    rows = (len(candidates) + columns - 1) // columns
    tile_width, tile_height = 760, 500
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), (245, 239, 221))
    draw = ImageDraw.Draw(sheet)
    title_font = _contact_font(22)
    meta_font = _contact_font(18)
    for index, candidate in enumerate(candidates):
        task = candidate.task
        column, row = index % columns, index // columns
        left, top = column * tile_width, row * tile_height
        with Image.open(candidate.task.output_path) as preview:
            preview.load()
            thumbnail = preview.copy()
        thumbnail.thumbnail((720, 405), Image.Resampling.LANCZOS)
        sheet.paste(thumbnail, (left + 20, top + 20))
        draw.text(
            (left + 20, top + 432),
            f"{task.scene_id}  {task.scene_name}",
            font=title_font,
            fill=(45, 45, 45),
        )
        draw.text(
            (left + 20, top + 464),
            f"元素 {task.element_count} · 时长 {task.duration_ms} ms",
            font=meta_font,
            fill=(80, 80, 80),
        )
    return sheet


def _current_bindings_unchanged(project: Project, tasks: Sequence[AnnotationPreviewTask], context: Any) -> None:
    current = load_project(project.root)
    validate_formal_context_current(current, context)
    for task in tasks:
        if not task.formal.annotation_path.is_file():
            raise RenderTimingError("batch 期间 annotation 已删除")
        if sha256_file(task.formal.annotation_path) != task.annotation_sha256:
            raise RenderTimingError("batch 期间 annotation binding 已变化")


def generate_annotation_preview_batch(
    workspace: WorkspaceConfig,
    project: Project,
    *,
    allow_v1_disabled_compat: bool = False,
    executor_factory: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Validate the complete Phase 4 technical gate, render, and publish in plan order."""

    context = build_formal_validation_context(project)
    scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    formals = resolve_formal_scenes(
        project,
        scene_ids,
        context=context,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )
    if len(formals) != len(scene_ids):
        raise AnnotationPreviewBatchError("Phase 4 未覆盖 generation plan 全部 scene")

    generation = {scene["sceneId"]: scene for scene in project.plan["scenes"]}
    configured = workspace.for_stage("annotationPreview")
    run_root = project.root / ".work" / f"annotation-preview-{uuid.uuid4().hex}"
    tasks: list[AnnotationPreviewTask] = []
    for sequence, formal in enumerate(formals, start=1):
        scene = generation[formal.scene_id]
        stem = Path(scene["outputFile"]).stem
        tasks.append(
            AnnotationPreviewTask(
                sequence=sequence,
                scene_id=formal.scene_id,
                scene_name=_scene_name(scene),
                duration_ms=int(formal.timing_scene["endMs"] - formal.timing_scene["startMs"]),
                element_count=len(formal.annotation["elements"]),
                formal=formal,
                annotation_sha256=sha256_file(formal.annotation_path),
                candidate_path=run_root / f"{sequence:04d}-{formal.scene_id}" / "candidate.png",
                output_path=project.root / "previews" / f"{stem}-annotation-preview.png",
            )
        )

    execution_kwargs: dict[str, Any] = {}
    if executor_factory is not None:
        execution_kwargs["executor_factory"] = executor_factory
    report = execute_bounded(
        tasks,
        _render_task,
        max_workers=configured,
        failure_policy=CONTINUE_INDEPENDENT,
        **execution_kwargs,
    )

    global_stale: str | None = None
    try:
        _current_bindings_unchanged(project, tasks, context)
    except Exception as exc:
        global_stale = _sanitize_error(exc)

    scene_results: list[dict[str, Any]] = []
    published_candidates: list[AnnotationPreviewCandidate] = []
    published_order: list[str] = []
    failed_count = 0
    for result in report.results:
        task = result.task
        if global_stale is not None:
            failed_count += 1
            scene_results.append({"sceneId": task.scene_id, "status": "stale", "error": global_stale})
            continue
        if result.outcome is None or not result.outcome.ok or result.outcome.value is None:
            failed_count += 1
            error = (
                result.outcome.error.message
                if result.outcome is not None and result.outcome.error is not None
                else "preview candidate 未完成"
            )
            scene_results.append({"sceneId": task.scene_id, "status": "failed", "error": _sanitize_error(error)})
            continue
        candidate = result.outcome.value
        try:
            _publish_bytes_atomic(candidate.task.candidate_path, task.output_path, candidate.sha256)
            published_candidates.append(candidate)
            published_order.append(task.scene_id)
            scene_results.append(
                {
                    "sceneId": task.scene_id,
                    "status": "published_current_technical",
                    "file": task.output_path.relative_to(project.root).as_posix(),
                    "sha256": candidate.sha256,
                }
            )
        except Exception as exc:
            failed_count += 1
            scene_results.append({"sceneId": task.scene_id, "status": "failed", "error": _sanitize_error(exc)})

    contact_file: str | None = None
    contact_sha: str | None = None
    contact_error: str | None = None
    if failed_count == 0 and len(published_candidates) == len(tasks):
        try:
            contact_candidate = run_root / "contact-sheet" / "candidate.png"
            contact_candidate.parent.mkdir(parents=True, exist_ok=False)
            contact = build_annotation_preview_contact_sheet(published_candidates)
            contact.save(contact_candidate, format="PNG", compress_level=1, optimize=False)
            with Image.open(contact_candidate) as image:
                image.load()
                if image.format != "PNG" or image.mode != "RGB":
                    raise AnnotationPreviewBatchError("contact sheet candidate 无效")
            contact_sha = sha256_file(contact_candidate)
            contact_target = project.root / "previews" / "annotation-preview-contact-sheet.png"
            _publish_bytes_atomic(contact_candidate, contact_target, contact_sha)
            contact_file = contact_target.relative_to(project.root).as_posix()
        except Exception as exc:
            failed_count += 1
            contact_error = _sanitize_error(exc)

    all_passed = failed_count == 0 and len(published_candidates) == len(tasks) and contact_file is not None
    annotation_review_identity: str | None = None
    if all_passed:
        try:
            _current_bindings_unchanged(project, tasks, context)
            technical = write_annotation_review_technical(project, formals, context)
            annotation_review_identity = technical["identityHash"]
        except Exception as exc:
            all_passed = False
            failed_count += 1
            contact_error = _sanitize_error(exc)
    return {
        "contractVersion": PREVIEW_BATCH_CONTRACT,
        "status": "PASS" if all_passed else "FAIL",
        "partialSuccess": bool(published_candidates and not all_passed),
        "configuredConcurrency": configured,
        "effectiveConcurrency": report.effective_workers,
        "peakActiveWorkers": report.peak_active_workers,
        "taskCount": len(tasks),
        "publishedCount": len(published_candidates),
        "failedCount": failed_count,
        "publishedOrder": published_order,
        "scenes": scene_results,
        "contactSheet": contact_file,
        "contactSheetSha256": contact_sha,
        "contactSheetError": contact_error,
        "annotationBindingSha256": annotation_binding_sha256(formals),
        "annotationReviewIdentitySha256": annotation_review_identity,
        "userConfirmationRequired": True,
        "previewConfirmationWritten": False,
        "approvalWritten": False,
        "nextHumanGate": "annotation_review_confirmation" if all_passed else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 current annotation 区域预览与 contact sheet")
    parser.add_argument("--project", required=True)
    parser.add_argument("--all", action="store_true", dest="all_scenes")
    parser.add_argument("--config")
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    return parser


def _gate_exit_code(exc: BaseException) -> int:
    if isinstance(exc, RenderTimingError):
        text = str(exc).lower()
        if any(token in text for token in ("stale", "变化", "approval", "批准", "current narration")):
            return 5
        return 2
    if isinstance(exc, (AnnotationPreviewBatchError, ProjectValidationError, WorkspaceError, OSError, ValueError)):
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.all_scenes:
        parser.error("正式批量入口必须显式传 --all")
    try:
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        summary = generate_annotation_preview_batch(
            workspace,
            project,
            allow_v1_disabled_compat=args.allow_v1_disabled_compat,
        )
        exit_code = 0 if summary["status"] == "PASS" else 1
    except Exception as exc:
        exit_code = _gate_exit_code(exc)
        summary = {
            "contractVersion": PREVIEW_BATCH_CONTRACT,
            "status": "FAIL",
            "error": _sanitize_error(exc),
            "configuredConcurrency": None,
            "effectiveConcurrency": 0,
            "peakActiveWorkers": 0,
            "taskCount": 0,
            "publishedCount": 0,
            "failedCount": 0,
            "partialSuccess": False,
            "userConfirmationRequired": True,
            "previewConfirmationWritten": False,
            "approvalWritten": False,
        }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "AnnotationPreviewBatchError",
    "PREVIEW_BATCH_CONTRACT",
    "annotation_binding_sha256",
    "build_annotation_preview_contact_sheet",
    "generate_annotation_preview_batch",
    "main",
]
